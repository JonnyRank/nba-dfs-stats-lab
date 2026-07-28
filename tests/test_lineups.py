"""Lineups source: dk_id extraction, slot validation, the melt, and discovery."""

import pytest

from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.lineups import (
    extract_dk_id,
    find_lineups_file,
    ingest_lineups,
    latest_lineups_by_slate,
    load_lineups_manifest,
    normalize_lineup_players,
    normalize_lineups,
    read_lineups,
    validate_lineups,
)
from nba_dfs_stats_lab.ingest.schemas import SlateValidationError

CSV_HEADER = (
    "Final_Rank,Lineup_Score,Total_Projection,Total_Ownership,Geomean_Ownership,"
    "Proj_Rank,Own_Rank,Geo_Rank,PG,SG,SF,PF,C,G,F,UTIL\n"
)


def _row(rank: int, first_id: int = 100) -> str:
    slots = ",".join(f"Player {i} ({first_id + i})" for i in range(8))
    return f"{rank},36.5,245.9,244.2,29.5,4.0,342.5,329.0,{slots}\n"


CSV_ROWS = _row(1, 100) + _row(2, 200)
SLATE_ID = "2026-05-18_classic_main"


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "ranked-lineups-2026-05-18_165150.csv"
    p.write_text(CSV_HEADER + CSV_ROWS)
    return p


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path / "analytics.db")
    init_db(conn)
    yield conn
    conn.close()


# --- extract_dk_id ------------------------------------------------------------


def test_extract_plain():
    assert extract_dk_id("Jamal Shead (42131681)") == 42131681


def test_extract_apostrophe_and_punctuation_in_name():
    assert extract_dk_id("De'Aaron Fox (43090637)") == 43090637
    assert extract_dk_id("Karl-Anthony Towns Jr. (11111111)") == 11111111


def test_extract_malformed_returns_none():
    assert extract_dk_id("Jamal Shead") is None  # no id at all
    assert extract_dk_id("") is None
    assert extract_dk_id(None) is None
    assert extract_dk_id(float("nan")) is None


def test_extract_ambiguous_returns_none():
    # Two parenthesised numbers: which one is the id? Surface, don't guess.
    assert extract_dk_id("Player (7) (42131681)") is None


# --- validate -----------------------------------------------------------------


def test_valid_file_passes(csv_path):
    report = validate_lineups(read_lineups(csv_path))
    assert report.ok
    assert report.row_count == 2


def test_slot_columns_are_not_flagged_unexpected(csv_path):
    assert validate_lineups(read_lineups(csv_path)).warnings == []


def test_missing_slot_column_fails(tmp_path):
    p = tmp_path / "noslot.csv"
    header = CSV_HEADER.replace(",UTIL\n", "\n")
    row = _row(1).replace(",Player 7 (107)", "")
    p.write_text(header + row)
    report = validate_lineups(read_lineups(p))
    assert not report.ok
    assert any("UTIL" in e for e in report.errors)


