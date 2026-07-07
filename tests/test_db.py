"""DB layer: schema creation, connection PRAGMAs, read-only ATTACH, load_slate."""

import sqlite3

import pandas as pd
import pytest

from nba_dfs_stats_lab.db.connection import attach_ops, get_connection, read_only_uri
from nba_dfs_stats_lab.db.schema import SCHEMA_VERSION, TABLES, init_db
from nba_dfs_stats_lab.db.writers import load_slate


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path / "analytics.db")
    init_db(conn)
    yield conn
    conn.close()


# --- schema ---------------------------------------------------------------------


def test_init_db_creates_all_five_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert set(TABLES) <= {r[0] for r in rows}


def test_init_db_creates_exposure_index(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    assert "ix_lineup_players_slate_dk" in {r[0] for r in rows}


def test_init_db_stamps_schema_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_init_db_idempotent(conn):
    init_db(conn)  # second call must not raise


# --- connection -----------------------------------------------------------------


def test_get_connection_creates_parent_dir(tmp_path):
    conn = get_connection(tmp_path / "nested" / "dir" / "analytics.db")
    conn.close()
    assert (tmp_path / "nested" / "dir" / "analytics.db").exists()


def test_get_connection_pragmas(tmp_path):
    conn = get_connection(tmp_path / "analytics.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_read_only_uri_encodes_windows_style_path():
    # Can't test a real G:\ path on Linux, but the encoding rules are pure.
    from pathlib import PureWindowsPath

    uri = read_only_uri(PureWindowsPath(r"G:\My Drive\Documents\backup.db"))
    assert uri == "file:///G:/My%20Drive/Documents/backup.db?mode=ro"


def test_attach_ops_is_read_only(tmp_path, conn):
    # Ops stand-in lives in a directory with a space, exercising the %20 encoding.
    ops_dir = tmp_path / "ops dir"
    ops_dir.mkdir()
    ops_path = ops_dir / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.execute("CREATE TABLE dim_players (PLAYER_ID INTEGER, PLAYER_NAME TEXT)")
    ops.execute("INSERT INTO dim_players VALUES (1, 'Jamal Shead')")
    ops.commit()
    ops.close()

    attach_ops(conn, ops_path)

    # Reads work…
    name = conn.execute("SELECT PLAYER_NAME FROM ops.dim_players").fetchone()[0]
    assert name == "Jamal Shead"

    # …writes must fail: this is the hard constraint on the ops DB.
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO ops.dim_players VALUES (2, 'Nobody')")


def test_attach_ops_rejects_bad_alias(conn, tmp_path):
    with pytest.raises(ValueError, match="alias"):
        attach_ops(conn, tmp_path / "x.db", alias="ops; DROP TABLE projections")


# --- load_slate -------------------------------------------------------------------


def _proj_df(slate_id, dk_ids):
    return pd.DataFrame({
        "slate_id": slate_id,
        "dk_id": dk_ids,
        "minutes": 30.0,
        "fppm": 1.1,
        "proj_pts": 33.0,
        "proj_own": 0.15,
    })


def _count(conn, slate_id):
    return conn.execute(
        "SELECT COUNT(*) FROM projections WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]


def test_load_slate_inserts_and_returns_count(conn):
    n = load_slate(conn, "s1", _proj_df("s1", [1, 2, 3]), "projections")
    assert n == 3
    assert _count(conn, "s1") == 3


def test_load_slate_reload_is_idempotent(conn):
    load_slate(conn, "s1", _proj_df("s1", [1, 2, 3]), "projections")
    load_slate(conn, "s1", _proj_df("s1", [1, 2, 3]), "projections")
    assert _count(conn, "s1") == 3


def test_load_slate_replaces_only_its_slate(conn):
    load_slate(conn, "s1", _proj_df("s1", [1, 2, 3]), "projections")
    load_slate(conn, "s2", _proj_df("s2", [7, 8]), "projections")
    load_slate(conn, "s1", _proj_df("s1", [1, 2]), "projections")  # shrink s1
    assert _count(conn, "s1") == 2
    assert _count(conn, "s2") == 2


def test_load_slate_atomic_on_failure(conn):
    load_slate(conn, "s1", _proj_df("s1", [1, 2, 3]), "projections")
    bad = _proj_df("s1", [10, 10])  # duplicate PK inside the batch -> insert fails
    with pytest.raises(sqlite3.IntegrityError):
        load_slate(conn, "s1", bad, "projections")
    # The failed load rolled back: the original 3 rows survive, not 0 or partial.
    assert _count(conn, "s1") == 3


def test_load_slate_nan_becomes_null(conn):
    df = _proj_df("s1", [1])
    df.loc[0, "proj_own"] = float("nan")
    load_slate(conn, "s1", df, "projections")
    assert conn.execute("SELECT proj_own FROM projections").fetchone()[0] is None


def test_load_slate_rejects_unknown_table(conn):
    with pytest.raises(ValueError, match="unknown slate table"):
        load_slate(conn, "s1", _proj_df("s1", [1]), "dk_crosswalk")


def test_load_slate_rejects_mismatched_slate_id(conn):
    with pytest.raises(ValueError, match="slate_id values other than"):
        load_slate(conn, "s1", _proj_df("s2", [1]), "projections")
