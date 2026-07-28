"""The v1 -> v2 schema step (lineups rank columns INTEGER -> REAL)."""

import pytest

from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import (
    SCHEMA_VERSION,
    SchemaMigrationError,
    init_db,
    migrate,
)

V1_LINEUPS_DDL = """
CREATE TABLE lineups (
  slate_id          TEXT    NOT NULL,
  final_rank        INTEGER NOT NULL,
  lineup_score      REAL,
  total_projection  REAL,
  total_ownership   REAL,
  geomean_ownership REAL,
  proj_rank         INTEGER,
  own_rank          REAL,
  geo_rank          INTEGER,
  PRIMARY KEY (slate_id, final_rank)
);
"""


@pytest.fixture
def v1_conn(tmp_path):
    conn = get_connection(tmp_path / "v1.db")
    conn.executescript(V1_LINEUPS_DDL)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    yield conn
    conn.close()


def _rank_types(conn):
    return {r[1]: r[2] for r in conn.execute("PRAGMA table_info(lineups)")}


def test_fresh_db_needs_no_migration(tmp_path):
    conn = get_connection(tmp_path / "fresh.db")
    assert migrate(conn) == []
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert _rank_types(conn)["proj_rank"] == "REAL"
    conn.close()


def test_empty_v1_db_migrates(v1_conn):
    assert _rank_types(v1_conn)["proj_rank"] == "INTEGER"
    actions = migrate(v1_conn)
    assert len(actions) == 1 and "v1->v2" in actions[0]
    init_db(v1_conn)
    types = _rank_types(v1_conn)
    assert (types["proj_rank"], types["own_rank"], types["geo_rank"]) == ("REAL", "REAL", "REAL")
    assert v1_conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_v1_db_with_rows_refuses_rather_than_dropping(v1_conn):
    v1_conn.execute(
        "INSERT INTO lineups (slate_id, final_rank, proj_rank) VALUES ('s1', 1, 4)"
    )
    v1_conn.commit()
    with pytest.raises(SchemaMigrationError, match="Delete data/analytics.db"):
        migrate(v1_conn)
    assert v1_conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 1


def test_migrate_is_idempotent(v1_conn):
    migrate(v1_conn)
    init_db(v1_conn)
    assert migrate(v1_conn) == []


def test_init_db_migrates_rather_than_stamping_over_v1(v1_conn):
    # Regression: init_db's DDL is all IF NOT EXISTS, so on a v1 DB it changes
    # nothing — but it stamps user_version. Stamping before migrating would mark
    # an un-upgraded schema as current and disable migrate() forever.
    init_db(v1_conn)
    assert _rank_types(v1_conn)["proj_rank"] == "REAL"
    assert v1_conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_migrate_detects_drift_even_when_version_says_current(v1_conn):
    # A v1 schema mis-stamped as v2 (what verify_gates.py's init_db used to do)
    # must still be detected — migrate reads the live column types, not the stamp.
    v1_conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    v1_conn.commit()
    actions = migrate(v1_conn)
    assert len(actions) == 1 and "v1->v2" in actions[0]
