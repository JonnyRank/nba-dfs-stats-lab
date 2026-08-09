"""Unit tests for the crosswalk: normalization, scoring, tiering, approval, writes.

The ops DB is stood in for by a second SQLite file attached under the same `ops`
alias the real code uses, so `match_players` runs against exactly the query shape
it will see in production without touching `G:\\`.

The name pairs are the real ones from the 2026-07-28 backfill — they are the
cases that actually distinguish a good matcher from a bad one, and pinning them
means a future change to `score_candidate` has to keep clearing them.
"""

import sqlite3
from pathlib import Path

import pytest

from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.crosswalk import (
    REVIEW_FLOOR,
    ApprovalError,
    Candidate,
    NameMatch,
    Tier,
    apply_approvals,
    clear_crosswalk,
    coverage,
    match_players,
    normalize_name,
    read_approvals,
    score_candidate,
    unmatched_report,
    write_crosswalk,
    write_review_csv,
)

# (ops player_id, ops PLAYER_NAME)
OPS_PLAYERS = (
    (2544, "LeBron James"),
    (1629029, "Luka Doncic"),
    (1630163, "MarJon Beauchamp"),
    (1630559, "Patrick Baldwin"),
    (1629057, "Robert Williams III"),
    (1642949, "Yanic Konan Niederhauser"),
    (1642905, "Yang Hansen"),
    (202334, "Ed Davis"),
    (202083, "Wesley Matthews"),
)


@pytest.fixture
def conn(tmp_path):
    """An analytics DB with a synthetic ops DB attached read-only under `ops`."""
    ops_path = tmp_path / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.execute(
        "CREATE TABLE dim_players (PLAYER_ID INTEGER PRIMARY KEY, PLAYER_NAME TEXT)"
    )
    ops.executemany("INSERT INTO dim_players VALUES (?, ?)", OPS_PLAYERS)
    ops.commit()
    ops.close()

    conn = sqlite3.connect(tmp_path / "analytics.db", uri=True)
    init_db(conn)
    conn.execute(
        "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
    )
    yield conn
    conn.close()


def add_players(conn, rows):
    """rows: (slate_id, dk_id, name)."""
    conn.executemany(
        "INSERT INTO slate_players (slate_id, dk_id, name, salary) VALUES (?, ?, ?, 5000)",
        rows,
    )
    conn.commit()