def test_malformed_slot_cell_fails(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text(CSV_HEADER + _row(1).replace("Player 3 (103)", "Player 3"))
    report = validate_lineups(read_lineups(p))
    assert not report.ok
    assert any("no single dk_id" in e for e in report.errors)


def test_empty_slot_cell_fails(tmp_path):
    p = tmp_path / "blank.csv"
    p.write_text(CSV_HEADER + _row(1).replace("Player 3 (103)", ""))
    report = validate_lineups(read_lineups(p))
    assert not report.ok
    assert any("no single dk_id" in e for e in report.errors)


def test_repeated_player_in_lineup_fails(tmp_path):
    p = tmp_path / "repeat.csv"
    p.write_text(CSV_HEADER + _row(1).replace("Player 3 (103)", "Player 0 (100)"))
    report = validate_lineups(read_lineups(p))
    assert not report.ok
    assert any("repeated player" in e for e in report.errors)


def test_repeated_player_without_final_rank_reports_not_raises(tmp_path):
    # Regression: the repeated-player message indexed Final_Rank, so a file
    # missing that column raised KeyError instead of returning a report — which
    # would kill the Phase 4 orchestrator's per-slate SlateValidationError catch.
    p = tmp_path / "norank.csv"
    header = CSV_HEADER.replace("Final_Rank,", "")
    row = _row(1).replace("Player 3 (103)", "Player 0 (100)").split(",", 1)[1]
    p.write_text(header + row)
    report = validate_lineups(read_lineups(p))
    assert not report.ok
    assert any("Final_Rank" in e and "missing" in e for e in report.errors)
    assert any("repeated player" in e for e in report.errors)


def test_duplicate_final_rank_fails(tmp_path):
    p = tmp_path / "duprank.csv"
    p.write_text(CSV_HEADER + _row(1, 100) + _row(1, 200))
    report = validate_lineups(read_lineups(p))
    assert not report.ok
    assert any("duplicate" in e for e in report.errors)


def test_fractional_ranks_pass(tmp_path):
    # Proj_Rank / Own_Rank / Geo_Rank are average-ranks: ties split. REAL, not INTEGER.
    p = tmp_path / "frac.csv"
    p.write_text(CSV_HEADER + _row(1).replace("4.0,342.5,329.0", "4.5,342.5,329.5"))
    assert validate_lineups(read_lineups(p)).ok


def test_trailing_exposure_columns_warn_but_pass(tmp_path):
    # One real file has an exposure report glued onto the right-hand side.
    p = tmp_path / "extra.csv"
    p.write_text(
        CSV_HEADER.rstrip("\n") + ",Player,Total Exp\n" + _row(1).rstrip("\n") + ",A,0.4\n"
    )
    report = validate_lineups(read_lineups(p))
    assert report.ok
    assert any("Total Exp" in w for w in report.warnings)


# --- normalize ----------------------------------------------------------------


def test_normalize_lineups_shape(csv_path):
    out = normalize_lineups(read_lineups(csv_path), SLATE_ID)
    assert list(out.columns) == [
        "slate_id", "final_rank", "lineup_score", "total_projection",
        "total_ownership", "geomean_ownership", "proj_rank", "own_rank", "geo_rank",
    ]
    assert out["final_rank"].tolist() == [1, 2]
    assert out["own_rank"].tolist() == [342.5, 342.5]


def test_melt_shape_and_order(csv_path):
    out = normalize_lineup_players(read_lineups(csv_path), SLATE_ID)
    assert list(out.columns) == ["slate_id", "final_rank", "slot", "dk_id"]
    assert len(out) == 16  # 2 lineups x 8 slots
    assert out["final_rank"].tolist() == [1] * 8 + [2] * 8
    assert out["slot"].tolist()[:8] == ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
    assert out["dk_id"].tolist()[:8] == list(range(100, 108))
    assert out["dk_id"].notna().all()


# --- ingest (end-to-end) ------------------------------------------------------


def test_ingest_writes_both_tables(csv_path, conn):
    result = ingest_lineups(csv_path, SLATE_ID, conn)
    assert result == (2, 16)
    assert conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 16


def test_ingest_eight_players_per_lineup(csv_path, conn):
    ingest_lineups(csv_path, SLATE_ID, conn)
    counts = conn.execute(
        "SELECT COUNT(*) FROM lineup_players WHERE slate_id = ? GROUP BY final_rank",
        (SLATE_ID,),
    ).fetchall()
    assert [c[0] for c in counts] == [8, 8]


def test_ingest_reload_idempotent(csv_path, conn):
    ingest_lineups(csv_path, SLATE_ID, conn)
    ingest_lineups(csv_path, SLATE_ID, conn)
    assert conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 16


def test_ingest_non_contiguous_final_ranks_round_trips(tmp_path, conn):
    # Final_Rank is the in-slate key, not a row counter: nothing requires it to
    # start at 1 or be contiguous, and both tables must agree on whatever it is.
    p = tmp_path / "sparse.csv"
    p.write_text(CSV_HEADER + _row(7, 100) + _row(42, 200))
    loaded = ingest_lineups(p, SLATE_ID, conn)
    assert tuple(loaded) == (2, 16)
    assert [r[0] for r in conn.execute("SELECT final_rank FROM lineups ORDER BY 1")] == [7, 42]
    assert conn.execute(
        "SELECT COUNT(*) FROM lineup_players lp WHERE NOT EXISTS ("
        " SELECT 1 FROM lineups l WHERE l.slate_id = lp.slate_id"
        " AND l.final_rank = lp.final_rank)"
    ).fetchone()[0] == 0


def test_ingest_invalid_writes_nothing(tmp_path, conn):
    p = tmp_path / "bad.csv"
    p.write_text(CSV_HEADER + _row(1).replace("Player 3 (103)", "Player 3"))
    with pytest.raises(SlateValidationError) as exc_info:
        ingest_lineups(p, SLATE_ID, conn)
    assert not exc_info.value.report.ok
    assert conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 0


# --- discovery (keep-latest by true generation time) --------------------------

MANIFEST_HEADER = "relabeled_name,original_name,slate_id,generated_at,is_latest_for_slate,n_lineups\n"


@pytest.fixture
def manifest(tmp_path):
    """A slate whose true-latest file has the *lower* HHMMSS suffix.

    This is the real 2026-02-10 case: the later run happened the next calendar
    day, so selecting on the filename suffix picks the wrong file.
    """
    rel = tmp_path / "relabeled"
    rel.mkdir()
    rows = [
        ("ranked-lineups-2026-02-10_182319.csv", "2026-02-10_classic_main", "2026-02-10T18:23:19", 500, False),
        ("ranked-lineups-2026-02-10_145546.csv", "2026-02-10_classic_main", "2026-02-11T14:55:46", 750, True),
        ("ranked-lineups-2026-03-13_205856.csv", "2026-03-13_classic_night", "2026-03-13T20:58:56", 100, True),
    ]
    for name, *_ in rows:
        (rel / name).write_text(CSV_HEADER + CSV_ROWS)
    m = tmp_path / "manifest.csv"
    m.write_text(
        MANIFEST_HEADER
        + "".join(f"{n},{n},{s},{g},{latest},{c}\n" for n, s, g, c, latest in rows)
    )
    return m, rel


def test_load_manifest(manifest):
    files = load_lineups_manifest(*manifest)
    assert len(files) == 3
    assert all(f.path.exists() for f in files)


def test_keep_latest_uses_generated_at_not_hhmmss(manifest):
    latest = latest_lineups_by_slate(*manifest)
    assert set(latest) == {"2026-02-10_classic_main", "2026-03-13_classic_night"}
    picked = latest["2026-02-10_classic_main"]
    # _145546 sorts *below* _182319 by suffix but was generated a day later.
    assert picked.path.name == "ranked-lineups-2026-02-10_145546.csv"
    assert picked.n_lineups == 750


def test_find_lineups_file(manifest):
    assert find_lineups_file("2026-03-13_classic_night", *manifest).n_lineups == 100
    assert find_lineups_file("2026-01-01_classic_main", *manifest) is None


def test_missing_manifest_raises_with_rebuild_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="match_lineups_to_slates"):
        load_lineups_manifest(tmp_path / "nope.csv", tmp_path)


