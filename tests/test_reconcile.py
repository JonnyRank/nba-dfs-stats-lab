"""`ingest.reconcile` — the classifier, the ±1 resolution, and the write path.

Everything here runs against a temporary analytics DB with a temporary ops DB
attached, so the module needs neither `G:\\` nor a backfilled `data/analytics.db`.

The fixture is a deliberate miniature of the real data's awkward parts rather
than a happy path: a two-calendar-day slate, a player who plays the next night
but whose team does not (the 907-row trap), an off-slate game, a matchup absent
from ops, a DK float, a stat correction, a DNP, and an uncrosswalked name.
"""

import sqlite3

import pytest

from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.reconcile import (
    ACTIONS,
    AuditRow,
    GameLink,
    ReconcileError,
    apply_corrections,
    audit_rollup,
    build_game_index,
    load_ops_points,
    load_prior_audit,
    off_slate_games,
    off_slate_sides,
    ops_window,
    reconcile,
    resolve_game_dates,
    slate_game_sides,
    write_audit,
)

MAIN = "2026-05-18_classic_main"

# (player_id, name)
PLAYERS = {
    1: "Exact Agree",
    2: "Float Noise",
    3: "Stat Correction",
    4: "Two Day Slate",
    5: "Did Not Play",
    6: "Next Night",  # the 907-row trap: plays d+1 for a team not on this slate
    7: "Off Slate A",
    8: "Off Slate B",
    9: "Absent Game",
    10: "DK Unscored",
}

# ops.fantasy_logs: (date, player_id, team, opp, dk_points)
OPS_LOGS = [
    # BOS/NYK — played on the slate date.
    ("2026-05-18", 1, "Boston Celtics", "New York Knicks", 30.0),
    ("2026-05-18", 2, "Boston Celtics", "New York Knicks", 25.75),
    ("2026-05-18", 3, "New York Knicks", "Boston Celtics", 52.0),
    ("2026-05-18", 10, "New York Knicks", "Boston Celtics", 18.25),
    # OKC/SAS — the same slate's second game, played the *next* night.
    ("2026-05-19", 4, "Oklahoma City Thunder", "San Antonio Spurs", 84.0),
    # Player 6's team is not on this slate at all; he simply played on d+1.
    # A per-player +1 window would drag this 99.0 onto his slate row.
    ("2026-05-19", 6, "Miami Heat", "Orlando Magic", 99.0),
    # LAC/POR — the off-slate game: DK scored nobody, ops has a real box score.
    ("2026-05-18", 7, "Los Angeles Clippers", "Portland Trail Blazers", 52.0),
    ("2026-05-18", 8, "Portland Trail Blazers", "Los Angeles Clippers", 55.0),
]

TEAM_MAP = [
    ("Boston Celtics", "BOS"),
    ("New York Knicks", "NYK"),
    ("Oklahoma City Thunder", "OKC"),
    ("San Antonio Spurs", "SAS"),
    ("Los Angeles Clippers", "LAC"),
    ("Portland Trail Blazers", "POR"),
    ("Miami Heat", "MIA"),
    ("Orlando Magic", "ORL"),
    ("Dallas Mavericks", "DAL"),
    ("Milwaukee Bucks", "MIL"),
]

# slate_players: (dk_id, name, team, opp, actual_fpts) — dk_id doubles as the
# crosswalk key so the fixture stays readable; the real ids are per-slate.
SLATE_ROWS = [
    (1, "Exact Agree", "BOS", "NYK", 30.0),
    (2, "Float Noise", "BOS", "NYK", 25.746666666666663),
    (3, "Stat Correction", "NYK", "BOS", 54.0),
    (10, "DK Unscored", "NYK", "BOS", 0.0),
    (4, "Two Day Slate", "OKC", "SAS", 84.0),
    (5, "Did Not Play", "OKC", "SAS", 0.0),
    (6, "Next Night", "SAS", "OKC", 0.0),
    (7, "Off Slate A", "LAC", "POR", 0.0),
    (8, "Off Slate B", "POR", "LAC", 0.0),
    # DAL/MIL: all-zero on both sides (so off-slate) *and* absent from ops on
    # every date (so no_ops_game), with an uncrosswalked player on one side.
    # Three facts, one `reason` column — the collision behind the off_slate flag.
    (9, "Absent Game", "DAL", "MIL", 0.0),
    (11, "Uncrosswalked", "MIL", "DAL", 0.0),
]

