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

SLATE_TYPES = frozenset({"main", "early", "turbo", "afternoon", "night"})
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


def _parse(filename: str, pattern: re.Pattern, source: str) -> ParsedFilename:
    """
    Parse and validate a filename according to the supplied convention.
    
    Parameters:
        filename (str): Filename to parse.
        pattern (re.Pattern): Regular expression defining the filename convention.
        source (str): Name of the file source used in validation errors.
    
    Returns:
        ParsedFilename: Normalized slate type, validated date, and optional timestamp.
    
    Raises:
        ValueError: If the filename does not match the convention, contains an unknown
            slate type, or contains an invalid date.
    """
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
    try:
        _date.fromisoformat(date)  # reject impossible dates like 2026-13-40
    except ValueError:
        raise ValueError(f"invalid date {date!r} in {source} filename {filename!r}") from None

    ts = m.groupdict().get("ts")
    return ParsedFilename(date=date, slate_type=slate_type, ts=ts)


def parse_salary_filename(filename: str) -> ParsedFilename:
    """
    Parse a salary filename into its date, slate type, and optional timestamp.
    
    Parameters:
        filename (str): Salary filename following the expected naming convention.
    
    Returns:
        ParsedFilename: Parsed and validated filename components.
    
    Raises:
        ValueError: If the filename format, slate type, or date is invalid.
    """
    return _parse(filename, SALARY_RE, "salary")


def parse_projections_filename(filename: str) -> ParsedFilename:
    """
    Parse a projections filename into its date, slate type, and optional timestamp.
    
    Parameters:
        filename (str): Projections CSV filename following the expected naming convention.
    
    Returns:
        ParsedFilename: The normalized date, slate type, and optional timestamp.
    
    Raises:
        ValueError: If the filename format, slate type, or date is invalid.
    """
    return _parse(filename, PROJECTIONS_RE, "projections")


def parse_lineups_filename(filename: str) -> ParsedFilename:
    """
    Parse a ranked lineups filename into its date, slate type, and optional timestamp.
    
    Parameters:
        filename (str): A filename following the ranked lineups naming convention.
    
    Returns:
        ParsedFilename: The normalized date, slate type, and optional timestamp.
    """
    return _parse(filename, LINEUPS_RE, "lineups")


def build_slate_id(date: str, slate_type: str) -> str:
    """
    Constructs the canonical slate identifier for a date and slate type.
    
    Parameters:
    	date (str): A date in `YYYY-MM-DD` format.
    	slate_type (str): The slate type, such as `main`, `early`, or `night`.
    
    Returns:
    	str: An identifier in the format `YYYY-MM-DD_classic_slate_type`.
    
    Raises:
    	ValueError: If the slate type is unknown or the date is invalid.
    """
    slate_type = slate_type.lower()
    if slate_type not in SLATE_TYPES:
        raise ValueError(f"unknown slate type {slate_type!r} (allowed: {sorted(SLATE_TYPES)})")
    _date.fromisoformat(date)
    return f"{date}_{GAME_STYLE}_{slate_type}"
