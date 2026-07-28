"""Analytics DB schema — DDL for the five tables, pinned to docs/ingestion-plan.md.

`init_db` is idempotent (every statement is IF NOT EXISTS), so calling it on an
existing database is a safe no-op.
"""

import sqlite3

# Bump when the DDL below changes shape; stored in PRAGMA user_version so a
# future migration (or a "rebuild from scratch" decision) can detect drift.
SCHEMA_VERSION = 2  # v2: lineups.proj_rank / geo_rank INTEGER -> REAL (see DDL note)

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
  -- All three ranks are average-ranks: ties are split, so fractional values are
  -- normal. The pinned DDL had only own_rank as REAL because the sample slate
  -- happened to show ties only there; across the 43 reconciled slates
  -- Proj_Rank is fractional 2266x and Geo_Rank 3002x. Declaring them INTEGER
  -- would either reject that data at validation or lean on SQLite's loose
  -- affinity to store REALs in an INTEGER column.
  proj_rank         REAL,
  own_rank          REAL,
  geo_rank          REAL,
  PRIMARY KEY (slate_id, final_rank)
);

-- No FK to lineups: load_slate deletes per-table, so ingest must always
-- reload lineups and lineup_players together for a slate (Phase 3 invariant).
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
    """Migrate if needed, then create all tables and indexes; stamp the version.

    `migrate` runs first because every DDL statement below is IF NOT EXISTS: on
    an existing DB `init_db` changes nothing but still stamps `user_version`, so
    stamping before migrating would mark an un-upgraded schema as current.
    """
    migrate(conn)
    conn.executescript(DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


class SchemaMigrationError(Exception):
    """A DB predates SCHEMA_VERSION and can't be upgraded without losing data."""


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing DB up to SCHEMA_VERSION. Returns the actions taken.

    Deliberately minimal — `analytics.db` is a rebuildable artifact, so the
    fallback for anything this can't handle is "delete it and re-ingest".
    `init_db` calls this itself; call it directly only to surface the actions.

    Each step detects drift from the **live schema**, not from `user_version`.
    The stamp is not trustworthy on its own: `init_db`'s DDL is all IF NOT
    EXISTS, so any caller that ran it against an old DB (e.g. verify_gates.py)
    stamped the current version onto a schema it never changed.
    """
    actions: list[str] = []
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not existing:
        return actions  # fresh DB — init_db creates the current schema directly

    # v1 -> v2: lineups.proj_rank / geo_rank were declared INTEGER. SQLite can't
    # change a column type in place, and the table is only written by Phase 3,
    # so recreating it empty is the whole migration.
    if "lineups" in existing:
        col_type = {
            row[1]: (row[2] or "").upper() for row in conn.execute("PRAGMA table_info(lineups)")
        }
        stale = [c for c in ("proj_rank", "geo_rank") if col_type.get(c) != "REAL"]
        if stale:
            n = conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0]
            if n:
                raise SchemaMigrationError(
                    f"lineups holds {n} row(s) written under schema v1, where {stale} "
                    "were INTEGER. Delete data/analytics.db and re-ingest."
                )
            conn.execute("DROP TABLE lineups")
            actions.append("v1->v2: recreated empty `lineups` with REAL rank columns")

    if actions:
        conn.commit()
    return actions