CROSSWALKED = {dk_id for dk_id, *_ in SLATE_ROWS} - {11}


# The one test that appends a next-season ops row needs the file back; the
# ATTACH is read-only by design, so it detaches, writes, and re-attaches.
_ops_paths: list = []


@pytest.fixture
def conn(tmp_path):
    ops_path = tmp_path / "ops.db"
    _ops_paths.clear()
    _ops_paths.append(ops_path)
    ops = sqlite3.connect(ops_path)
    ops.executescript(
        """
        CREATE TABLE fantasy_logs (
          DATE TEXT, PLAYER_ID INTEGER, TEAM TEXT, OPPONENT TEXT,
          MINUTES REAL, DK_POINTS REAL);
        CREATE TABLE map_teams (RAW_TEAM_NAME TEXT, TEAM_ABBREVIATION TEXT);
        CREATE TABLE dim_players (PLAYER_ID INTEGER PRIMARY KEY, PLAYER_NAME TEXT);
        """
    )
    ops.executemany(
        "INSERT INTO fantasy_logs (DATE, PLAYER_ID, TEAM, OPPONENT, MINUTES, DK_POINTS) "
        "VALUES (?, ?, ?, ?, 20.0, ?)",
        OPS_LOGS,
    )
    ops.executemany("INSERT INTO map_teams VALUES (?, ?)", TEAM_MAP)
    ops.executemany("INSERT INTO dim_players VALUES (?, ?)", PLAYERS.items())
    ops.commit()
    ops.close()

    conn = sqlite3.connect(tmp_path / "analytics.db", uri=True)
    init_db(conn)
    conn.executemany(
        "INSERT INTO slate_players (slate_id, dk_id, name, team, opp, salary, actual_fpts) "
        "VALUES (?, ?, ?, ?, ?, 5000, ?)",
        [(MAIN, *row) for row in SLATE_ROWS],
    )
    conn.executemany(
        "INSERT INTO dk_crosswalk (dk_id, player_id, display_name) VALUES (?, ?, ?)",
        [(dk_id, dk_id, PLAYERS[dk_id]) for dk_id in sorted(CROSSWALKED)],
    )
    conn.commit()
    conn.execute(
        "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
    )
    yield conn
    conn.close()


def verdicts(report):
    """`{dk_id: (action, reason)}` — the shape most assertions want."""
    return {r.dk_id: (r.action, r.reason) for r in report.rows}


# --- Reading both sides --------------------------------------------------------


def test_game_index_translates_team_names_to_abbreviations(conn):
    index = build_game_index(conn, since="2026-01-01")
    assert ("2026-05-18", "BOS", "NYK") in index
    assert ("2026-05-19", "OKC", "SAS") in index
    assert index["2026-05-18", "BOS", "NYK"].player_count == 2


def test_game_index_since_bounds_the_scan(conn):
    assert build_game_index(conn, since="2026-06-01") == {}


def test_ops_points_are_keyed_by_player_and_date(conn):
    points = load_ops_points(conn, since="2026-01-01")
    assert points[1, "2026-05-18"] == 30.0
    assert (1, "2026-05-19") not in points


def test_slate_game_sides_are_distinct_matchups(conn):
    assert slate_game_sides(conn) == {
        ("2026-05-18", "BOS", "NYK"),
        ("2026-05-18", "NYK", "BOS"),
        ("2026-05-18", "OKC", "SAS"),
        ("2026-05-18", "SAS", "OKC"),
        ("2026-05-18", "LAC", "POR"),
        ("2026-05-18", "POR", "LAC"),
        ("2026-05-18", "DAL", "MIL"),
        ("2026-05-18", "MIL", "DAL"),
    }