# --- normalize_name -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LeBron James", "lebron james"),
        ("Luka Dončić", "luka doncic"),  # accents folded via NFKD
        ("MarJon Beauchamp", "marjon beauchamp"),  # case-only variant
        ("Patrick Baldwin Jr.", "patrick baldwin"),  # trailing suffix dropped
        ("Robert Williams III", "robert williams"),
        ("Jaren Jackson Jr", "jaren jackson"),  # suffix without the period
        ("P.J. Washington", "pj washington"),  # periods close up, not split
        ("De'Aaron Fox", "deaaron fox"),  # apostrophe removed
        ("Shai Gilgeous-Alexander", "shai gilgeous alexander"),  # hyphen splits
        ("  Trailing  Space  ", "trailing space"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_keeps_suffix_like_token_that_is_not_trailing():
    """Only a *trailing* suffix token is dropped — a leading one is a real name."""
    assert normalize_name("Vi Alexander") == "vi alexander"
    assert normalize_name("Iv Jackson") == "iv jackson"


def test_normalize_does_not_empty_a_pure_suffix_name():
    """Degenerate, but it must not raise or produce a key that matches everything."""
    assert normalize_name("Jr.") == ""


# --- score_candidate ----------------------------------------------------------


def test_true_fuzzy_match_outscores_every_false_one():
    """The pairs from the real data, which is the only reason these numbers matter.

    `Yanic Niederhauser` (a dropped middle name) and `Hansen Yang` (a reversed
    name order) are real matches; the other two are different people whose raw
    character ratio is deceptively high.
    """
    true_pairs = [
        ("yanic niederhauser", "yanic konan niederhauser"),
        ("hansen yang", "yang hansen"),
    ]
    false_pairs = [
        ("rj davis", "jd davison"),
        ("cameron matthews", "garrison mathews"),
        ("thomas sorber", "thomas bryant"),
        ("nikola djurisic", "nikola jokic"),
        ("zack austin", "austin reaves"),
    ]
    worst_true = min(score_candidate(a, b)[0] for a, b in true_pairs)
    best_false = max(score_candidate(a, b)[0] for a, b in false_pairs)
    assert worst_true >= REVIEW_FLOOR
    assert best_false < worst_true


def test_reversed_name_order_is_rescued_by_the_token_sort():
    """Left-to-right these are 0.57 similar — under the floor, i.e. invisible."""
    score, reason = score_candidate("hansen yang", "yang hansen")
    assert score >= REVIEW_FLOOR
    assert "sorted" in reason


def test_score_is_zero_for_an_empty_name():
    assert score_candidate("", "lebron james") == (0.0, "empty name")


# --- match_players ------------------------------------------------------------


def test_exact_and_normalized_tiers_are_the_only_auto_matches(conn):
    add_players(
        conn,
        [
            ("s1", 1, "LeBron James"),  # exact
            ("s1", 2, "MarJon Beauchamp"),  # exact
            ("s1", 3, "Patrick Baldwin Jr."),  # normalized (suffix)
            ("s1", 4, "Robert Williams"),  # normalized (ops has the suffix)
            ("s1", 5, "Yanic Niederhauser"),  # review
            ("s1", 6, "Totally Unknown"),  # none
        ],
    )
    report = match_players(conn)
    tiers = {m.name: m.tier for m in report.matches}

    assert tiers["LeBron James"] is Tier.EXACT
    assert tiers["MarJon Beauchamp"] is Tier.EXACT
    assert tiers["Patrick Baldwin Jr."] is Tier.NORMALIZED
    assert tiers["Robert Williams"] is Tier.NORMALIZED
    assert tiers["Yanic Niederhauser"] is Tier.REVIEW
    assert tiers["Totally Unknown"] is Tier.NONE
    assert {m.name for m in report.auto} == {
        "LeBron James",
        "MarJon Beauchamp",
        "Patrick Baldwin Jr.",
        "Robert Williams",
    }


def test_review_tier_carries_candidates_but_no_decision(conn):
    """A proposal must not look like a decision to anything downstream."""
    add_players(conn, [("s1", 1, "Yanic Niederhauser")])
    match = match_players(conn).matches[0]
    assert match.tier is Tier.REVIEW
    assert match.player_id is None
    assert not match.approvable
    assert match.candidates[0].player_id == 1642949


@pytest.fixture
def colliding_conn(tmp_path):
    """An ops DB where two distinct players share one normalized key.

    Zero of these exist in the current snapshot; the guard is for the next one.
    """
    ops_path = tmp_path / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.execute(
        "CREATE TABLE dim_players (PLAYER_ID INTEGER PRIMARY KEY, PLAYER_NAME TEXT)"
    )
    # Both normalize to "gary payton" — the father and the son.
    ops.executemany(
        "INSERT INTO dim_players VALUES (?, ?)",
        [(1, "Gary Payton II"), (2, "Gary Payton")],
    )
    ops.commit()
    ops.close()

    conn = sqlite3.connect(tmp_path / "a.db", uri=True)
    init_db(conn)
    conn.execute(
        "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
    )
    yield conn
    conn.close()


def test_ambiguous_normalized_key_is_never_auto_matched(colliding_conn):
    """Two ops players behind one key is a data problem, not a coin flip.

    `Gary Payton Jr.` matches neither raw string, so it falls through to the
    normalized tier and finds both. Picking one would mis-attribute every box
    score for the other.
    """
    add_players(colliding_conn, [("s1", 1, "Gary Payton Jr.")])
    match = match_players(colliding_conn).matches[0]
    assert match.tier is Tier.AMBIGUOUS
    assert match.player_id is None
    assert not match.approvable
    assert {c.player_id for c in match.candidates} == {1, 2}
    assert match_players(colliding_conn).auto == []


def test_an_exact_raw_hit_beats_a_normalized_collision(colliding_conn):
    """An identical source string is unambiguous evidence even when the
    normalized key is not — otherwise a suffixed name could never auto-match."""
    add_players(colliding_conn, [("s1", 1, "Gary Payton II")])
    match = match_players(colliding_conn).matches[0]
    assert match.tier is Tier.EXACT
    assert match.player_id == 1


def test_duplicate_ops_names_are_ambiguous_at_the_exact_tier(tmp_path):
    """Two ops rows with the same PLAYER_NAME can't be told apart either."""
    ops_path = tmp_path / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.execute(
        "CREATE TABLE dim_players (PLAYER_ID INTEGER PRIMARY KEY, PLAYER_NAME TEXT)"
    )
    ops.executemany(
        "INSERT INTO dim_players VALUES (?, ?)", [(1, "Bob Smith"), (2, "Bob Smith")]
    )
    ops.commit()
    ops.close()

    conn = sqlite3.connect(tmp_path / "a.db", uri=True)
    init_db(conn)
    conn.execute(
        "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
    )
    add_players(conn, [("s1", 1, "Bob Smith")])

    match = match_players(conn).matches[0]
    assert match.tier is Tier.AMBIGUOUS
    assert match.player_id is None
    conn.close()


def test_an_empty_normalized_key_never_auto_matches(tmp_path):
    """`normalize_name` returns "" for a pure-suffix name. If that key were
    indexed, any other name normalizing to "" would meet it there and auto-write
    a NORMALIZED row between two unrelated players — without review, which is
    the one outcome the tiering exists to make impossible."""
    ops_path = tmp_path / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.execute(
        "CREATE TABLE dim_players (PLAYER_ID INTEGER PRIMARY KEY, PLAYER_NAME TEXT)"
    )
    ops.executemany("INSERT INTO dim_players VALUES (?, ?)", [(1, "III")])
    ops.commit()
    ops.close()

    conn = sqlite3.connect(tmp_path / "a.db", uri=True)
    init_db(conn)
    conn.execute(
        "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
    )
    add_players(conn, [("s1", 1, "Jr.")])

    match = match_players(conn).matches[0]
    assert normalize_name("Jr.") == normalize_name("III") == ""
    assert match.tier is not Tier.NORMALIZED
    assert match.player_id is None
    conn.close()


def test_unmatched_carries_its_nearest_candidate_for_context(conn):
    """The floor must not hide what the near miss was — that is how a real match
    below it (Hansen Yang, before the token sort) stays discoverable."""
    add_players(conn, [("s1", 1, "Completely Different")])
    match = match_players(conn).matches[0]
    assert match.tier is Tier.NONE
    assert len(match.candidates) == 1
    assert match.candidates[0].score < REVIEW_FLOOR


def test_dk_ids_are_grouped_per_name_across_slates(conn):
    """dk_id is per-slate: one name owns one id per slate it appears in."""
    add_players(conn, [("s1", 10, "LeBron James"), ("s2", 20, "LeBron James")])
    match = match_players(conn).matches[0]
    assert match.dk_ids == (10, 20)
    assert match.slate_count == 2
    assert match_players(conn).dk_id_coverage() == (2, 2)


def test_match_rate_and_counts(conn):
    add_players(conn, [("s1", 1, "LeBron James"), ("s1", 2, "Totally Unknown")])
    report = match_players(conn)
    assert report.match_rate() == 0.5
    assert report.counts()[Tier.EXACT.value] == 1
    assert report.counts()[Tier.NONE.value] == 1


def test_empty_slate_players_does_not_divide_by_zero(conn):
    report = match_players(conn)
    assert report.matches == []
    assert report.match_rate() == 0.0


# --- the review round-trip ----------------------------------------------------


def test_review_csv_round_trip(conn, tmp_path):
    add_players(conn, [("s1", 1, "Yanic Niederhauser"), ("s1", 2, "Hansen Yang")])
    report = match_players(conn)
    path = tmp_path / "review.csv"
    assert write_review_csv(report, path) == 2

    # Nothing is approved until Jonny edits the file.
    assert read_approvals(path) == {}

    # Stand in for that edit: tick `approve` on the Niederhauser row only.
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("\n,Yanic Niederhauser,", "\ny,Yanic Niederhauser,"),
        encoding="utf-8",
    )
    assert read_approvals(path) == {"Yanic Niederhauser": 1642949}

    resolved = apply_approvals(report, read_approvals(path))
    assert [(m.name, m.player_id) for m in resolved] == [
        ("Yanic Niederhauser", 1642949)
    ]