def test_manifest_with_blank_generated_at_raises(manifest):
    # Regression: a blank cell stringifies to "nan", and "n" sorts above every
    # digit — that row would silently win keep-latest for its slate.
    m, rel = manifest
    m.write_text(m.read_text().replace("2026-02-11T14:55:46", ""))
    with pytest.raises(ValueError, match="unusable generated_at"):
        load_lineups_manifest(m, rel)


def test_manifest_with_garbled_generated_at_raises(manifest):
    m, rel = manifest
    m.write_text(m.read_text().replace("2026-02-11T14:55:46", "yesterday"))
    with pytest.raises(ValueError, match="unusable generated_at"):
        load_lineups_manifest(m, rel)


def test_manifest_referencing_absent_file_raises(manifest):
    m, rel = manifest
    (rel / "ranked-lineups-2026-02-10_145546.csv").unlink()
    with pytest.raises(FileNotFoundError, match="missing from"):
        load_lineups_manifest(m, rel)


def test_manifest_missing_column_raises_with_rebuild_hint(manifest):
    # The manifest is a regenerated artifact from a separate script, so a renamed
    # column is real drift. Without the header check, itertuples() would die with
    # "'Pandas' object has no attribute 'generated_at'".
    m, rel = manifest
    m.write_text(m.read_text().replace("generated_at", "generated"))
    with pytest.raises(ValueError, match="missing column"):
        load_lineups_manifest(m, rel)


def test_manifest_name_cannot_escape_relabeled_dir(manifest):
    m, rel = manifest
    (rel / "escaped.csv").write_text(CSV_HEADER + CSV_ROWS)
    m.write_text(m.read_text().replace("ranked-lineups-2026-03-13_205856.csv,", "../escaped.csv,", 1))
    files = load_lineups_manifest(m, rel)
    assert all(f.path.parent == rel for f in files)


def test_keep_latest_warns_when_manifest_flag_disagrees(manifest, caplog):
    m, rel = manifest
    m.write_text(m.read_text().replace("2026-02-11T14:55:46,True", "2026-02-11T14:55:46,False"))
    with caplog.at_level("WARNING"):
        latest = latest_lineups_by_slate(m, rel)
    # generated_at still decides; the disagreement is surfaced, not obeyed.
    assert latest["2026-02-10_classic_main"].path.name == "ranked-lineups-2026-02-10_145546.csv"
    assert "is_latest_for_slate" in caplog.text


def test_keep_latest_silent_when_manifest_agrees(manifest, caplog):
    with caplog.at_level("WARNING"):
        latest_lineups_by_slate(*manifest)
    assert caplog.text == ""