def test_alias_must_be_an_identifier(conn):
    with pytest.raises(ValueError, match="invalid attach alias"):
        build_game_index(conn, alias="ops; DROP TABLE slate_players")


# --- Resolution ----------------------------------------------------------------


def test_same_day_resolves_without_a_shift(conn):
    links = resolve_game_dates(build_game_index(conn, since="2026-01-01"), slate_game_sides(conn))
    link = links["2026-05-18", "BOS", "NYK"]
    assert (link.game_date, link.shift_days, link.shifted) == ("2026-05-18", 0, False)


def test_the_second_game_of_a_two_day_slate_resolves_at_plus_one(conn):
    links = resolve_game_dates(build_game_index(conn, since="2026-01-01"), slate_game_sides(conn))
    link = links["2026-05-18", "OKC", "SAS"]
    assert (link.game_date, link.shift_days, link.shifted) == ("2026-05-19", 1, True)


def test_a_matchup_absent_from_ops_resolves_to_nothing(conn):
    links = resolve_game_dates(build_game_index(conn, since="2026-01-01"), slate_game_sides(conn))
    link = links["2026-05-18", "DAL", "MIL"]
    assert (link.game_date, link.shift_days, link.shifted) == (None, None, False)


def test_same_day_wins_over_a_neighbouring_game_between_the_same_teams():
    # A team playing the same opponent on d-1, d and d+1 must take d. Same-day
    # first is what makes a back-to-back rematch unambiguous.
    index = {
        (d, "BOS", "NYK"): GameLink(d, "BOS", "NYK", d, 0)
        for d in ("2026-05-17", "2026-05-18", "2026-05-19")
    }
    links = resolve_game_dates(index, [("2026-05-18", "BOS", "NYK")])
    assert links["2026-05-18", "BOS", "NYK"].game_date == "2026-05-18"


def test_plus_one_wins_over_minus_one():
    # On the 05-21 slate OKC/SAS exists in ops at both 05-20 and 05-22. Only the
    # later game can still be on a slate, so +1 must be tried first.
    index = {(d, "OKC", "SAS"): object() for d in ("2026-05-20", "2026-05-22")}
    links = resolve_game_dates(index, [("2026-05-21", "OKC", "SAS")])
    assert links["2026-05-21", "OKC", "SAS"].shift_days == 1


# --- Off-slate detection -------------------------------------------------------


def test_off_slate_finds_only_the_reciprocating_all_zero_matchup(conn):
    sides = off_slate_games(conn)
    assert (MAIN, "LAC", "POR") in sides and (MAIN, "POR", "LAC") in sides
    # DAL/MIL is all-zero on both sides too — it is off-slate as well, and its
    # game is *also* missing from ops, which is what separates the two reasons.
    assert (MAIN, "DAL", "MIL") in sides
    # OKC's rows are not all zero (Two Day Slate scored 84), so it is not.
    assert (MAIN, "OKC", "SAS") not in sides


def test_a_one_sided_zero_is_not_off_slate(conn):
    # A lopsided real result, or a team whose opponent isn't on the slate.
    conn.execute(
        "UPDATE slate_players SET actual_fpts = 10.0 WHERE dk_id = 8"
    )  # POR now scored
    assert not {s for s in off_slate_games(conn) if s[1] in ("LAC", "POR")}


# --- Classification ------------------------------------------------------------


def test_every_row_is_classified_exactly_once(conn):
    report = reconcile(conn)
    assert len(report.rows) == len(SLATE_ROWS)
    assert sum(report.action_counts().values()) == len(SLATE_ROWS)
    assert set(report.action_counts()) == set(ACTIONS)


def test_an_exact_agreement_is_unchanged_with_no_reason(conn):
    assert verdicts(reconcile(conn))[1] == ("unchanged", None)


def test_a_dk_float_is_corrected_as_float_noise(conn):
    report = reconcile(conn)
    assert verdicts(report)[2] == ("corrected", "float_noise")
    row = next(r for r in report.rows if r.dk_id == 2)
    assert row.ops_value == 25.75 and abs(row.delta) < 0.01


