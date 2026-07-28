"""Ranked-lineups CSV → `lineups` + `lineup_players`.

Mirrors the projections four-method shape, with two source-specific additions:

1. **Two tables from one file.** The eight slot columns (`PG SG SF PF C G F
   UTIL`) hold cells like ``"Jamal Shead (42131681)"``. The header columns land
   in `lineups`; the slots are melted to long form in `lineup_players`.
   `validate_lineups` proves every slot cell yields exactly one integer id
   *before* the melt, so the melt itself cannot produce a NULL dk_id.

2. **Discovery is manifest-driven, not filename-driven.** The optimizer named
   its output files by run time rather than slate date, so `LINEUPS_DIR` holds
   26 wrong dates and 5 wrong slate types. `scripts/match_lineups_to_slates.py`
   resolves each file to its true slate by DK-ID intersection and writes
   corrected copies to `LINEUPS_RELABELED_DIR` plus a `manifest.csv` carrying
   the true generation time. Keep-latest selects on that `generated_at`, **not**
   on the `_HHMMSS` filename suffix: renaming preserved the suffix but it is a
   generation time, so for 7 slates the suffix now points at the wrong file.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from nba_dfs_stats_lab.config import LINEUPS_MANIFEST, LINEUPS_RELABELED_DIR
from nba_dfs_stats_lab.db.writers import load_slate
from nba_dfs_stats_lab.ingest.schemas import (
    LINEUP_SLOTS,
    LINEUPS_SCHEMA,
    SlateValidationError,
    ValidationReport,
    normalize_frame,
    validate_frame,
)

logger = logging.getLogger(__name__)

DK_ID_RE = re.compile(r"\((\d+)\)")

_MAX_REPORTED = 5  # cap examples in error messages; the count carries the scale


class LineupsLoad(NamedTuple):
    """Rows written to each table by one `ingest_lineups` call."""

    lineups: int
    lineup_players: int


# --- Slot cell parsing --------------------------------------------------------


def extract_dk_id(cell: object) -> int | None:
    """``"Jamal Shead (42131681)"`` -> 42131681; None if the cell is unusable.

    Returns None rather than raising so validation can collect *every* bad cell
    in one pass. A cell with no id, or with more than one parenthesised number,
    is ambiguous and treated as unusable — names with apostrophes or hyphens
    parse fine, but a name that itself contained "(123)" would not, and that is
    a case we want surfaced rather than guessed at.
    """
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return None
    found = DK_ID_RE.findall(str(cell))
    return int(found[0]) if len(found) == 1 else None


# --- Four-method shape --------------------------------------------------------


def read_lineups(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)


def validate_lineups(df: pd.DataFrame) -> ValidationReport:
    """Generic header-column contract, plus the slot checks the melt depends on."""
    report = validate_frame(df, LINEUPS_SCHEMA)

    missing_slots = [s for s in LINEUP_SLOTS if s not in df.columns]
    if missing_slots:
        report.error(f"missing slot column(s): {missing_slots}")
        return report
    if len(df) == 0:
        return report  # already errored in validate_frame

    ids = df[list(LINEUP_SLOTS)].map(extract_dk_id)

    # 1. Every slot cell must yield exactly one id.
    bad_mask = ids.isna()
    n_bad = int(bad_mask.to_numpy().sum())
    if n_bad:
        examples = [
            f"row {df.index[r]} {LINEUP_SLOTS[c]}={df[LINEUP_SLOTS[c]].iloc[r]!r}"
            for r, c in list(zip(*bad_mask.to_numpy().nonzero()))[:_MAX_REPORTED]
        ]
        report.error(f"{n_bad} slot cell(s) with no single dk_id, e.g. {examples}")

    # 2. A lineup must roster 8 distinct players — a repeat means a bad melt
    #    (the PK is (slate_id, final_rank, slot), so it would insert silently).
    if not n_bad:
        n_distinct = ids.nunique(axis=1)
        dupes = n_distinct[n_distinct != len(LINEUP_SLOTS)]
        if len(dupes) > 0:
            ranks = df.loc[dupes.index, "Final_Rank"].head(_MAX_REPORTED).tolist()
            report.error(
                f"{len(dupes)} lineup(s) with a repeated player, "
                f"e.g. Final_Rank {ranks}"
            )

    return report


def normalize_lineups(df: pd.DataFrame, slate_id: str) -> pd.DataFrame:
    """Header grain → `lineups` columns."""
    return normalize_frame(df, LINEUPS_SCHEMA, slate_id)


def normalize_lineup_players(df: pd.DataFrame, slate_id: str) -> pd.DataFrame:
    """Melt the 8 slot columns to long form → `lineup_players` columns.

    Assumes `validate_lineups` passed, so every cell yields an id and dk_id is
    never NA here.
    """
    long = df.melt(
        id_vars=["Final_Rank"],
        value_vars=list(LINEUP_SLOTS),
        var_name="slot",
        value_name="_cell",
    )
    out = pd.DataFrame(
        {
            "slate_id": slate_id,
            "final_rank": pd.to_numeric(long["Final_Rank"]).astype("Int64"),
            "slot": long["slot"].astype("string"),
            "dk_id": long["_cell"].map(extract_dk_id).astype("Int64"),
        }
    )
    # Sort into (lineup, roster-slot) order so the table reads naturally; melt
    # emits column-major, which would interleave all lineups per slot.
    order = pd.Categorical(long["slot"], categories=LINEUP_SLOTS, ordered=True)
    return (
        out.assign(_slot_order=order)
        .sort_values(["final_rank", "_slot_order"])
        .drop(columns="_slot_order")
        .reset_index(drop=True)
    )


def ingest_lineups(path: Path, slate_id: str, conn: sqlite3.Connection) -> LineupsLoad:
    """read → validate → (stop if errors) → normalize → two `load_slate` calls.

    The two loads are separate transactions (that is `load_slate`'s contract),
    so a failure between them can leave `lineups` refreshed while
    `lineup_players` still holds the previous load. Both writes are
    delete-then-insert keyed on slate_id, so simply re-running the slate repairs
    it — nothing is duplicated or half-inserted within a table.
    """
    path = Path(path)
    df = read_lineups(path)
    report = validate_lineups(df)
    for warning in report.warnings:
        logger.warning("%s [%s]: %s", slate_id, path.name, warning)
    if not report.ok:
        raise SlateValidationError(f"lineups {path.name} ({slate_id})", report)

    n_lineups = load_slate(conn, slate_id, normalize_lineups(df, slate_id), "lineups")
    n_players = load_slate(
        conn, slate_id, normalize_lineup_players(df, slate_id), "lineup_players"
    )
    return LineupsLoad(lineups=n_lineups, lineup_players=n_players)


# --- Discovery (keep-latest by true generation time) ---------------------------


@dataclass(frozen=True)
class LineupsFile:
    slate_id: str
    path: Path
    generated_at: str  # ISO-8601, from the manifest — the keep-latest key
    n_lineups: int


def load_lineups_manifest(
    manifest_path: Path = LINEUPS_MANIFEST,
    relabeled_dir: Path = LINEUPS_RELABELED_DIR,
) -> list[LineupsFile]:
    """Every reconciled lineups file, one entry per file (not per slate)."""
    manifest_path, relabeled_dir = Path(manifest_path), Path(relabeled_dir)
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"lineups manifest not found at {manifest_path}. It is a gitignored "
            "artifact — rebuild it with:\n"
            "    uv run python scripts/match_lineups_to_slates.py"
        )
    df = pd.read_csv(manifest_path)
    files = [
        LineupsFile(
            slate_id=row.slate_id,
            path=relabeled_dir / row.relabeled_name,
            generated_at=str(row.generated_at),
            n_lineups=int(row.n_lineups),
        )
        for row in df.itertuples()
    ]
    absent = [f.path.name for f in files if not f.path.exists()]
    if absent:
        raise FileNotFoundError(
            f"{len(absent)} file(s) listed in {manifest_path.name} are missing from "
            f"{relabeled_dir}, e.g. {absent[:_MAX_REPORTED]}. Re-run "
            "scripts/match_lineups_to_slates.py."
        )
    return files


def latest_lineups_by_slate(
    manifest_path: Path = LINEUPS_MANIFEST,
    relabeled_dir: Path = LINEUPS_RELABELED_DIR,
) -> dict[str, LineupsFile]:
    """slate_id → the most recently generated lineups file for that slate.

    Selects on `generated_at`, reproducing the manifest's `is_latest_for_slate`.
    Ties break on filename so the choice is deterministic.
    """
    latest: dict[str, LineupsFile] = {}
    for f in load_lineups_manifest(manifest_path, relabeled_dir):
        current = latest.get(f.slate_id)
        if current is None or (f.generated_at, f.path.name) > (
            current.generated_at,
            current.path.name,
        ):
            latest[f.slate_id] = f
    return latest


def find_lineups_file(
    slate_id: str,
    manifest_path: Path = LINEUPS_MANIFEST,
    relabeled_dir: Path = LINEUPS_RELABELED_DIR,
) -> LineupsFile | None:
    """The keep-latest lineups file for one slate, or None if it has none."""
    return latest_lineups_by_slate(manifest_path, relabeled_dir).get(slate_id)
