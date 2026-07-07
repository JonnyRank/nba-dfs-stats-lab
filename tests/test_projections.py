"""Projections source: validate/normalize contract and end-to-end ingest."""

import pytest

from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.projections import (
    ingest_projections,
    normalize_projections,
    read_projections,
    validate_projections,
)
from nba_dfs_stats_lab.ingest.schemas import SlateValidationError

CSV_HEADER = "ID,Player,Team,Opponent,Minutes,FPPM,Projection,Own_Proj\n"
CSV_ROWS = (
    "42131681,Jamal Shead,HOU,DAL,28.5,0.95,27.1,0.12\n"
    "42131682,Luka Doncic,DAL,HOU,36.0,1.55,55.8,0.45\n"
    "42131683,Amen Thompson,HOU,DAL,33.2,1.21,40.2,0.31\n"
)
SLATE_ID = "2026-02-28_classic_main"


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "NBA-Projs-2026-02-28.csv"
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
    report = validate_projections(read_projections(csv_path))
    assert report.ok
    assert report.row_count == 3
    assert report.errors == []


def test_missing_required_column_fails(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("ID,Minutes,FPPM,Projection\n1,30,1.0,30\n")  # no Own_Proj
    report = validate_projections(read_projections(p))
    assert not report.ok
    assert any("Own_Proj" in e for e in report.errors)


def test_duplicate_dk_id_fails(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(CSV_HEADER + "1,A,X,Y,30,1.0,30,0.1\n1,B,X,Y,20,1.0,20,0.1\n")
    report = validate_projections(read_projections(p))
    assert not report.ok
    assert any("duplicate" in e for e in report.errors)


def test_garbled_numeric_fails(tmp_path):
    p = tmp_path / "garbled.csv"
    p.write_text(CSV_HEADER + "1,A,X,Y,thirty,1.0,30,0.1\n")
    report = validate_projections(read_projections(p))
    assert not report.ok
    assert any("Minutes" in e and "non-numeric" in e for e in report.errors)


def test_missing_dk_id_fails(tmp_path):
    p = tmp_path / "noid.csv"
    p.write_text(CSV_HEADER + ",A,X,Y,30,1.0,30,0.1\n")
    report = validate_projections(read_projections(p))
    assert not report.ok
    assert any("ID" in e and "missing" in e for e in report.errors)


def test_empty_file_fails(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text(CSV_HEADER)
    report = validate_projections(read_projections(p))
    assert not report.ok
    assert any("0 data rows" in e for e in report.errors)


def test_unexpected_column_warns_but_passes(tmp_path):
    p = tmp_path / "extra.csv"
    p.write_text(
        "ID,Player,Team,Opponent,Minutes,FPPM,Projection,Own_Proj,Ceiling\n"
        "1,A,X,Y,30,1.0,30,0.1,60\n"
    )
    report = validate_projections(read_projections(p))
    assert report.ok
    assert any("Ceiling" in w for w in report.warnings)


def test_missing_metric_value_warns_but_passes(tmp_path):
    p = tmp_path / "gap.csv"
    p.write_text(CSV_HEADER + "1,A,X,Y,,1.0,30,0.1\n")
    report = validate_projections(read_projections(p))
    assert report.ok
    assert any("Minutes" in w for w in report.warnings)


# --- normalize ------------------------------------------------------------------


def test_normalize_shape_and_names(csv_path):
    out = normalize_projections(read_projections(csv_path), SLATE_ID)
    assert list(out.columns) == ["slate_id", "dk_id", "minutes", "fppm", "proj_pts", "proj_own"]
    assert (out["slate_id"] == SLATE_ID).all()
    assert out["dk_id"].tolist() == [42131681, 42131682, 42131683]
    assert out["proj_pts"].tolist() == [27.1, 55.8, 40.2]


# --- ingest (end-to-end) ----------------------------------------------------------


def test_ingest_writes_rows(csv_path, conn):
    assert ingest_projections(csv_path, SLATE_ID, conn) == 3
    n, slates = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT slate_id) FROM projections"
    ).fetchone()
    assert (n, slates) == (3, 1)


def test_ingest_reload_idempotent(csv_path, conn):
    ingest_projections(csv_path, SLATE_ID, conn)
    ingest_projections(csv_path, SLATE_ID, conn)
    assert conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 3


def test_ingest_invalid_writes_nothing(tmp_path, conn):
    p = tmp_path / "dup.csv"
    p.write_text(CSV_HEADER + "1,A,X,Y,30,1.0,30,0.1\n1,B,X,Y,20,1.0,20,0.1\n")
    with pytest.raises(SlateValidationError) as exc_info:
        ingest_projections(p, SLATE_ID, conn)
    assert not exc_info.value.report.ok  # the report rides on the exception
    assert conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 0
