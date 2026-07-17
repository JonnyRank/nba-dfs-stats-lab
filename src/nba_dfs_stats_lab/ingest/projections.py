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
    """
    Read a projections CSV file into a pandas DataFrame.
    
    Parameters:
        path (Path): Path to the projections CSV file.
    
    Returns:
        pd.DataFrame: The CSV contents with pandas-inferred data types.
    """
    return pd.read_csv(path)


def validate_projections(df: pd.DataFrame) -> ValidationReport:
    """
    Validate a projections DataFrame against the projections schema.
    
    Parameters:
        df (pd.DataFrame): The projections data to validate.
    
    Returns:
        ValidationReport: The validation results, including errors and warnings.
    """
    return validate_frame(df, PROJECTIONS_SCHEMA)


def normalize_projections(df: pd.DataFrame, slate_id: str) -> pd.DataFrame:
    """Normalize validated projections data for a specific slate.
    
    Parameters:
        slate_id (str): Identifier of the slate associated with the projections.
    
    Returns:
        pd.DataFrame: Normalized projections data including the slate identifier.
    """
    return normalize_frame(df, PROJECTIONS_SCHEMA, slate_id)


def ingest_projections(path: Path, slate_id: str, conn: sqlite3.Connection) -> int:
    """
    Ingest a projections CSV file into the database for a slate.
    
    Warnings are logged during validation. Invalid data raises
    `SlateValidationError` before any rows are written.
    
    Parameters:
        path (Path): Path to the projections CSV file.
        slate_id (str): Identifier of the slate associated with the projections.
        conn (sqlite3.Connection): Database connection used for loading the data.
    
    Returns:
        int: Number of rows written.
    
    Raises:
        SlateValidationError: If validation errors are found.
    """
    path = Path(path)  # tolerate str paths from callers
    df = read_projections(path)
    report = validate_projections(df)
    for warning in report.warnings:
        logger.warning("%s [%s]: %s", slate_id, path.name, warning)
    if not report.ok:
        raise SlateValidationError(f"projections {path.name} ({slate_id})", report)
    return load_slate(conn, slate_id, normalize_projections(df, slate_id), "projections")