@pytest.mark.parametrize("flag", ["y", "Y", "yes", "TRUE", "1", "x"])
def test_approval_flags_accepted(tmp_path, flag):
    path = tmp_path / "r.csv"
    path.write_text(
        f"approve,dk_name,player_id\n{flag},Some Name,42\n", encoding="utf-8"
    )
    assert read_approvals(path) == {"Some Name": 42}


@pytest.mark.parametrize("flag", ["", "n", "no", "?", "later"])
def test_non_approval_flags_ignored(tmp_path, flag):
    path = tmp_path / "r.csv"
    path.write_text(
        f"approve,dk_name,player_id\n{flag},Some Name,42\n", encoding="utf-8"
    )
    assert read_approvals(path) == {}


def test_two_candidates_approved_for_one_name_is_rejected(tmp_path):
    """Last-write-wins would silently pick one — the likeliest review mistake."""
    path = tmp_path / "r.csv"
    path.write_text(
        "approve,dk_name,player_id\ny,RJ Davis,202334\ny,RJ Davis,1631098\n",
        encoding="utf-8",
    )
    with pytest.raises(ApprovalError, match="approved twice"):
        read_approvals(path)


def test_same_name_and_id_approved_twice_is_fine(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(
        "approve,dk_name,player_id\ny,RJ Davis,202334\ny,RJ Davis,202334\n",
        encoding="utf-8",
    )
    assert read_approvals(path) == {"RJ Davis": 202334}


def test_missing_columns_and_bad_ids_are_rejected(tmp_path):
    missing = tmp_path / "m.csv"
    missing.write_text("dk_name,player_id\nA,1\n", encoding="utf-8")
    with pytest.raises(ApprovalError, match="missing column"):
        read_approvals(missing)

    bad = tmp_path / "b.csv"
    bad.write_text("approve,dk_name,player_id\ny,A,not-an-int\n", encoding="utf-8")
    with pytest.raises(ApprovalError, match="not an integer"):
        read_approvals(bad)

    with pytest.raises(ApprovalError, match="no such review file"):
        read_approvals(tmp_path / "nope.csv")


def test_approving_an_id_that_was_never_offered_is_rejected(conn):
    """A hand-typed id that was never a candidate is a typo, not a decision."""
    add_players(conn, [("s1", 1, "Yanic Niederhauser")])
    report = match_players(conn)
    with pytest.raises(ApprovalError, match="never offered"):
        apply_approvals(report, {"Yanic Niederhauser": 2544})


def test_approving_an_unknown_name_is_rejected(conn):
    add_players(conn, [("s1", 1, "LeBron James")])
    report = match_players(conn)
    with pytest.raises(ApprovalError, match="not in slate_players"):
        apply_approvals(report, {"Nobody At All": 2544})


def test_a_sub_floor_near_miss_cannot_be_approved(conn):
    """The nearest candidate on an unmatched name is context, not a menu item.

    `write_review_csv` exports `needs_review` only, so a NONE-tier name was
    never offered — but it still carries the near miss `_score_all` attached for
    the report. Approving that would write a sub-floor guess with nothing
    flagging it, since the leak check whitelists approved names.
    """
    add_players(conn, [("s1", 1, "Completely Different")])
    report = match_players(conn)
    match = report.matches[0]
    assert match.tier is Tier.NONE
    assert match.candidates and match.candidates[0].score < REVIEW_FLOOR

    near_miss = match.candidates[0].player_id
    with pytest.raises(ApprovalError, match="never in the review queue"):
        apply_approvals(report, {"Completely Different": near_miss})


# --- writing ------------------------------------------------------------------


def test_write_crosswalk_fans_out_to_every_dk_id(conn):
    add_players(conn, [("s1", 10, "LeBron James"), ("s2", 20, "LeBron James")])
    report = match_players(conn)
    assert write_crosswalk(conn, report.auto) == 2
    rows = conn.execute(
        "SELECT dk_id, player_id, display_name FROM dk_crosswalk"
    ).fetchall()
    assert rows == [(10, 2544, "LeBron James"), (20, 2544, "LeBron James")]


def test_write_crosswalk_is_idempotent(conn):
    add_players(conn, [("s1", 10, "LeBron James")])
    report = match_players(conn)
    write_crosswalk(conn, report.auto)
    write_crosswalk(conn, report.auto)
    assert conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[0] == 1


def test_write_crosswalk_preserves_earlier_batches(conn):
    """Approvals arrive over time; a second batch must not drop the first."""
    add_players(conn, [("s1", 10, "LeBron James"), ("s1", 11, "Yanic Niederhauser")])
    report = match_players(conn)
    write_crosswalk(conn, report.auto)
    approved = apply_approvals(report, {"Yanic Niederhauser": 1642949})
    write_crosswalk(conn, approved)
    assert dict(conn.execute("SELECT dk_id, player_id FROM dk_crosswalk")) == {
        10: 2544,
        11: 1642949,
    }


def test_write_crosswalk_refuses_an_unresolved_match(conn):
    unresolved = NameMatch(
        name="RJ Davis",
        tier=Tier.REVIEW,
        dk_ids=(1,),
        candidates=(Candidate(202334, "Ed Davis", 0.7, "fuzzy"),),
    )
    with pytest.raises(ValueError, match="refusing to write unresolved"):
        write_crosswalk(conn, [unresolved])
    assert conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[0] == 0


def test_write_crosswalk_refuses_a_dk_id_claimed_by_two_players(conn):
    """`slate_players` is keyed (slate_id, dk_id), so one dk_id can carry two
    names — and two names are two NameMatches. The upsert would resolve that
    last-wins with nothing logged, which is the silent mis-attribution the whole
    module is built to prevent. It has to refuse instead."""
    contested = [
        NameMatch(name="LeBron James", tier=Tier.EXACT, dk_ids=(7,), player_id=2544),
        NameMatch(name="Luka Doncic", tier=Tier.EXACT, dk_ids=(7,), player_id=1629029),
    ]
    with pytest.raises(ValueError, match="claimed by two players"):
        write_crosswalk(conn, contested)
    assert conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[0] == 0


def test_write_crosswalk_allows_two_names_for_one_player(conn):
    """The legitimate direction: DK renamed Yanic Niederhauser mid-season, so two
    spellings map to one ops player. That stitches his October slates onto the
    rest of the season and must keep working."""
    written = write_crosswalk(
        conn,
        [
            NameMatch(
                name="Yanic Niederhauser",
                tier=Tier.REVIEW,
                dk_ids=(1, 2),
                player_id=1642949,
            ),
            NameMatch(
                name="Yanic Konan Niederhauser",
                tier=Tier.EXACT,
                dk_ids=(3,),
                player_id=1642949,
            ),
        ],
    )
    assert written == 3
    rows = conn.execute("SELECT DISTINCT player_id FROM dk_crosswalk").fetchall()
    assert rows == [(1642949,)]


def test_write_crosswalk_corrects_a_previous_mapping(conn):
    """A wrong approval must be fixable by re-approving, not only by --rebuild."""
    add_players(conn, [("s1", 10, "LeBron James")])
    write_crosswalk(conn, [NameMatch("LeBron James", Tier.EXACT, (10,), player_id=999)])
    write_crosswalk(conn, match_players(conn).auto)
    assert conn.execute("SELECT player_id FROM dk_crosswalk").fetchone()[0] == 2544


def test_clear_crosswalk(conn):
    add_players(conn, [("s1", 10, "LeBron James")])
    write_crosswalk(conn, match_players(conn).auto)
    assert clear_crosswalk(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[0] == 0


def test_write_crosswalk_of_nothing_is_a_no_op(conn):
    assert write_crosswalk(conn, []) == 0


# --- reporting ----------------------------------------------------------------


def test_unmatched_report_groups_by_name(conn):
    add_players(
        conn,
        [
            ("s1", 10, "LeBron James"),
            ("s1", 11, "Totally Unknown"),
            ("s2", 21, "Totally Unknown"),
        ],
    )
    write_crosswalk(conn, match_players(conn).auto)
    rows = unmatched_report(conn)
    assert len(rows) == 1
    assert rows[0].name == "Totally Unknown"
    assert rows[0].dk_id_count == 2
    assert rows[0].slate_count == 2
    assert (rows[0].first_slate, rows[0].last_slate) == ("s1", "s2")


def test_unmatched_report_is_empty_when_everything_is_mapped(conn):
    add_players(conn, [("s1", 10, "LeBron James")])
    write_crosswalk(conn, match_players(conn).auto)
    assert unmatched_report(conn) == []


def test_coverage_counts_rows_and_names(conn):
    add_players(conn, [("s1", 10, "LeBron James"), ("s1", 11, "Totally Unknown")])
    write_crosswalk(conn, match_players(conn).auto)
    stats = coverage(conn)
    assert stats == {
        "slate_player_rows": 2,
        "rows_covered": 1,
        "names": 2,
        "names_covered": 1,
        "crosswalk_rows": 1,
    }


def test_coverage_on_an_empty_db(conn):
    assert coverage(conn) == {
        "slate_player_rows": 0,
        "rows_covered": 0,
        "names": 0,
        "names_covered": 0,
        "crosswalk_rows": 0,
    }


# --- the tracked approvals file -----------------------------------------------
#
# `docs/crosswalk-approvals.csv` is the only non-reproducible input in the
# project: everything else rebuilds from source CSVs and code, and the docs
# actively tell you to delete analytics.db and re-ingest. Nothing else in the
# suite touches it, so a change to `score_candidate` or `REVIEW_FLOOR` that
# stopped offering one of these ids would turn the documented rebuild command
# into an ApprovalError — discovered on Jonny's machine, at gate time, phases
# later. These two tests move that failure into CI.

APPROVALS = Path(__file__).parents[1] / "docs" / "crosswalk-approvals.csv"

# The two names Jonny approved on 2026-08-09, and the ops ids he approved them to.
APPROVED = {"Yanic Niederhauser": 1642949, "Hansen Yang": 1642905}


def test_the_tracked_approvals_still_parse():
    assert read_approvals(APPROVALS) == APPROVED


def test_every_approved_id_is_still_offered_as_a_candidate(conn):
    """The half that actually catches a scoring regression: parsing proves the
    file is well-formed, but `apply_approvals` also requires each id to still be
    among the candidates the matcher proposes for that name."""
    add_players(
        conn,
        [("s1", i, name) for i, name in enumerate(APPROVED, start=1)],
    )
    report = match_players(conn)
    resolved = apply_approvals(report, read_approvals(APPROVALS))
    assert {m.name: m.player_id for m in resolved} == APPROVED
