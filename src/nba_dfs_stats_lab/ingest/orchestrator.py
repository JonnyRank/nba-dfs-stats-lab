"""Cross-source discovery, per-slate orchestration, and the historical backfill.

The three source modules each know how to load *one* file into *one* slate. This
module answers the questions above them:

  - **Which slates exist?** `discover_slates()` scans all three sources and
    unions them by `slate_id`. Salary and projections are discovered by
    filename; lineups come from the reconciled manifest (`latest_lineups_by_slate`),
    never from `LINEUPS_DIR` — see the module docstring in `ingest/lineups.py`.

  - **What loads for one slate?** `ingest_day(date, slate_type, conn)` builds the
    `slate_id` and runs whichever of the three sources that slate actually has.

  - **What happens across all of them?** `backfill()` loops `ingest_slate()` and
    returns a `BackfillSummary` — per-table row totals, per-source status counts,
    and every failure, so one bad file never aborts the run.

**Partial coverage is normal, not an error.** The three sources cover wildly
different numbers of slates: ~409 salary CSVs, 49 projections files, 43 slates
with reconciled lineups. Most slates therefore load salary only. A missing
source is reported as `Status.ABSENT` and contributes nothing to the failure
count; only a file that is present and unusable is a failure.

**`--dry-run` writes nothing.** It runs read + validate for every source and
reports the same summary, so the full backfill's validation outcome is knowable
before a single row is written.
"""

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pandas as pd

from nba_dfs_stats_lab.config import PROJECTIONS_DIR, SALARY_DIR
from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import SchemaMigrationError, init_db
from nba_dfs_stats_lab.ingest.filenames import (
    ParsedFilename,
    build_slate_id,
    parse_projections_filename,
    parse_salary_filename,
    parse_slate_id,
)
from nba_dfs_stats_lab.ingest.lineups import (
    LineupsFile,
    ingest_lineups,
    latest_lineups_by_slate,
    read_lineups,
    validate_lineups,
)
from nba_dfs_stats_lab.ingest.projections import (
    ingest_projections,
    read_projections,
    validate_projections,
)
from nba_dfs_stats_lab.ingest.salary import ingest_salary, read_salary, validate_salary
from nba_dfs_stats_lab.ingest.schemas import (
    LINEUP_SLOTS,
    SlateValidationError,
    ValidationReport,
)

logger = logging.getLogger(__name__)

SOURCES = ("salary", "projections", "lineups")

# Errors a single slate can raise without the run as a whole being broken: a
# malformed CSV (pandas raises ValueError subclasses), an unreadable file, or a
# row SQLite rejects. Anything else propagates and stops the backfill, because
# it means the *code* is wrong rather than one file.
_SLATE_ERRORS = (OSError, ValueError, sqlite3.Error)

_MAX_REPORTED = 5  # cap examples in messages; the count carries the scale

_MANIFEST_HINT = "rebuild it with: uv run python scripts/match_lineups_to_slates.py"


class Status(StrEnum):
    """Per-source outcome for one slate."""

    LOADED = "loaded"  # rows written (real run)
    VALID = "valid"  # read + validated, nothing written (dry run)
    ABSENT = "absent"  # this slate has no file for this source — expected
    INVALID = "invalid"  # file present, validation failed — nothing written
    SKIPPED = "skipped"  # deliberately not attempted (see detail)
    ERROR = "error"  # unreadable file or DB error

    @property
    def is_failure(self) -> bool:
        return self in (Status.INVALID, Status.ERROR)


@dataclass
class SourceOutcome:
    source: str
    status: Status
    rows: dict[str, int] = field(default_factory=dict)  # table -> row count
    detail: str = ""
    warnings: tuple[str, ...] = ()

    def __str__(self) -> str:
        counts = " ".join(f"{table}={n}" for table, n in self.rows.items())
        parts = [p for p in (self.source, str(self.status), counts, self.detail) if p]
        return " ".join(parts)