def test_a_scoring_change_is_corrected_as_a_stat_correction(conn):
    report = reconcile(conn)
    assert verdicts(report)[3] == ("corrected", "stat_correction")
    assert next(r for r in report.rows if r.dk_id == 3).delta == -2.0


def test_a_dk_zero_against_a_real_box_score_is_dk_unscored(conn):
    assert verdicts(reconcile(conn))[10] == ("corrected", "dk_unscored")


def test_the_shifted_game_takes_its_value_from_the_next_day(conn):
    report = reconcile(conn)
    row = next(r for r in report.rows if r.dk_id == 4)
    assert (row.game_date, row.action, row.reason) == ("2026-05-19", "unchanged", "date_shift")


def test_a_dnp_on_a_shifted_game_stays_zero_and_reads_as_a_dnp(conn):
    report = reconcile(conn)
    row = next(r for r in report.rows if r.dk_id == 5)
    assert (row.action, row.reason, row.ops_value) == ("no_ops_row", "dnp", None)


def test_the_907_row_trap_a_player_who_played_the_next_night_is_not_pulled_in(conn):
    # Player 6 has a 99.0 ops log at d+1 — for MIA/ORL, a game his slate team
    # never played. His own matchup (SAS/OKC) resolves to d+1 as a whole, and he
    # has no log for *that* game, so he stays 0. A per-player window would write
    # 99.0 here, which is the entire failure mode this module is shaped around.
    report = reconcile(conn)
    row = next(r for r in report.rows if r.dk_id == 6)
    assert (row.action, row.ops_value, row.dk_value) == ("no_ops_row", None, 0.0)
    assert report.split_matchups() == []


def test_off_slate_rows_are_corrected_and_flagged(conn):
    report = reconcile(conn)
    row = next(r for r in report.rows if r.dk_id == 7)
    assert (row.action, row.reason, row.off_slate, row.ops_value) == (
        "corrected",
        "off_slate",
        True,
        52.0,
    )


def test_a_matchup_absent_from_ops_is_no_ops_game_even_though_it_is_off_slate(conn):
    # The two facts are orthogonal: `reason` takes the narrower cause, the
    # `off_slate` flag still carries the row. This is the collision that made
    # off_slate a column rather than a reason value.
    report = reconcile(conn)
    row = next(r for r in report.rows if r.dk_id == 9)
    assert (row.action, row.reason, row.off_slate) == ("no_ops_row", "no_ops_game", True)


def test_an_uncrosswalked_row_in_an_off_slate_game_keeps_no_crosswalk(conn):
    report = reconcile(conn)
    row = next(r for r in report.rows if r.dk_id == 11)
    assert (row.action, row.reason, row.off_slate, row.player_id) == (
        "unmapped",
        "no_crosswalk",
        True,
        None,
    )
    # The flag is the population; the reason is the primary cause.
    assert len(report.off_slate_rows()) == 4  # LAC, POR, DAL, and the unmapped MIL row


def test_a_row_with_no_team_falls_back_to_the_slate_date(conn):
    conn.execute("UPDATE slate_players SET team = NULL, opp = NULL WHERE dk_id = 1")
    row = next(r for r in reconcile(conn).rows if r.dk_id == 1)
    assert (row.game_date, row.action, row.matchup) == ("2026-05-18", "unchanged", None)


def test_slate_ids_scopes_the_pass(conn):
    conn.execute(
        "INSERT INTO slate_players (slate_id, dk_id, name, team, opp, salary, actual_fpts) "
        "VALUES ('2026-05-19_classic_main', 99, 'Exact Agree', 'BOS', 'NYK', 5000, 0.0)"
    )
    assert {r.slate_id for r in reconcile(conn, slate_ids=[MAIN]).rows} == {MAIN}
    assert len(reconcile(conn).rows) == len(SLATE_ROWS) + 1


