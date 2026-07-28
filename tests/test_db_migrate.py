"""The v1 -> v2 schema step (lineups rank columns INTEGER -> REAL)."""

import re

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
    assert len(actions) == 1 and "not REAL" in actions[0]
    init_db(v1_conn)
    types = _rank_types(v1_conn)
    assert (types["proj_rank"], types["own_rank"], types["geo_rank"]) == ("REAL", "REAL", "REAL")
    assert v1_conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_v1_db_with_rows_refuses_rather_than_dropping(v1_conn):
    v1_conn.execute(
        "INSERT INTO lineups (slate_id, final_rank, proj_rank) VALUES ('s1', 1, 4)"
    )
    v1_conn.commit()
    with pytest.raises(SchemaMigrationError, match=re.escape("Delete data/analytics.db")):
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
    assert len(actions) == 1 and "not REAL" in actions[0]


OWN_RANK_DRIFT_DDL = V1_LINEUPS_DDL.replace("own_rank          REAL", "own_rank          INTEGER") \
    .replace("proj_rank         INTEGER", "proj_rank         REAL") \
    .replace("geo_rank          INTEGER", "geo_rank          REAL")


def test_own_rank_drift_is_detected_even_when_stamped_current(tmp_path):
    # own_rank was already REAL in v1, so this shape never shipped — but the
    # check exists to detect drift from the *live* types, and a hand-edited or
    # half-migrated DB stamped v2 would otherwise keep an INTEGER rank column
    # that init_db's IF NOT EXISTS DDL will never correct.
    conn = get_connection(tmp_path / "drift.db")
    conn.executescript(OWN_RANK_DRIFT_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()

    actions = migrate(conn)
    assert len(actions) == 1 and "own_rank" in actions[0]
    init_db(conn)
    assert _rank_types(conn)["own_rank"] == "REAL"
    conn.close()


def test_own_rank_drift_with_rows_refuses_rather_than_dropping(tmp_path):
    conn = get_connection(tmp_path / "drift_rows.db")
    conn.executescript(OWN_RANK_DRIFT_DDL)
    conn.execute("INSERT INTO lineups (slate_id, final_rank, own_rank) VALUES ('s1', 1, 4)")
    conn.commit()
    with pytest.raises(SchemaMigrationError, match="own_rank"):
        migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 1
    conn.close()


V1_LINEUP_PLAYERS_DDL = """
CREATE TABLE lineup_players (
  slate_id   TEXT    NOT NULL,
  final_rank INTEGER NOT NULL,
  slot       TEXT    NOT NULL,
  dk_id      INTEGER NOT NULL,
  PRIMARY KEY (slate_id, final_rank, slot)
);
"""


def test_stale_lineups_with_populated_lineup_players_refuses(tmp_path):
    # ingest_lineups writes the two tables in separate transactions, so a crash
    # between them leaves slot rows with `lineups` empty. Dropping `lineups`
    # here would strand those rows permanently and stamp the DB v2 carrying
    # orphans, so refuse on either table holding data — not just `lineups`.
    conn = get_connection(tmp_path / "half.db")
    conn.executescript(V1_LINEUPS_DDL + V1_LINEUP_PLAYERS_DDL)
    conn.execute("INSERT INTO lineup_players VALUES ('s1', 1, 'PG', 100)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    with pytest.raises(SchemaMigrationError, match="lineup_players holds 1 row"):
        migrate(conn)
    # Nothing was dropped or stamped on the way out.
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 1
    assert _rank_types(conn)["proj_rank"] == "INTEGER"
    conn.close()


def test_stale_lineups_with_both_tables_empty_still_migrates(tmp_path):
    conn = get_connection(tmp_path / "empty_both.db")
    conn.executescript(V1_LINEUPS_DDL + V1_LINEUP_PLAYERS_DDL)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    assert len(migrate(conn)) == 1
    init_db(conn)
    assert _rank_types(conn)["proj_rank"] == "REAL"
    conn.close()
