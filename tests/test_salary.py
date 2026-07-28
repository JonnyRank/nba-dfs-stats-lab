"""Salary source: validate/normalize contract and end-to-end ingest."""

import pytest

from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.salary import (
    ingest_salary,
    normalize_salary,
    read_salary,
    validate_salary,
)
from nba_dfs_stats_lab.ingest.schemas import SlateValidationError

# The real file is fully quoted, so the numeric columns arrive as strings.
CSV_HEADER = '"DFS ID","Name","Position","Team","Opponent","Salary","Actual_FPTs"\n'
CSV_ROWS = (
    '"43090603","Victor Wembanyama","C/UTIL","SAS","OKC","10800","84"\n'
    '"43090605","Shai Gilgeous-Alexander","PG/G/UTIL","OKC","SAS","10000","58.25"\n'
    '"43090637","De\'Aaron Fox","PG/G/UTIL","SAS","OKC","6200","31.5"\n'
)
SLATE_ID = "2026-05-18_classic_main"


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "Main-2026-05-18.csv"
    p.write_text(CSV_HEADER + CSV_ROWS)
    return p


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path / "analytics.db")
    init_db(conn)
    yield conn
    conn.close()


# --- validate -----------------------------------------------------------------


def test_valid_file_passes(csv_path):
    report = validate_salary(read_salary(csv_path))
    assert report.ok
    assert report.row_count == 3
    assert report.errors == []


def test_missing_required_column_fails(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text('"DFS ID","Name","Position","Team","Opponent","Salary"\n"1","A","PG","X","Y","5000"\n')
    report = validate_salary(read_salary(p))
    assert not report.ok
    assert any("Actual_FPTs" in e for e in report.errors)


def test_duplicate_dk_id_fails(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000","20"\n"1","B","SG","X","Y","4000","10"\n')
    report = validate_salary(read_salary(p))
    assert not report.ok
    assert any("duplicate" in e for e in report.errors)


def test_missing_team_warns_but_passes(tmp_path):
    # 36 rows across the real 409 files have no Team/Opponent — nullable.
    p = tmp_path / "noteam.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","","","5000","20"\n')
    report = validate_salary(read_salary(p))
    assert report.ok
    assert any("Team" in w for w in report.warnings)


def test_unplayed_slate_passes(tmp_path):
    # Actual_FPTs is empty until the slate is played — a warning, not an error.
    p = tmp_path / "unplayed.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000",\n')
    report = validate_salary(read_salary(p))
    assert report.ok
    assert any("Actual_FPTs" in w for w in report.warnings)


def test_garbled_salary_fails(tmp_path):
    p = tmp_path / "garbled.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","five thousand","20"\n')
    report = validate_salary(read_salary(p))
    assert not report.ok
    assert any("Salary" in e and "non-numeric" in e for e in report.errors)


# --- normalize ----------------------------------------------------------------


def test_normalize_shape_and_types(csv_path):
    out = normalize_salary(read_salary(csv_path), SLATE_ID)
    assert list(out.columns) == [
        "slate_id", "dk_id", "name", "positions", "team", "opp", "salary", "actual_fpts",
    ]
    assert (out["slate_id"] == SLATE_ID).all()
    assert out["dk_id"].tolist() == [43090603, 43090605, 43090637]
    assert out["salary"].dtype == "Int64"  # quoted strings coerced to ints
    assert out["positions"][1] == "PG/G/UTIL"  # kept raw, not split
    assert out["actual_fpts"][1] == 58.25


# --- ingest (end-to-end) ------------------------------------------------------


def test_ingest_writes_rows(csv_path, conn):
    assert ingest_salary(csv_path, SLATE_ID, conn) == 3
    row = conn.execute(
        "SELECT name, positions, team, salary, actual_fpts FROM slate_players WHERE dk_id = ?",
        (43090603,),
    ).fetchone()
    assert row == ("Victor Wembanyama", "C/UTIL", "SAS", 10800, 84.0)


def test_ingest_reload_idempotent(csv_path, conn):
    ingest_salary(csv_path, SLATE_ID, conn)
    ingest_salary(csv_path, SLATE_ID, conn)
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 3


def test_ingest_unplayed_slate_writes_null_actuals(tmp_path, conn):
    p = tmp_path / "unplayed.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000",\n')
    assert ingest_salary(p, SLATE_ID, conn) == 1
    assert conn.execute("SELECT actual_fpts FROM slate_players").fetchone()[0] is None


def test_ingest_invalid_writes_nothing(tmp_path, conn):
    p = tmp_path / "dup.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000","20"\n"1","B","SG","X","Y","4000","10"\n')
    with pytest.raises(SlateValidationError) as exc_info:
        ingest_salary(p, SLATE_ID, conn)
    assert not exc_info.value.report.ok
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0
