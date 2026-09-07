"""Analytics DB schema — DDL for the six tables, pinned to docs/ingestion-plan.md.

`init_db` is idempotent (every statement is IF NOT EXISTS), so calling it on an
existing database is a safe no-op.
"""

import sqlite3

# Bump when the DDL below changes shape; stored in PRAGMA user_version so a
# future migration (or a "rebuild from scratch" decision) can detect drift.
SCHEMA_VERSION = 3  # v3: fpts_audit added (Phase 6); v2: lineups ranks INTEGER -> REAL

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

-- Phase 6. A full census of slate_players: one row per (slate_id, dk_id),
-- reconciled or not. A census rather than a change log because it makes the
-- audit self-verifying (COUNT(fpts_audit) == COUNT(slate_players) is a gate
-- check) and answers "why is this row 0?" for every row, not just the 1,144
-- that moved.
CREATE TABLE IF NOT EXISTS fpts_audit (
  slate_id   TEXT    NOT NULL,
  dk_id      INTEGER NOT NULL,
  player_id  INTEGER,                       -- NULL when uncrosswalked
  game_date  TEXT,                          -- the ops DATE used; may differ from the slate date
  dk_value   REAL,                          -- the pristine value as ingested from the DK CSV
  ops_value  REAL,                          -- ops DK_POINTS; NULL when no ops row
  delta      REAL,                          -- ops_value - dk_value
  action     TEXT    NOT NULL,              -- corrected | unchanged | no_ops_row | unmapped
  reason     TEXT,                          -- the primary cause; see ingest/reconcile.py
  -- A property of the *game*, not of the row's cause, so it can't live in
  -- `reason`: 209 rows span three actions and three reasons (off_slate,
  -- no_ops_game for 2026-01-25 DAL/MIL, no_crosswalk for Alex Toohey). A
  -- backtest excludes the population with this flag alone.
  off_slate  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (slate_id, dk_id)
);

CREATE INDEX IF NOT EXISTS ix_fpts_audit_action ON fpts_audit (action, reason);
"""

# Checked by `migrate`. Kept beside the DDL so the two can't drift apart —
# `test_db_migrate.py` pins them equal.
FPTS_AUDIT_COLUMNS = frozenset(
    {
        "slate_id",
        "dk_id",
        "player_id",
        "game_date",
        "dk_value",
        "ops_value",
        "delta",
        "action",
        "reason",
        "off_slate",
    }
)

TABLES = (
    "dk_crosswalk",
    "slate_players",
    "projections",
    "lineups",
    "lineup_players",
    "fpts_audit",
)


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
        # All three, not just the two v1 got wrong: the point is to detect drift
        # from the live types, and a hand-edited or partially-migrated DB with
        # own_rank INTEGER would otherwise pass here and then be stamped v2 by
        # init_db, whose IF NOT EXISTS DDL leaves the wrong column in place.
        stale = [c for c in ("proj_rank", "own_rank", "geo_rank") if col_type.get(c) != "REAL"]
        if stale:
            # Both tables, not just `lineups`. ingest_lineups writes them in two
            # separate transactions, so a crash between the calls can leave slot
            # rows behind with `lineups` empty. Dropping `lineups` then would
            # strand those rows permanently — nothing else deletes them, and the
            # DB would be stamped v2 carrying orphans. Refuse instead: this
            # module never destroys data it wasn't asked to, and analytics.db is
            # rebuildable by design.
            held = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("lineups", "lineup_players")
                if table in existing
            }
            populated = {table: n for table, n in held.items() if n}
            if populated:
                detail = ", ".join(f"{table} holds {n} row(s)" for table, n in populated.items())
                raise SchemaMigrationError(
                    f"{detail} under a schema where {stale} are not REAL. "
                    "Delete data/analytics.db and re-ingest."
                )
            conn.execute("DROP TABLE lineups")
            actions.append(f"recreated empty `lineups`; {stale} were not REAL")

    # v2 -> v3: `fpts_audit` is new, so on every real DB today this step finds
    # nothing and init_db's IF NOT EXISTS creates the table. It exists for the
    # DB that already has a table of that name with the wrong shape — a
    # hand-made one, or a future column change — which IF NOT EXISTS would
    # silently leave in place while stamping v3 over it.
    #
    # It refuses rather than dropping, for a sharper reason than the lineups
    # step: `dk_value` is the pristine DK figure, and once `apply_corrections`
    # has overwritten `slate_players.actual_fpts` this table is the *only*
    # record of what DraftKings said. Dropping it populated would destroy data
    # that no re-run can rebuild — only a re-ingest from the source CSVs could.
    if "fpts_audit" in existing:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(fpts_audit)")}
        missing = sorted(FPTS_AUDIT_COLUMNS - columns)
        if missing:
            held = conn.execute("SELECT COUNT(*) FROM fpts_audit").fetchone()[0]
            if held:
                raise SchemaMigrationError(
                    f"fpts_audit holds {held} row(s) but is missing column(s) {missing}. "
                    "Its dk_value column is the only record of the pristine DraftKings "
                    "values once corrections are applied, so this is not dropped "
                    "automatically. Re-ingest the affected slates to restore them, or "
                    "delete data/analytics.db and rebuild."
                )
            conn.execute("DROP TABLE fpts_audit")
            actions.append(f"recreated empty `fpts_audit`; missing column(s) {missing}")

    if actions:
        conn.commit()
    return actions
