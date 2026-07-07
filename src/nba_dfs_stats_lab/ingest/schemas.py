"""Declarative column contracts + the generic validate/normalize machinery.

Each CSV source declares a `SourceSchema` (source header → canonical column →
dtype → required). `validate_frame` and `normalize_frame` are driven entirely
by that declaration, so salary and lineups (Phase 3) reuse this module and only
add their own quirks (id-extraction, melt) on top.

Validation surfaces problems, never fixes them: a failed check lands in
`ValidationReport.errors` and the caller writes nothing. Rows are never
silently dropped.
"""

from dataclasses import dataclass, field

import pandas as pd


@dataclass(frozen=True)
class ColumnSpec:
    source: str  # header in the CSV
    canonical: str  # column name in analytics.db
    dtype: str  # "int" | "float" | "str"
    required: bool = True
    nullable: bool = True  # False => any missing value is an error


@dataclass(frozen=True)
class SourceSchema:
    name: str  # for messages, e.g. "projections"
    table: str  # analytics.db target table
    key: str  # canonical unique-key column within a slate
    columns: tuple[ColumnSpec, ...]
    drop: tuple[str, ...] = ()  # known source columns we intentionally discard


@dataclass
class ValidationReport:
    ok: bool = True
    row_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


class SlateValidationError(Exception):
    """Raised by ingest_* when validation fails; carries the report."""

    def __init__(self, source: str, report: ValidationReport):
        self.report = report
        detail = "; ".join(report.errors) or "unknown validation failure"
        super().__init__(f"{source} validation failed ({len(report.errors)} error(s)): {detail}")


# --- Projections (the reference source) --------------------------------------

PROJECTIONS_SCHEMA = SourceSchema(
    name="projections",
    table="projections",
    key="dk_id",
    columns=(
        # Header is `ID` here vs `DFS ID` in the salary file — both are the DK id.
        ColumnSpec("ID", "dk_id", "int", nullable=False),
        ColumnSpec("Minutes", "minutes", "float"),
        ColumnSpec("FPPM", "fppm", "float"),
        ColumnSpec("Projection", "proj_pts", "float"),
        ColumnSpec("Own_Proj", "proj_own", "float"),
    ),
    drop=("Player", "Team", "Opponent"),  # redundant with slate_players
)


# --- Generic machinery --------------------------------------------------------


def _coerced(series: pd.Series, dtype: str) -> pd.Series:
    """Numeric coercion used by both validate (to detect) and normalize (to apply)."""
    if dtype in ("int", "float"):
        return pd.to_numeric(series, errors="coerce")
    return series


def validate_frame(df: pd.DataFrame, schema: SourceSchema) -> ValidationReport:
    """Check `df` (as read from the CSV) against the contract. Writes nothing."""
    report = ValidationReport(row_count=len(df))

    missing = [c.source for c in schema.columns if c.required and c.source not in df.columns]
    if missing:
        report.error(f"missing required column(s): {missing}")
        return report  # per-column checks below would just cascade

    if len(df) == 0:
        report.error("file has 0 data rows")
        return report

    expected = {c.source for c in schema.columns} | set(schema.drop)
    unexpected = [c for c in df.columns if c not in expected]
    if unexpected:
        report.warn(f"unexpected column(s) ignored: {unexpected}")

    for col in schema.columns:
        if col.source not in df.columns:
            continue  # optional column absent — nothing to check
        raw = df[col.source]
        coerced = _coerced(raw, col.dtype)

        # Values that were present but didn't survive numeric coercion.
        garbled = raw[coerced.isna() & raw.notna()]
        if len(garbled) > 0:
            report.error(
                f"{col.source}: {len(garbled)} non-numeric value(s), "
                f"e.g. {garbled.head(3).tolist()}"
            )

        # Count nulls on the raw column — garbled values already errored above
        # and shouldn't be double-reported as "missing".
        n_null = int(raw.isna().sum())
        if n_null > 0:
            if col.nullable:
                report.warn(f"{col.source}: {n_null} missing value(s)")
            else:
                report.error(f"{col.source}: {n_null} missing value(s) in non-nullable column")

        if col.dtype == "int" and len(garbled) == 0:
            fractional = coerced.dropna() % 1 != 0
            if fractional.any():
                report.error(f"{col.source}: {int(fractional.sum())} non-integer value(s)")

    key_source = next(c.source for c in schema.columns if c.canonical == schema.key)
    dupes = df[key_source][df[key_source].duplicated()]
    if len(dupes) > 0:
        report.error(f"{key_source}: {len(dupes)} duplicate key(s), e.g. {dupes.head(3).tolist()}")

    return report


def normalize_frame(df: pd.DataFrame, schema: SourceSchema, slate_id: str) -> pd.DataFrame:
    """Rename to canonical columns, coerce dtypes, prepend slate_id.

    Assumes `validate_frame` passed — coercion here cannot fail on data that
    validation accepted.
    """
    out = pd.DataFrame({"slate_id": slate_id}, index=df.index)
    for col in schema.columns:
        if col.source not in df.columns:
            continue
        values = _coerced(df[col.source], col.dtype)
        if col.dtype == "int":
            # Nullable Int64: a plain int64 cast would crash on NaN, and Phase 3
            # sources may legitimately have missing optional ints. The writer
            # turns NA into SQL NULL.
            values = values.astype("Int64")
        out[col.canonical] = values
    return out
