"""Local gate verification for Phase 4 (orchestrator + backfill) — run on the Windows machine.

Usage:
    uv run python scripts/verify_phase4.py              # discovery + dry-run gate (writes nothing)
    uv run python scripts/verify_phase4.py --backfill   # the above, then the full backfill
    uv run python scripts/verify_phase4.py --sample 8   # dry-run more slates

Runs the Phase 4 gate from docs/ingestion-plan.md in two parts.

**Part 1 — discovery + dry-run (default, writes nothing).** This is the gate
Jonny must see *before* anything is written:

  - discovery resolves every CSV in both source directories to a slate
  - the coverage inventory matches the known file counts (409 salary,
    49 projections, 43 lineups slates)
  - a sample spanning every coverage combination dry-runs clean
  - the DB's row counts are byte-identical before and after the dry run

**Part 2 — backfill (`--backfill`).** Loads every discovered slate, then re-checks
the Phase 3 integrity invariants across the *whole* DB rather than one slate:

  - per-table row counts and slates-loaded counts
  - 8 players per lineup, everywhere
  - zero rostered players missing from `slate_players`
  - every lineup header has its slot rows, and no slot rows lack a header
  - re-running one slate changes no row count (idempotency at orchestrator level)

**Precondition for `--backfill`:** the check that every table holds exactly what
the backfill wrote assumes the DB contains only slates discovery still finds.
`data/analytics.db` is rebuildable — delete it first if it holds slates from an
older manifest, or that check reports them as stale rows.

Exit code 0 = all gates passed; 1 = something failed (details printed).
"""

import argparse
import sqlite3

from nba_dfs_stats_lab.config import PROJECTIONS_DIR, SALARY_DIR
from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import SchemaMigrationError, init_db, migrate
from nba_dfs_stats_lab.ingest.orchestrator import (
    SOURCES,
    Discovery,
    Status,
    backfill,
    discover_slates,
    ingest_day,
)

_failures: list[str] = []

_SLATE_TABLES = ("slate_players", "projections", "lineups", "lineup_players")

# Counts established by probing the real directories (CLAUDE.md Status). They are
# a tripwire, not a spec: if Jonny adds files these move, and the check reports
# the drift rather than failing the gate.
_EXPECTED = {"salary": 409, "projections": 49, "lineups": 43}


def check(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    if not ok:
        _failures.append(label)
    return ok


def note(label: str, detail: str) -> None:
    print(f"  [note] {label} — {detail}")


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in _SLATE_TABLES
    }


def print_status_counts(summary, header: str) -> None:
    counts = summary.status_counts()
    print(f"\n  {header}:")
    for source in SOURCES:
        print(f"    {source:<12} " + ", ".join(f"{k} {v}" for k, v in sorted(counts[source].items())))


# --- Part 1: discovery --------------------------------------------------------


