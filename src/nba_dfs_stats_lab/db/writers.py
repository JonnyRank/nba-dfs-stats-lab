"""Idempotent slate-grained writes: delete-then-insert in one transaction."""

import re
import sqlite3

import pandas as pd

# Only the slate-keyed tables are valid targets; dk_crosswalk has no slate_id
# and is written by its own phase.
SLATE_TABLES = frozenset({"slate_players", "projections", "lineups", "lineup_players"})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_slate(conn: sqlite3.Connection, slate_id: str, df: pd.DataFrame, table: str) -> int:
    """Replace `table`'s rows for `slate_id` with `df`, atomically.

    DELETE + INSERT run inside one transaction (`with conn`), so a failed load
    rolls back to the previous state — re-running never duplicates or
    half-writes. Returns the number of rows inserted.
    """
    if table not in SLATE_TABLES:
        raise ValueError(f"unknown slate table: {table!r} (expected one of {sorted(SLATE_TABLES)})")
    if len(df) == 0:
        # A vacuously-true slate_id check would let an empty frame silently
        # clear the slate; there is no legitimate "replace with nothing" here.
        raise ValueError(f"df is empty — refusing to clear {table}.{slate_id!r}")

    columns = list(df.columns)
    if "slate_id" not in columns:
        raise ValueError("df must carry a slate_id column (normalize before loading)")
    bad = [c for c in columns if not _IDENTIFIER.match(c)]
    if bad:
        raise ValueError(f"invalid column names: {bad}")
    if not (df["slate_id"] == slate_id).all():
        raise ValueError(f"df contains slate_id values other than {slate_id!r}")

    # astype(object) + None-for-NaN so sqlite3 receives plain Python scalars
    # (it can't bind numpy types) and missing values land as NULL.
    rows = df.astype(object).where(df.notna(), None).itertuples(index=False, name=None)

    col_list = ", ".join(columns)
    placeholders = ", ".join("?" * len(columns))
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE slate_id = ?", (slate_id,))
        conn.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", rows)
    return len(df)
