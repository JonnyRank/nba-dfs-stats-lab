"""Analytics DB schema — DDL for the five tables, pinned to docs/ingestion-plan.md.

`init_db` is idempotent (every statement is IF NOT EXISTS), so calling it on an
existing database is a safe no-op.
"""

import sqlite3

# Bump when the DDL below changes shape; stored in PRAGMA user_version so a
# future migration (or a "rebuild from scratch" decision) can detect drift.
SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS dk_crosswalk (
  dk_id        INTEGER PRIMARY KEY,
  player_id    INTEGER NOT NULL,            -- ops dim_players.PLAYER_ID
  display_name TEXT
);

CREATE TABLE IF NOT EXISTS slate_players (
  slate_id    TEXT    NOT NULL,
  dk_id       INTEGER NOT NULL,
  name        TEXT,
  positions   TEXT,                         -- raw, e.g. "PG/G/UTIL"
  team        TEXT,
  opp         TEXT,
  salary      INTEGER,
  actual_fpts REAL,                         -- nullable until slate is played
  PRIMARY KEY (slate_id, dk_id)
);

CREATE TABLE IF NOT EXISTS projections (
  slate_id  TEXT    NOT NULL,
  dk_id     INTEGER NOT NULL,
  minutes   REAL,
  fppm      REAL,
  proj_pts  REAL,
  proj_own  REAL,
  PRIMARY KEY (slate_id, dk_id)
);

CREATE TABLE IF NOT EXISTS lineups (
  slate_id          TEXT    NOT NULL,
  final_rank        INTEGER NOT NULL,
  lineup_score      REAL,
  total_projection  REAL,
  total_ownership   REAL,
  geomean_ownership REAL,
  proj_rank         INTEGER,
  own_rank          REAL,                   -- read as REAL; sample had fractional values
  geo_rank          INTEGER,
  PRIMARY KEY (slate_id, final_rank)
);

CREATE TABLE IF NOT EXISTS lineup_players (
  slate_id   TEXT    NOT NULL,
  final_rank INTEGER NOT NULL,
  slot       TEXT    NOT NULL,              -- PG SG SF PF C G F UTIL
  dk_id      INTEGER NOT NULL,
  PRIMARY KEY (slate_id, final_rank, slot)
);

CREATE INDEX IF NOT EXISTS ix_lineup_players_slate_dk
  ON lineup_players (slate_id, dk_id);      -- exposure rollups
"""

TABLES = ("dk_crosswalk", "slate_players", "projections", "lineups", "lineup_players")


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes; stamp the schema version."""
    conn.executescript(DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