def discovery_gate(discovery: Discovery) -> None:
    print("\nDiscovery")
    counts = {
        "salary": sum(1 for s in discovery.slates.values() if s.salary is not None),
        "projections": sum(1 for s in discovery.slates.values() if s.projections is not None),
        "lineups": sum(1 for s in discovery.slates.values() if s.lineups is not None),
    }

    check(
        "every source CSV resolved to a slate",
        not discovery.skipped_files,
        f"{len(discovery.skipped_files)} unresolved"
        + (f", e.g. {discovery.skipped_files[:3]}" if discovery.skipped_files else ""),
    )
    check(
        "all three sources discovered",
        all(counts[source] > 0 for source in SOURCES),
        ", ".join(f"{source} {counts[source]}" for source in SOURCES),
    )
    for source, expected in _EXPECTED.items():
        if counts[source] != expected:
            note(
                f"{source} slate count moved",
                f"expected {expected} (CLAUDE.md), found {counts[source]} — "
                "fine if files were added; update the Status if so",
            )

    # A lineups or projections file with no salary CSV is the interesting gap:
    # lineups get skipped (they'd orphan), projections load standalone.
    lineups_no_salary = sorted(
        s.slate_id
        for s in discovery.slates.values()
        if s.lineups is not None and s.salary is None
    )
    check(
        "no lineups slate is missing its salary CSV",
        not lineups_no_salary,
        f"{len(lineups_no_salary)} would be skipped: {lineups_no_salary[:5]}",
    )
    proj_no_salary = sorted(
        s.slate_id
        for s in discovery.slates.values()
        if s.projections is not None and s.salary is None
    )
    if proj_no_salary:
        note(
            "projections with no salary CSV",
            f"{len(proj_no_salary)} slate(s) will load projections only: {proj_no_salary}",
        )

    print("\n  coverage:")
    for coverage, n in sorted(discovery.coverage_counts().items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4}  {coverage}")

    print("\n  slate types:")
    for slate_type in sorted({s.slate_type for s in discovery.slates.values()}):
        of_type = [s for s in discovery.slates.values() if s.slate_type == slate_type]
        n_proj = sum(1 for s in of_type if s.projections is not None)
        n_lu = sum(1 for s in of_type if s.lineups is not None)
        print(f"    {len(of_type):>4}  {slate_type:<10} ({n_proj} projections, {n_lu} lineups)")


def sample_slates(discovery: Discovery, per_coverage: int) -> list[str]:
    """Up to `per_coverage` slates from each distinct coverage combination.

    Sampling by coverage rather than by date is what makes the dry run
    representative: picking the newest N would exercise only whichever
    combination happens to be recent.
    """
    by_coverage: dict[str, list[str]] = {}
    for slate_id, sources in discovery.slates.items():
        by_coverage.setdefault(sources.coverage, []).append(slate_id)
    picked: list[str] = []
    for coverage in sorted(by_coverage):
        picked.extend(sorted(by_coverage[coverage])[:per_coverage])
    return sorted(picked)


def dry_run_gate(conn: sqlite3.Connection, discovery: Discovery, per_coverage: int) -> None:
    picked = sample_slates(discovery, per_coverage)
    print(f"\nDry run — {len(picked)} slate(s), one or more per coverage combination")

    before = row_counts(conn)
    summary = backfill(conn, dry_run=True, slate_ids=picked, discovery=discovery)
    after = row_counts(conn)

    for result in summary.results:
        print(f"    {result.summary_line()}")

    check(
        "dry run wrote nothing",
        before == after,
        f"row counts unchanged: {before}" if before == after else f"{before} -> {after}",
    )
    check(
        "every sampled slate validates",
        not summary.failures,
        "no failures"
        if not summary.failures
        else f"{len(summary.failures)}: {summary.failures[:3]}",
    )
    # A sampled slate where every source is ABSENT would make the dry run
    # vacuous — it would "pass" without reading a single file.
    read_any = sum(
        1
        for r in summary.results
        if any(o.status is Status.VALID for o in r.outcomes)
    )
    check(
        "the sample actually read files",
        read_any == len(summary.results),
        f"{read_any}/{len(summary.results)} slates validated at least one source",
    )

    print_status_counts(summary, "per-source status across the sample")
    print("\n  rows that would be read:")
    for table, n in sorted(summary.totals.items()):
        print(f"    {table:<16} {n:>8,}")


# --- Part 2: backfill ---------------------------------------------------------