def test_an_empty_slate_id_list_is_refused(conn):
    # `None` means every slate; `[]` silently meaning the same is the accident.
    with pytest.raises(ReconcileError, match="pass None"):
        reconcile(conn, slate_ids=[])


# --- Writing -------------------------------------------------------------------


def test_write_audit_is_a_full_census(conn):
    report = reconcile(conn)
    assert write_audit(conn, report.rows) == len(SLATE_ROWS)
    assert conn.execute("SELECT COUNT(*) FROM fpts_audit").fetchone()[0] == len(SLATE_ROWS)
    missing = conn.execute(
        "SELECT COUNT(*) FROM slate_players sp LEFT JOIN fpts_audit a "
        " ON a.slate_id = sp.slate_id AND a.dk_id = sp.dk_id WHERE a.dk_id IS NULL"
    ).fetchone()[0]
    assert missing == 0


def test_apply_corrections_only_touches_corrected_rows(conn):
    report = reconcile(conn)
    write_audit(conn, report.rows)
    assert apply_corrections(conn, report.rows) == len(report.corrections)
    values = dict(conn.execute("SELECT dk_id, actual_fpts FROM slate_players"))
    assert values[2] == 25.75  # float noise cleaned up
    assert values[3] == 52.0  # stat correction applied
    assert values[7] == 52.0  # off-slate box score written
    assert values[1] == 30.0  # already agreed, untouched
    assert values[5] == 0.0  # DNP zero stays 0
    assert values[9] == 0.0  # no ops evidence, untouched
    assert values[11] == 0.0  # uncrosswalked, unreachable


def test_no_nulls_are_introduced(conn):
    report = reconcile(conn)
    write_audit(conn, report.rows)
    apply_corrections(conn, report.rows)
    nulls = conn.execute(
        "SELECT COUNT(*) FROM slate_players WHERE actual_fpts IS NULL"
    ).fetchone()[0]
    assert nulls == 0


def test_a_correction_with_no_ops_value_is_refused(conn):
    row = AuditRow(MAIN, 1, 1, "2026-05-18", 30.0, None, "corrected", "stat_correction", False)
    with pytest.raises(ReconcileError, match="no ops_value"):
        apply_corrections(conn, [row])


def test_an_unknown_action_is_refused(conn):
    row = AuditRow(MAIN, 1, 1, "2026-05-18", 30.0, 30.0, "fixed", None, False)
    with pytest.raises(ReconcileError, match="unknown action"):
        write_audit(conn, [row])


def test_write_audit_replaces_a_slate_rather_than_accumulating(conn):
    report = reconcile(conn)
    write_audit(conn, report.rows)
    conn.execute("DELETE FROM slate_players WHERE dk_id = 11")
    write_audit(conn, reconcile(conn).rows)
    # The stale census row for the departed player is gone, not left orphaned.
    assert conn.execute("SELECT COUNT(*) FROM fpts_audit").fetchone()[0] == len(SLATE_ROWS) - 1


# --- Idempotency (§3.4) --------------------------------------------------------


def _digest(conn):
    return conn.execute(
        "SELECT COUNT(*), TOTAL(actual_fpts), TOTAL(actual_fpts * actual_fpts) FROM slate_players"
    ).fetchone()


def test_a_second_pass_changes_nothing(conn):
    first = reconcile(conn)
    write_audit(conn, first.rows)
    apply_corrections(conn, first.rows)
    after_first, audit_first = _digest(conn), conn.execute(
        "SELECT COUNT(*), TOTAL(dk_value), TOTAL(ops_value) FROM fpts_audit"
    ).fetchone()

    second = reconcile(conn)
    write_audit(conn, second.rows)
    apply_corrections(conn, second.rows)
    assert _digest(conn) == after_first
    assert (
        conn.execute(
            "SELECT COUNT(*), TOTAL(dk_value), TOTAL(ops_value) FROM fpts_audit"
        ).fetchone()
        == audit_first
    )


