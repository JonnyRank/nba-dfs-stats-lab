"""Salary CSV → `slate_players`.

Mirrors the projections four-method shape. Two source-specific notes:

1. The file is fully quoted, so `DFS ID` and `Salary` can arrive as strings —
   the generic `_coerced` in schemas.py handles that, so the schema declaration
   is all that's needed.

2. `validate_salary` adds an off-slate-game check on top of the generic
   contract, the same way `validate_lineups` adds its slot checks. See
   `check_zero_scored_games` for why a column of valid numbers still needs one.
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

_MAX_REPORTED = 5  # cap examples in messages; the count carries the scale


def read_salary(path: Path) -> pd.DataFrame:
    """Fully-quoted CSV; pandas strips the quotes, validation checks the types."""
    return pd.read_csv(path)


def _zeroed_teams(team: pd.Series, is_zero: pd.Series) -> set[str]:
    """Teams whose every row scored exactly 0.

    NaN never compares equal to 0, so a slate whose results aren't filled in yet
    (all `Actual_FPTs` NULL) is not flagged here — that is the nullable-column
    case, and `validate_frame` already warns about it.
    """
    return {
        name
        for name, idx in team.groupby(team, dropna=True).groups.items()
        if len(idx) > 0 and bool(is_zero.loc[idx].all())
    }


def _sole_opponent(team: pd.Series, opp: pd.Series, name: str) -> str | None:
    """The one team `name` played, or None if that isn't unambiguous.

    Every row for a team on a classic slate carries the same `Opponent`, so more
    than one means the file disagrees with itself. Returning None there keeps the
    team out of the rollup rather than counting its roster into several games.
    """
    others = opp[team == name].dropna().unique()
    return others[0] if len(others) == 1 else None


def check_zero_scored_games(df: pd.DataFrame, report: ValidationReport) -> None:
    """Warn where every rostered player on BOTH sides of a game scored exactly 0.

    Six real games are in this state (2025-10-26 LAC/POR, 2025-10-28 GSW/LAC,
    2026-01-25 DAL/MIL, 2026-02-02 CHA/NOP, and both games of Early-2025-12-07) —
    209 of 51,971 rows. They are games that tipped off outside the slate's
    window (an odd-hour start on a day when everything else began later), so
    their players were never actually rosterable in that contest and no actuals
    were ever recorded against it.

    This needs its own check because the generic contract cannot see it: the
    cells hold `0`, not blank. Across all 409 salary files there is **not one
    blank `Actual_FPTs` cell**, so a null-based check finds nothing, and `0` is a
    present, numeric, in-range value that `validate_frame` is right to accept. It
    then lands in `slate_players.actual_fpts` as a real `0.0`, indistinguishable
    from a genuine DNP.

    The discriminator has to be the whole game, not the value: single zeros are
    normal and common — the unaffected teams on 2026-02-02 carry 6 to 9 each — so
    any rule keyed on the value alone would destroy real data. Both sides of one
    matchup scoring nothing is what cannot happen in a played game.

    A warning, not an error: the rows are otherwise valid and still load, per the
    project's surface-don't-drop rule. Deciding whether to exclude them belongs
    to the ops-reconciliation pass, which can check the box scores.
    """
    if any(c not in df.columns for c in ("Team", "Opponent", "Actual_FPTs")):
        return  # validate_frame already errored on the missing column

    is_zero = pd.to_numeric(df["Actual_FPTs"], errors="coerce") == 0
    team = df["Team"].astype("string").str.strip().replace("", pd.NA)
    opp = df["Opponent"].astype("string").str.strip().replace("", pd.NA)

    zeroed = _zeroed_teams(team, is_zero)
    if not zeroed:
        return

    # Every team on the slate is zeroed — report it once as a slate-level fact
    # rather than emitting a line per game.
    if zeroed == set(team.dropna().unique()):
        report.warn(
            f"every team on the slate scored 0 across all {len(df)} rows — "
            "the slate's results were never recorded"
        )
        return

    # The pairing must reciprocate. `other` merely being zeroed says nothing
    # about who `other` played: if A-B are off-slate and a third zeroed team C
    # names B, "B vs C" would be reported as a game that never existed, and B's
    # roster would be counted into two games at once.
    opponent = {name: _sole_opponent(team, opp, name) for name in zeroed}
    games: dict[tuple[str, str], int] = {}
    for name in zeroed:
        other = opponent[name]
        if other is not None and other in zeroed and opponent.get(other) == name:
            pair = (name, other) if name < other else (other, name)
            games[pair] = games.get(pair, 0) + int((team == name).sum())
    if not games:
        # One side zeroed and the other not: a real (if lopsided) result, or a
        # team whose opponent isn't on this slate. Not the off-slate signature.
        return

    rendered = ", ".join(
        f"{a} vs {b} ({n} players)" for (a, b), n in sorted(games.items())[:_MAX_REPORTED]
    )
    report.warn(
        f"{len(games)} game(s) where every rostered player on both sides scored 0, "
        f"i.e. off-slate games whose actuals were never recorded: {rendered}"
    )


def validate_salary(df: pd.DataFrame) -> ValidationReport:
    report = validate_frame(df, SALARY_SCHEMA)
    check_zero_scored_games(df, report)
    return report


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