def backfill_gate(conn: sqlite3.Connection, discovery: Discovery) -> None:
    n = len(discovery.slates)
    print(f"\nBackfill — {n} slate(s). This reads every file on G:\\ and will take a while.")

    done = 0

    def progress(result) -> None:
        nonlocal done
        done += 1
        if done % 25 == 0 or done == n:
            print(f"    {done}/{n} slates")
        if result.failures:
            print(f"    ! {result.summary_line()}")

    summary = backfill(conn, discovery=discovery, on_result=progress)

    check(
        "backfill completed with no failures",
        not summary.failures,
        "no failures"
        if not summary.failures
        else f"{len(summary.failures)} failure(s), first: {summary.failures[0]}",
    )

    warnings = summary.warnings
    if warnings:
        print(f"\n  {len(warnings)} validation warning(s):")
        for slate_id, source, warning in warnings[:10]:
            print(f"    {slate_id} [{source}] {warning}")
        if len(warnings) > 10:
            print(f"    ... and {len(warnings) - 10} more")
    else:
        print("\n  no validation warnings.")

    print_status_counts(summary, "per-source status")

    print("\n  rows written vs rows in the DB:")
    db = row_counts(conn)
    for table in _SLATE_TABLES:
        written = summary.totals.get(table, 0)
        slates = conn.execute(f"SELECT COUNT(DISTINCT slate_id) FROM {table}").fetchone()[0]
        print(f"    {table:<16} wrote {written:>8,}   in DB {db[table]:>8,}   over {slates:>4} slates")

    mismatched = [t for t in _SLATE_TABLES if summary.totals.get(t, 0) != db[t]]
    check(
        "every table holds exactly what the backfill wrote",
        not mismatched,
        "matches" if not mismatched else _stale_detail(conn, discovery, summary, mismatched),
    )


def _stale_detail(conn, discovery: Discovery, summary, mismatched: list[str]) -> str:
    """Name what disagrees, not just that something does.

    A bare row-count dump means re-deriving the diff by hand against a 412-slate
    discovery. The usual cause is rows for a slate discovery no longer finds, so
    report those slate_ids first, then the per-table deltas.
    """
    parts = []
    for table in mismatched:
        wrote = summary.totals.get(table, 0)
        in_db = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        stale = sorted(
            row[0]
            for row in conn.execute(f"SELECT DISTINCT slate_id FROM {table}")
            if row[0] not in discovery.slates
        )
        detail = f"{table}: wrote {wrote:,}, DB holds {in_db:,}"
        if stale:
            detail += f" — {len(stale)} slate(s) not in discovery: {stale[:5]}"
        parts.append(detail)
    return "; ".join(parts)


def integrity_gate(conn: sqlite3.Connection) -> None:
    """The Phase 3 per-slate invariants, re-checked across the whole DB."""
    print("\nIntegrity (whole DB)")

    bad = conn.execute(
        "SELECT slate_id, final_rank, COUNT(*) c FROM lineup_players"
        " GROUP BY slate_id, final_rank HAVING c <> 8"
    ).fetchall()
    check("8 players per lineup everywhere", not bad, f"{len(bad)} bad, e.g. {bad[:3]}")

    orphans = conn.execute(
        "SELECT COUNT(*) FROM lineup_players lp"
        " LEFT JOIN slate_players sp ON lp.slate_id = sp.slate_id AND lp.dk_id = sp.dk_id"
        " WHERE sp.dk_id IS NULL"
    ).fetchone()[0]
    check("no orphan rostered players", orphans == 0, f"{orphans} orphan(s)")

    headerless = conn.execute(
        "SELECT COUNT(*) FROM (SELECT lp.slate_id, lp.final_rank FROM lineup_players lp"
        " WHERE NOT EXISTS (SELECT 1 FROM lineups l"
        "   WHERE l.slate_id = lp.slate_id AND l.final_rank = lp.final_rank)"
        " GROUP BY lp.slate_id, lp.final_rank)"
    ).fetchone()[0]
    check("no slot rows without a lineup header", headerless == 0, f"{headerless} group(s)")

    slotless = conn.execute(
        "SELECT COUNT(*) FROM lineups l WHERE NOT EXISTS (SELECT 1 FROM lineup_players lp"
        " WHERE lp.slate_id = l.slate_id AND lp.final_rank = l.final_rank)"
    ).fetchone()[0]
    check("every lineup has its players", slotless == 0, f"{slotless} lineup(s) with no slots")

    # Projections without a slate_players row are only expected on the three
    # dates that have no salary CSV at all; anywhere else it means a wrong-slate
    # write, which is the failure mode the `late` open question warned about.
    proj_orphan_slates = conn.execute(
        "SELECT p.slate_id, COUNT(*) FROM projections p"
        " LEFT JOIN slate_players sp ON p.slate_id = sp.slate_id AND p.dk_id = sp.dk_id"
        " WHERE sp.dk_id IS NULL GROUP BY p.slate_id ORDER BY 2 DESC"
    ).fetchall()
    no_salary_at_all = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT slate_id FROM projections WHERE slate_id NOT IN"
            " (SELECT DISTINCT slate_id FROM slate_players)"
        )
    }
    unexpected = [r for r in proj_orphan_slates if r[0] not in no_salary_at_all]
    check(
        "projections rows join to slate_players wherever salary exists",
        not unexpected,
        f"{len(unexpected)} slate(s) with unjoinable projections, e.g. {unexpected[:3]}",
    )
    if no_salary_at_all:
        note(
            "projections-only slates",
            f"{len(no_salary_at_all)} slate(s) have projections but no salary CSV: "
            f"{sorted(no_salary_at_all)}",
        )


