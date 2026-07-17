"""Projections CSV → `projections` table.

This is the reference source: the four-method shape below
(read → validate → normalize → ingest) is the template salary and lineups copy
in Phase 3.
"""

import logging
import sqlite3
from pathlib import Path

import pandas as pd

from nba_dfs_stats_lab.db.writers import load_slate
from nba_dfs_stats_lab.ingest.schemas import (
    PROJECTIONS_SCHEMA,
    SlateValidationError,
    ValidationReport,
    normalize_frame,
    validate_frame,
)

logger = logging.getLogger(__name__)


def read_projections(path: Path) -> pd.DataFrame:
    """Plain unquoted CSV; pandas infers dtypes, validation checks them."""
    return pd.read_csv(path)


def validate_projections(df: pd.DataFrame) -> ValidationReport:
    return validate_frame(df, PROJECTIONS_SCHEMA)


def normalize_projections(df: pd.DataFrame, slate_id: str) -> pd.DataFrame:
    return normalize_frame(df, PROJECTIONS_SCHEMA, slate_id)


def ingest_projections(path: Path, slate_id: str, conn: sqlite3.Connection) -> int:
    """read → validate → (stop if errors) → normalize → load_slate.

    Returns rows written. On validation errors nothing is written and
    SlateValidationError (carrying the full report) is raised — the orchestrator
    catches it per-slate; warnings are logged but don't block.
    """
    path = Path(path)  # tolerate str paths from callers
    df = read_projections(path)
    report = validate_projections(df)
    for warning in report.warnings:
        logger.warning("%s [%s]: %s", slate_id, path.name, warning)
    if not report.ok:
        raise SlateValidationError(f"projections {path.name} ({slate_id})", report)
    return load_slate(conn, slate_id, normalize_projections(df, slate_id), "projections")
