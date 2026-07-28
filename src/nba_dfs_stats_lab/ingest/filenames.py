"""Filename → (date, slate_type) parsing and slate_id construction.

Three conventions (pinned in docs/ingestion-plan.md):

    salary:       <Type>-<YYYY-MM-DD>.csv                       (Type explicit, incl. Main)
    projections:  NBA-Projs-[<Type>-]<YYYY-MM-DD>.csv           (Type absent => main)
    lineups:      ranked-lineups-[<Type>-]<YYYY-MM-DD>[_<HHMMSS>].csv

The lineups _HHMMSS suffix is a version selector for keep-latest discovery —
it is parsed and returned, but never stored in the DB.
"""

import re
from dataclasses import dataclass
from datetime import date as _date

# `late` was not in the originally pinned set; three real salary files use it
# (Late-2026-01-04/-01-26/-02-07), two of which also have projections and
# lineups. Confirmed with Jonny 2026-07-27 — see CLAUDE.md Status.
SLATE_TYPES = frozenset({"main", "early", "turbo", "afternoon", "night", "late"})
GAME_STYLE = "classic"  # constant this phase; Showdown is a later phase

SALARY_RE = re.compile(r"^(?P<type>[A-Za-z]+)-(?P<date>\d{4}-\d{2}-\d{2})\.csv$")
PROJECTIONS_RE = re.compile(r"^NBA-Projs-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})\.csv$")
LINEUPS_RE = re.compile(
    r"^ranked-lineups-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<ts>\d{6}))?\.csv$"
)


@dataclass(frozen=True)
class ParsedFilename:
    date: str  # "YYYY-MM-DD"
    slate_type: str  # normalized lowercase, always in SLATE_TYPES
    ts: str | None = None  # lineups "_HHMMSS" suffix; None elsewhere


def _check_date(date: str, context: str) -> None:
    """Reject impossible dates like 2026-13-40, naming what they came from.

    `date.fromisoformat` alone raises "Invalid isoformat string: '2026-2-8'",
    which doesn't say which file or slate_id produced it.
    """
    try:
        _date.fromisoformat(date)
    except ValueError:
        raise ValueError(f"invalid date {date!r} in {context}") from None


def _parse(filename: str, pattern: re.Pattern, source: str) -> ParsedFilename:
    m = pattern.match(filename)
    if m is None:
        raise ValueError(f"{source} filename does not match convention: {filename!r}")

    raw_type = m.group("type")
    slate_type = (raw_type or "main").lower()  # absent type group => main
    if slate_type not in SLATE_TYPES:
        raise ValueError(
            f"unknown slate type {raw_type!r} in {source} filename {filename!r} "
            f"(allowed: {sorted(SLATE_TYPES)})"
        )

    date = m.group("date")
    _check_date(date, f"{source} filename {filename!r}")

    ts = m.groupdict().get("ts")
    return ParsedFilename(date=date, slate_type=slate_type, ts=ts)


def parse_salary_filename(filename: str) -> ParsedFilename:
    return _parse(filename, SALARY_RE, "salary")


def parse_projections_filename(filename: str) -> ParsedFilename:
    return _parse(filename, PROJECTIONS_RE, "projections")


def parse_lineups_filename(filename: str) -> ParsedFilename:
    return _parse(filename, LINEUPS_RE, "lineups")


def parse_slate_id(slate_id: str) -> ParsedFilename:
    """Inverse of build_slate_id: '2026-02-28_classic_main' -> date + type."""
    parts = slate_id.split("_")
    if len(parts) != 3 or parts[1] != GAME_STYLE:
        raise ValueError(f"malformed slate_id {slate_id!r} (expected '<date>_{GAME_STYLE}_<type>')")
    date, _, slate_type = parts
    if slate_type not in SLATE_TYPES:
        raise ValueError(f"unknown slate type {slate_type!r} in slate_id {slate_id!r}")
    _check_date(date, f"slate_id {slate_id!r}")
    return ParsedFilename(date=date, slate_type=slate_type)


def salary_filename(date: str, slate_type: str) -> str:
    """Inverse of parse_salary_filename: '2026-05-18', 'main' -> 'Main-2026-05-18.csv'.

    Salary filenames spell the type with a leading capital and always include
    it — even for Main, unlike the other two sources.
    """
    slate_type = slate_type.lower()
    if slate_type not in SLATE_TYPES:
        raise ValueError(f"unknown slate type {slate_type!r} (allowed: {sorted(SLATE_TYPES)})")
    _check_date(date, "salary filename")
    return f"{slate_type.capitalize()}-{date}.csv"


def build_slate_id(date: str, slate_type: str) -> str:
    """'2026-02-28', 'main' -> '2026-02-28_classic_main'."""
    slate_type = slate_type.lower()
    if slate_type not in SLATE_TYPES:
        raise ValueError(f"unknown slate type {slate_type!r} (allowed: {sorted(SLATE_TYPES)})")
    _check_date(date, "slate_id")
    return f"{date}_{GAME_STYLE}_{slate_type}"