def test_the_off_slate_flag_survives_a_second_pass(conn):
    # The trap: the off-slate signature is "both sides at exactly 0", and the
    # first pass overwrites exactly those zeros. Reading the detector off the
    # live actual_fpts finds the game once and never again, silently dropping
    # the flag from every off-slate row — which is the one thing that keeps the
    # population excludable from a backtest (D3).
    first = reconcile(conn)
    write_audit(conn, first.rows)
    apply_corrections(conn, first.rows)
    assert {r.dk_id for r in first.off_slate_rows()} == {7, 8, 9, 11}

    # The live values no longer carry the signature at all...
    assert not {s for s in off_slate_games(conn) if s[1] in ("LAC", "POR")}
    # ...but the pass reconstructs it from the pristine dk_values.
    second = reconcile(conn)
    assert {r.dk_id for r in second.off_slate_rows()} == {7, 8, 9, 11}
    # Still `corrected`, not `unchanged`: the audit records the relationship
    # between the two sources (DK said 0, ops says 52), not whether this
    # particular run moved a byte. That is what makes it re-derivable.
    assert verdicts(second)[7] == ("corrected", "off_slate")


def test_off_slate_sides_takes_pristine_values_directly():
    assert off_slate_sides(
        [
            ("s1", "LAC", "POR", 0.0),
            ("s1", "POR", "LAC", 0.0),
            ("s1", "BOS", "NYK", 0.0),  # zeroed, but NYK is not
            ("s1", "NYK", "BOS", 30.0),
        ]
    ) == {("s1", "LAC", "POR"), ("s1", "POR", "LAC")}


def test_the_pristine_dk_value_survives_a_second_pass(conn):
    first = reconcile(conn)
    write_audit(conn, first.rows)
    apply_corrections(conn, first.rows)
    second = reconcile(conn)
    # Reading actual_fpts back would capture 52.0 as what DraftKings said.
    assert next(r for r in second.rows if r.dk_id == 3).dk_value == 54.0
    assert next(r for r in second.rows if r.dk_id == 2).dk_value == 25.746666666666663


def test_a_re_ingested_slate_re_pins_its_own_dk_value(conn):
    first = reconcile(conn)
    write_audit(conn, first.rows)
    apply_corrections(conn, first.rows)
    # load_slate is delete-then-insert, so re-ingesting restores DK's raw value
    # — and here Jonny has also fixed the source CSV to 53.0 by hand.
    conn.execute("UPDATE slate_players SET actual_fpts = 53.0 WHERE dk_id = 3")

    row = next(r for r in reconcile(conn).rows if r.dk_id == 3)
    assert row.dk_value == 53.0 and row.action == "corrected" and row.ops_value == 52.0


def test_a_crash_between_the_two_writes_still_yields_the_true_dk_value(conn):
    # write_audit runs before apply_corrections precisely so this is recoverable:
    # the audit says ops_value 52.0, slate_players still holds DK's 54.0, so the
    # next pass takes 54.0 as pristine rather than believing the audit.
    first = reconcile(conn)
    write_audit(conn, first.rows)  # ...and nothing else runs
    row = next(r for r in reconcile(conn).rows if r.dk_id == 3)
    assert row.dk_value == 54.0


def test_load_prior_audit_reads_back_what_was_written(conn):
    report = reconcile(conn)
    write_audit(conn, report.rows)
    prior = load_prior_audit(conn)
    assert prior[MAIN, 3] == (54.0, 52.0)
    assert prior[MAIN, 11] == (0.0, None)


# --- Rollup --------------------------------------------------------------------


def test_audit_rollup_reports_the_stored_census(conn):
    report = reconcile(conn)
    write_audit(conn, report.rows)
    rollup = audit_rollup(conn)
    assert dict(rollup["actions"])["corrected"] == len(report.corrections)
    assert dict(rollup["off_slate"])["corrected"] == 2  # the LAC/POR pair
    assert rollup["shifted"] == [("2026-05-19", 2)]  # the two OKC/SAS rows


def test_audit_rollup_on_an_empty_table(conn):
    assert audit_rollup(conn)["actions"] == []


# --- PR #11 review round -------------------------------------------------------