def idempotency_gate(conn: sqlite3.Connection, discovery: Discovery) -> None:
    """Re-run the richest slate through the orchestrator; nothing may change."""
    richest = max(
        discovery.slates.values(),
        key=lambda s: (len(s.present), s.lineups.n_lineups if s.lineups else 0),
    )
    print(f"\nIdempotency — re-running {richest.slate_id} ({richest.coverage})")

    before = row_counts(conn)
    result = ingest_day(richest.date, richest.slate_type, conn, discovery=discovery)
    after = row_counts(conn)
    print(f"    {result.summary_line()}")
    check("re-ingest changed no row count", before == after, f"{before} -> {after}")
    check("re-ingest reported no failure", result.ok, str(result.failures))


# --- main ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # `_failures` is module-level, so a second `main()` call in one process would
    # otherwise inherit the first run's failures — skipping the backfill and
    # returning 1 on an otherwise clean run.
    _failures.clear()

    parser = argparse.ArgumentParser(description="Phase 4 gate: orchestrator + backfill.")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="after the dry-run gate, load every discovered slate into data/analytics.db",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=2,
        metavar="N",
        help="slates to dry-run per coverage combination (default 2)",
    )
    args = parser.parse_args(argv)
    if args.sample < 1:
        # `--sample 0` samples nothing, and every dry-run check then passes
        # vacuously — including the one guarding against a vacuous pass.
        parser.error("--sample must be at least 1")

    print("Phase 4 gate — orchestrator + backfill")
    print(f"  salary dir:      {SALARY_DIR}")
    print(f"  projections dir: {PROJECTIONS_DIR}")

    conn = get_connection()
    try:
        try:
            for action in migrate(conn):
                print(f"  schema migration: {action}")
            init_db(conn)
        except SchemaMigrationError as exc:
            check("schema migration", False, str(exc))
            return 1

        try:
            discovery = discover_slates()
        except (FileNotFoundError, ValueError) as exc:
            # A missing source dir or a drifted lineups manifest — both messages
            # carry their own fix, so report rather than traceback.
            check("discovery", False, str(exc))
            return 1

        if not discovery.slates:
            check("discovery found slates", False, "no slate resolved from any source directory")
            return 1

        discovery_gate(discovery)
        dry_run_gate(conn, discovery, args.sample)

        if args.backfill:
            if _failures:
                print("\nSkipping the backfill: the dry-run gate failed above.")
            else:
                backfill_gate(conn, discovery)
                integrity_gate(conn)
                idempotency_gate(conn, discovery)
        else:
            print("\nDry-run gate only. Re-run with --backfill to load the data.")
    finally:
        conn.close()

    if _failures:
        print(f"\n{len(_failures)} gate check(s) FAILED: {_failures}")
        return 1
    print("\nAll gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