@dataclass
class SlateResult:
    slate_id: str
    dry_run: bool
    outcomes: list[SourceOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[SourceOutcome]:
        return [o for o in self.outcomes if o.status.is_failure]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def rows(self) -> dict[str, int]:
        """Merged table -> row count across all sources for this slate."""
        merged: dict[str, int] = {}
        for outcome in self.outcomes:
            for table, n in outcome.rows.items():
                merged[table] = merged.get(table, 0) + n
        return merged

    def outcome(self, source: str) -> SourceOutcome | None:
        return next((o for o in self.outcomes if o.source == source), None)

    def summary_line(self) -> str:
        return f"{self.slate_id}  " + "  ".join(str(o) for o in self.outcomes)


@dataclass
class BackfillSummary:
    dry_run: bool
    results: list[SlateResult] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        """table -> rows written (or, in a dry run, rows that would be read)."""
        merged: dict[str, int] = {}
        for result in self.results:
            for table, n in result.rows.items():
                merged[table] = merged.get(table, 0) + n
        return merged

    @property
    def failures(self) -> list[tuple[str, SourceOutcome]]:
        return [(r.slate_id, o) for r in self.results for o in r.failures]

    @property
    def warnings(self) -> list[tuple[str, str, str]]:
        """(slate_id, source, warning) for every validation warning in the run.

        Symmetric with `failures` and populated on both paths, so a write run can
        report the same warning tally a dry run does.
        """
        return [
            (r.slate_id, o.source, w) for r in self.results for o in r.outcomes for w in o.warnings
        ]

    def status_counts(self) -> dict[str, Counter]:
        """source -> Counter of Status over every slate in the run."""
        counts = {source: Counter() for source in SOURCES}
        for result in self.results:
            for outcome in result.outcomes:
                counts[outcome.source][str(outcome.status)] += 1
        return counts


# --- Discovery ----------------------------------------------------------------


@dataclass(frozen=True)
class SlateSources:
    """The files that exist for one slate. Any of the three may be None."""

    slate_id: str
    date: str
    slate_type: str
    salary: Path | None = None
    projections: Path | None = None
    lineups: LineupsFile | None = None

    @property
    def present(self) -> tuple[str, ...]:
        pairs = (("salary", self.salary), ("projections", self.projections), ("lineups", self.lineups))
        return tuple(name for name, value in pairs if value is not None)

    @property
    def coverage(self) -> str:
        """Stable label for grouping, e.g. 'salary+projections'."""
        return "+".join(self.present) or "none"


@dataclass(frozen=True)
class Discovery:
    slates: dict[str, SlateSources]
    # (filename, reason) for every CSV in a source directory that could not be
    # resolved to a slate. Surfaced rather than silently ignored — a file whose
    # name drifted out of convention is invisible data loss otherwise.
    skipped_files: list[tuple[str, str]] = field(default_factory=list)

    def coverage_counts(self) -> Counter:
        return Counter(s.coverage for s in self.slates.values())


def _require_dir(directory: Path, label: str) -> Path:
    """`Path.glob` on a missing directory yields nothing, which would read as
    'this source has no files' — on the G:\\ drive that is far more likely to
    mean the drive isn't mounted. Fail loudly instead."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(
            f"{label} directory not found: {directory}. Check that the G:\\ drive is "
            "mounted and that src/nba_dfs_stats_lab/config.py points at the right path."
        )
    return directory


def _discover_by_filename(
    directory: Path,
    parse: Callable[[str], ParsedFilename],
    label: str,
    skipped: list[tuple[str, str]],
) -> dict[str, Path]:
    """slate_id -> path for every parseable CSV in `directory`."""
    found: dict[str, Path] = {}
    for path in sorted(_require_dir(directory, label).glob("*.csv")):
        try:
            parsed = parse(path.name)
            slate_id = build_slate_id(parsed.date, parsed.slate_type)
        except ValueError as exc:
            skipped.append((path.name, str(exc)))
            continue
        if slate_id in found:
            # Two filenames resolving to one slate would make the load
            # order-dependent (the second silently replaces the first).
            skipped.append((path.name, f"resolves to {slate_id}, already taken by {found[slate_id].name}"))
            continue
        found[slate_id] = path
    return found


def discover_slates(
    salary_dir: Path = SALARY_DIR,
    projections_dir: Path = PROJECTIONS_DIR,
    lineups: dict[str, LineupsFile] | None = None,
) -> Discovery:
    """Union all three sources into one slate_id -> SlateSources map.

    `lineups` defaults to `latest_lineups_by_slate()` — one manifest read for the
    whole backfill, as `find_lineups_file`'s docstring asks for. A missing or
    drifted manifest raises with the rebuild command; callers that report gates
    should catch `(FileNotFoundError, ValueError)` and print it rather than
    letting it traceback.
    """
    skipped: list[tuple[str, str]] = []
    salary = _discover_by_filename(salary_dir, parse_salary_filename, "salary", skipped)
    projections = _discover_by_filename(
        projections_dir, parse_projections_filename, "projections", skipped
    )
    if lineups is None:
        lineups = latest_lineups_by_slate()

    slates: dict[str, SlateSources] = {}
    for slate_id in sorted(set(salary) | set(projections) | set(lineups)):
        try:
            parsed = parse_slate_id(slate_id)
        except ValueError as exc:
            # Only reachable for a manifest-derived id — the filename-derived ones
            # were built by `build_slate_id`. Recorded rather than raised, so one
            # hand-edited manifest row doesn't take the whole inventory with it,
            # and carrying the same rebuild hint every other manifest defect does.
            skipped.append(
                (slate_id, f"{exc} — from the lineups manifest; {_MANIFEST_HINT}")
            )
            continue
        slates[slate_id] = SlateSources(
            slate_id=slate_id,
            date=parsed.date,
            slate_type=parsed.slate_type,
            salary=salary.get(slate_id),
            projections=projections.get(slate_id),
            lineups=lineups.get(slate_id),
        )
    return Discovery(slates=slates, skipped_files=skipped)


# --- Per-slate orchestration --------------------------------------------------


def _dry_run_outcome(
    source: str, report: ValidationReport, table: str, extra_rows: dict[str, int] | None = None
) -> SourceOutcome:
    warnings = tuple(report.warnings)
    if not report.ok:
        return SourceOutcome(
            source,
            Status.INVALID,
            detail="; ".join(report.errors),
            warnings=warnings,
        )
    rows = {table: report.row_count} | (extra_rows or {})
    return SourceOutcome(source, Status.VALID, rows=rows, warnings=warnings)


def _warning_collector() -> tuple[list[str], Callable[[ValidationReport], None]]:
    """Sink for the `on_report` hook the three `ingest_*` functions accept.

    The write path validates inside `ingest_*`, which logs its warnings and
    drops them. Without this the run summary could only report warnings for dry
    runs, so `--all` would say nothing about a file that loaded *with* warnings.
    """
    collected: list[str] = []
    return collected, lambda report: collected.extend(report.warnings)


def _run_flat_source(
    source: str,
    path: Path | None,
    slate_id: str,
    conn: sqlite3.Connection,
    dry_run: bool,
    read: Callable[[Path], pd.DataFrame],
    validate: Callable[[pd.DataFrame], ValidationReport],
    ingest: Callable[..., int],
    table: str,
) -> SourceOutcome:
    """One file -> one table (salary, projections). Never raises for one slate."""
    if path is None:
        return SourceOutcome(source, Status.ABSENT)
    try:
        if dry_run:
            return _dry_run_outcome(source, validate(read(path)), table)
        warnings, on_report = _warning_collector()
        rows = ingest(path, slate_id, conn, on_report=on_report)
        return SourceOutcome(
            source, Status.LOADED, rows={table: rows}, warnings=tuple(warnings)
        )
    except SlateValidationError as exc:
        return SourceOutcome(source, Status.INVALID, detail=str(exc))
    except _SLATE_ERRORS as exc:
        return SourceOutcome(source, Status.ERROR, detail=f"{type(exc).__name__}: {exc}")


def _run_lineups(
    lineups_file: LineupsFile | None,
    slate_id: str,
    conn: sqlite3.Connection,
    dry_run: bool,
    salary_outcome: SourceOutcome,
) -> SourceOutcome:
    """One file -> two tables, and only when the slate has its players loaded.

    `lineup_players` rows name a `dk_id` that must exist in `slate_players` for
    the same slate — the Phase 3 gate checks for zero orphans, and a lineup
    referencing players the DB has never seen is unjoinable. So lineups are
    skipped (not failed) when the slate has no players: either its salary CSV is
    missing, or the salary step just failed. All 43 reconciled lineups slates do
    have a salary CSV, so in practice this only fires on a real defect.

    The write path checks *both* the salary outcome and the row count. Rows
    alone would let a previous run's `slate_players` satisfy the gate while this
    run's salary step was INVALID, loading the new lineups against a stale player
    set and recording nothing about it.
    """
    if lineups_file is None:
        return SourceOutcome("lineups", Status.ABSENT)

    # Checked before the row count, and on both paths: a salary step that failed
    # leaves whatever slate_players held before, which must not stand in for the
    # player set this run was supposed to write.
    if salary_outcome.status.is_failure:
        return SourceOutcome(
            "lineups",
            Status.SKIPPED,
            detail=f"salary is {salary_outcome.status} — this run wrote no slate_players "
            "for the slate",
        )

    if dry_run:
        # No DB state to consult in a dry run, so gate on the salary step: if it
        # wouldn't load, the lineups wouldn't be loadable either.
        if salary_outcome.status not in (Status.VALID, Status.LOADED):
            return SourceOutcome(
                "lineups",
                Status.SKIPPED,
                detail=f"salary is {salary_outcome.status} — lineup_players would have no slate_players to join",
            )
    else:
        n_players = conn.execute(
            "SELECT COUNT(*) FROM slate_players WHERE slate_id = ?", (slate_id,)
        ).fetchone()[0]
        if n_players == 0:
            return SourceOutcome(
                "lineups",
                Status.SKIPPED,
                detail=f"slate_players is empty for this slate (salary is {salary_outcome.status})",
            )

    try:
        if dry_run:
            df = read_lineups(lineups_file.path)
            report = validate_lineups(df)
            outcome = _dry_run_outcome(
                "lineups",
                report,
                "lineups",
                {"lineup_players": report.row_count * len(LINEUP_SLOTS)},
            )
            # The manifest is a separate artifact from the files it indexes; a
            # count mismatch means it drifted out of sync with relabeled/.
            if outcome.status is Status.VALID and report.row_count != lineups_file.n_lineups:
                outcome.warnings += (
                    f"manifest says {lineups_file.n_lineups} lineups, file has {report.row_count}",
                )
            return outcome
        warnings, on_report = _warning_collector()
        load = ingest_lineups(lineups_file.path, slate_id, conn, on_report=on_report)
        if load.lineups != lineups_file.n_lineups:
            warnings.append(
                f"manifest says {lineups_file.n_lineups} lineups, file has {load.lineups}"
            )
        return SourceOutcome(
            "lineups",
            Status.LOADED,
            rows={"lineups": load.lineups, "lineup_players": load.lineup_players},
            warnings=tuple(warnings),
        )
    except SlateValidationError as exc:
        return SourceOutcome("lineups", Status.INVALID, detail=str(exc))
    except _SLATE_ERRORS as exc:
        return SourceOutcome("lineups", Status.ERROR, detail=f"{type(exc).__name__}: {exc}")


def ingest_slate(
    sources: SlateSources, conn: sqlite3.Connection, dry_run: bool = False
) -> SlateResult:
    """Load (or validate) every source this slate has, in dependency order.

    Salary runs first because lineups depend on it. Projections are independent:
    three projections files (2026-02-19/-02-20/-02-22) have no salary CSV at all,
    and refusing to load them would discard data Jonny has rather than surface
    the gap — the run summary reports projections-only slates instead.
    """
    result = SlateResult(slate_id=sources.slate_id, dry_run=dry_run)

    salary = _run_flat_source(
        "salary", sources.salary, sources.slate_id, conn, dry_run,
        read_salary, validate_salary, ingest_salary, "slate_players",
    )
    result.outcomes.append(salary)

    result.outcomes.append(
        _run_flat_source(
            "projections", sources.projections, sources.slate_id, conn, dry_run,
            read_projections, validate_projections, ingest_projections, "projections",
        )
    )
    result.outcomes.append(_run_lineups(sources.lineups, sources.slate_id, conn, dry_run, salary))

    for outcome in result.outcomes:
        for warning in outcome.warnings:
            logger.warning("%s [%s]: %s", sources.slate_id, outcome.source, warning)
    return result


def _sources_for(discovery: Discovery, slate_id: str) -> SlateSources:
    """Discovery's entry for `slate_id`, or an empty one.

    Asking for a slate no source has is a legitimate query, not a failure —
    `ingest_slate` reports every source ABSENT, which is the honest answer.
    Raises `ValueError` if `slate_id` is malformed rather than merely unknown.
    """
    sources = discovery.slates.get(slate_id)
    if sources is not None:
        return sources
    parsed = parse_slate_id(slate_id)
    return SlateSources(slate_id=slate_id, date=parsed.date, slate_type=parsed.slate_type)


def ingest_day(
    date: str,
    slate_type: str,
    conn: sqlite3.Connection,
    dry_run: bool = False,
    discovery: Discovery | None = None,
) -> SlateResult:
    """Ingest one slate identified by date + type.

    Pass `discovery` when looping — building it rescans three directories and
    re-reads the lineups manifest, which is wasted work per slate on a
    Google Drive-backed path.
    """
    slate_id = build_slate_id(date, slate_type)
    if discovery is None:
        discovery = discover_slates()
    return ingest_slate(_sources_for(discovery, slate_id), conn, dry_run=dry_run)


# --- Backfill -----------------------------------------------------------------


def backfill(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    slate_ids: Iterable[str] | None = None,
    discovery: Discovery | None = None,
    on_result: Callable[[SlateResult], None] | None = None,
) -> BackfillSummary:
    """Run every discovered slate (or just `slate_ids`) through `ingest_slate`.

    One slate's bad file never stops the run — it lands in the summary's
    failures. `on_result` is called after each slate for progress output.

    Raises `ValueError` if a `slate_ids` entry is malformed (as opposed to merely
    undiscovered). `main()` validates `--slate` up front so the CLI reports that
    rather than tracebacking.
    """
    if discovery is None:
        discovery = discover_slates()
    wanted = list(discovery.slates) if slate_ids is None else list(slate_ids)

    summary = BackfillSummary(dry_run=dry_run)
    for slate_id in wanted:
        result = ingest_slate(_sources_for(discovery, slate_id), conn, dry_run=dry_run)
        summary.results.append(result)
        if on_result is not None:
            on_result(result)
    return summary


# --- CLI ----------------------------------------------------------------------


def _print_inventory(discovery: Discovery) -> None:
    print(f"{len(discovery.slates)} slate(s) discovered across the three sources.\n")

    print("  by source coverage:")
    for coverage, n in sorted(discovery.coverage_counts().items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4}  {coverage}")

    print("\n  by slate type:")
    by_type = Counter(s.slate_type for s in discovery.slates.values())
    for slate_type, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        with_proj = sum(
            1
            for s in discovery.slates.values()
            if s.slate_type == slate_type and s.projections is not None
        )
        with_lineups = sum(
            1
            for s in discovery.slates.values()
            if s.slate_type == slate_type and s.lineups is not None
        )
        print(f"    {n:>4}  {slate_type:<10} ({with_proj} with projections, {with_lineups} with lineups)")

    if discovery.skipped_files:
        print(f"\n  {len(discovery.skipped_files)} file(s) not resolved to a slate:")
        for name, reason in discovery.skipped_files[:_MAX_REPORTED]:
            print(f"    {name}: {reason}")
        if len(discovery.skipped_files) > _MAX_REPORTED:
            print(f"    ... and {len(discovery.skipped_files) - _MAX_REPORTED} more")
    else:
        print("\n  every CSV in both source directories resolved to a slate.")


def _print_summary(summary: BackfillSummary) -> None:
    mode = "DRY RUN — nothing written" if summary.dry_run else "backfill"
    print(f"\n=== {mode}: {len(summary.results)} slate(s) ===\n")

    counts = summary.status_counts()
    for source in SOURCES:
        rendered = ", ".join(f"{status} {n}" for status, n in sorted(counts[source].items()))
        print(f"  {source:<12} {rendered}")

    label = "rows that would be read" if summary.dry_run else "rows written"
    print(f"\n  {label}:")
    totals = summary.totals
    for table in ("slate_players", "projections", "lineups", "lineup_players"):
        print(f"    {table:<16} {totals.get(table, 0):>8,}")

    warnings = summary.warnings
    if warnings:
        print(f"\n  {len(warnings)} validation warning(s):")
        for slate_id, source, warning in warnings[:_MAX_REPORTED]:
            print(f"    {slate_id} [{source}] {warning}")
        if len(warnings) > _MAX_REPORTED:
            print(f"    ... and {len(warnings) - _MAX_REPORTED} more")
    else:
        print("\n  no validation warnings.")

    failures = summary.failures
    if failures:
        print(f"\n  {len(failures)} failure(s):")
        for slate_id, outcome in failures:
            print(f"    {slate_id} [{outcome.source}] {outcome.status}: {outcome.detail}")
    else:
        print("  no failures.")


def _select_slates(discovery: Discovery, args: argparse.Namespace) -> list[str]:
    """Apply --slate / --date / --limit to the discovered slate ids."""
    selected = list(discovery.slates)
    if args.slate:
        unknown = [s for s in args.slate if s not in discovery.slates]
        if unknown:
            # Still return them: ingest_slate reports every source ABSENT, which
            # is the honest answer to "load this slate" for a slate with no files.
            print(f"  note: {len(unknown)} requested slate(s) have no files at all: {unknown}")
        selected = list(args.slate)
    elif args.date:
        selected = [s for s in selected if discovery.slates[s].date == args.date]
        if not selected:
            print(f"  note: no slate found for date {args.date}")
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nba_dfs_stats_lab.ingest.orchestrator",
        description="Discover, dry-run, and backfill DFS slates into data/analytics.db.",
    )
    parser.add_argument("--list", action="store_true", help="show the discovery inventory and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="read + validate every source; write nothing"
    )
    parser.add_argument(
        "--all", action="store_true", help="required to write every discovered slate"
    )
    parser.add_argument(
        "--slate", action="append", metavar="SLATE_ID", help="restrict to this slate (repeatable)"
    )
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="restrict to every slate on this date")
    parser.add_argument("--limit", type=int, metavar="N", help="stop after N slates")
    parser.add_argument(
        "--per-slate", action="store_true", help="print a line per slate as it is processed"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show validation warnings")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        discovery = discover_slates()
    except (FileNotFoundError, ValueError) as exc:
        # A missing source directory or a drifted lineups manifest — both carry
        # their own fix. Report it, don't traceback.
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 1

    if args.list:
        _print_inventory(discovery)
        return 0

    if discovery.skipped_files:
        # --list is not the only route a person takes to a run, and a file whose
        # name drifted out of convention is invisible data loss if nothing says so.
        print(
            f"warning: {len(discovery.skipped_files)} file(s) not resolved to a slate "
            "and will not be ingested — run --list for detail",
            file=sys.stderr,
        )

    malformed = []
    for slate_id in args.slate or ():
        try:
            parse_slate_id(slate_id)
        except ValueError as exc:
            malformed.append(str(exc))
    if malformed:
        # A typo in a hand-typed slate id is the likeliest way this CLI is driven
        # wrong. Unknown-but-parseable ids are fine (every source reports ABSENT);
        # unparseable ones would only reach parse_slate_id inside backfill().
        print("invalid --slate value(s):", file=sys.stderr)
        for message in malformed:
            print(f"  {message}", file=sys.stderr)
        return 2

    selected = _select_slates(discovery, args)
    # "A restriction implies intent" — but only if it actually restricts.
    # `--limit 500` against 412 slates selects every one of them, so taking the
    # flag's mere presence as intent would let it walk past the --all guard.
    restricted = bool(args.slate or args.date) or (
        args.limit is not None and args.limit < len(discovery.slates)
    )
    if not restricted and not args.all and not args.dry_run:
        print(
            f"Refusing to write all {len(discovery.slates)} slates without --all.\n"
            "  Dry-run first:  --dry-run\n"
            "  Then backfill:  --all",
            file=sys.stderr,
        )
        return 2

    conn = get_connection()
    try:
        try:
            init_db(conn)
        except SchemaMigrationError as exc:
            print(f"schema migration failed: {exc}", file=sys.stderr)
            return 1
        summary = backfill(
            conn,
            dry_run=args.dry_run,
            slate_ids=selected,
            discovery=discovery,
            on_result=(lambda r: print(f"  {r.summary_line()}")) if args.per_slate else None,
        )
    finally:
        conn.close()

    _print_summary(summary)
    return 1 if summary.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