def test_the_ops_window_is_derived_from_the_slates_not_hardcoded(conn):
    # A hardcoded season start silently bounds the pass to one season: the first
    # slate of the next one finds no game-sides and every row lands as
    # `no_ops_game` — the right action by accident, the wrong reason.
    assert ops_window(conn) == "2026-05-17"  # earliest slate date, minus one day

    conn.execute(
        "INSERT INTO slate_players (slate_id, dk_id, name, team, opp, salary, actual_fpts) "
        "VALUES ('2027-01-04_classic_main', 900, 'Next Season', 'BOS', 'NYK', 5000, 0.0)"
    )
    assert ops_window(conn) == "2026-05-17"  # still bounded by the earliest
    assert ops_window(conn, slate_ids=["2027-01-04_classic_main"]) == "2027-01-03"


def test_the_ops_window_of_an_empty_db_loads_nothing(conn):
    conn.execute("DELETE FROM slate_players")
    assert ops_window(conn) == "9999-12-31"


def test_a_next_season_slate_still_resolves_its_games(conn):
    # The regression the hardcoded window would have caused. Ops gains a game a
    # season later; a bounded scan would never see it.
    conn.execute(
        "INSERT INTO slate_players (slate_id, dk_id, name, team, opp, salary, actual_fpts) "
        "VALUES ('2027-01-04_classic_main', 1, 'Exact Agree', 'BOS', 'NYK', 5000, 41.0)"
    )
    conn.execute("DETACH DATABASE ops")
    ops_path = [p for p in _ops_paths if p.name == "ops.db"][0]
    ops = sqlite3.connect(ops_path)
    ops.execute(
        "INSERT INTO fantasy_logs (DATE, PLAYER_ID, TEAM, OPPONENT, MINUTES, DK_POINTS) "
        "VALUES ('2027-01-04', 1, 'Boston Celtics', 'New York Knicks', 30.0, 41.0)"
    )
    ops.commit()
    ops.close()
    conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))

    row = next(
        r for r in reconcile(conn).rows if r.slate_id == "2027-01-04_classic_main"
    )
    assert (row.action, row.reason, row.ops_value) == ("unchanged", None, 41.0)


def test_a_half_null_side_is_not_read_as_off_slate():
    # A missing value can't be shown to be 0, and the claim is about *every*
    # player on the side — so partial evidence must not produce a game-level
    # verdict that goes on to overwrite real box scores.
    assert off_slate_sides(
        [
            ("s1", "LAC", "POR", 0.0),
            ("s1", "LAC", "POR", None),
            ("s1", "POR", "LAC", 0.0),
        ]
    ) == set()
    # ...and the same matchup with the value present is still detected.
    assert off_slate_sides(
        [
            ("s1", "LAC", "POR", 0.0),
            ("s1", "LAC", "POR", 0.0),
            ("s1", "POR", "LAC", 0.0),
        ]
    ) == {("s1", "LAC", "POR"), ("s1", "POR", "LAC")}


def test_a_hand_fix_that_lands_on_the_ops_value_keeps_the_stale_dk_value(conn):
    # The one case `_pristine_dk_value` cannot see, pinned so it is a known
    # boundary rather than a surprise. The two states are indistinguishable
    # from actual_fpts alone; it costs an audit row's accuracy, never a value.
    first = reconcile(conn)
    write_audit(conn, first.rows)
    apply_corrections(conn, first.rows)

    # Jonny fixes the source CSV to agree with ops exactly, and re-ingests.
    conn.execute("UPDATE slate_players SET actual_fpts = 52.0 WHERE dk_id = 3")

    row = next(r for r in reconcile(conn).rows if r.dk_id == 3)
    assert row.dk_value == 54.0  # superseded, and not detectable from here
    assert row.action == "corrected"

    # The documented manual reset clears it.
    conn.execute("DELETE FROM fpts_audit WHERE slate_id = ?", (MAIN,))
    repinned = next(r for r in reconcile(conn).rows if r.dk_id == 3)
    assert (repinned.dk_value, repinned.action, repinned.reason) == (52.0, "unchanged", None)
