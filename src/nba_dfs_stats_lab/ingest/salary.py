"""Salary CSV → `slate_players`.

Mirrors the projections four-method shape. The only source-specific quirk is
that the file is fully quoted, so `DFS ID` and `Salary` can arrive as strings —
the generic `_coerced` in schemas.py handles that, so nothing extra is needed
here beyond the schema declaration.
"""

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from nba_dfs_stats_lab.db.writers import load_slate
from nba_dfs_stats_lab.ingest.schemas import (
    SALARY_SCHEMA,
    SlateValidationError,
    ValidationReport,
    normalize_frame,
    validate_frame,
)

logger = logging.getLogger(__name__)


def read_salary(path: Path) -> pd.DataFrame:
    """Fully-quoted CSV; pandas strips the quotes, validation checks the types."""
    return pd.read_csv(path)


def validate_salary(df: pd.DataFrame) -> ValidationReport:
    return validate_frame(df, SALARY_SCHEMA)


def normalize_salary(df: pd.DataFrame, slate_id: str) -> pd.DataFrame:
    return normalize_frame(df, SALARY_SCHEMA, slate_id)


def ingest_salary(
    path: Path,
    slate_id: str,
    conn: sqlite3.Connection,
    on_report: Callable[[ValidationReport], None] | None = None,
) -> int:
    """read → validate → (stop if errors) → normalize → load_slate.

    Returns rows written. On validation errors nothing is written and
    SlateValidationError (carrying the full report) is raised; warnings are
    logged but don't block.

    `on_report` receives the `ValidationReport` before the write. Logging alone
    discards the warnings, so a caller that wants to *count* them (the
    orchestrator's run summary) has no other way to see them on this path —
    the exception only carries the report when validation fails.
    """
    path = Path(path)
    df = read_salary(path)
    report = validate_salary(df)
    if on_report is not None:
        on_report(report)
    for warning in report.warnings:
        logger.warning("%s [%s]: %s", slate_id, path.name, warning)
    if not report.ok:
        raise SlateValidationError(f"salary {path.name} ({slate_id})", report)
    return load_slate(conn, slate_id, normalize_salary(df, slate_id), "slate_players")
