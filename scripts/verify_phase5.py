"""Local gate verification for Phase 5 (crosswalk) — run on the Windows machine.

Usage:
    uv run python scripts/verify_phase5.py                  # the gate: report only, writes nothing
    uv run python scripts/verify_phase5.py --review q.csv   # also export the review queue
    uv run python scripts/verify_phase5.py --write          # then write the auto-matched tiers
    uv run python scripts/verify_phase5.py --write --apply q.csv   # ...plus approved rows

The Phase 5 gate in docs/ingestion-plan.md is "show match-rate and the
low-confidence list for review before writing", so **the default run writes
nothing** and its job is to put those two things in front of Jonny.

**Part 1 — match report (default).**

  - the ops DB attaches read-only, and a probe write to it is rejected
  - `dim_players` is readable and non-empty
  - every distinct `slate_players` name is tiered exactly once
  - no name is auto-matched ambiguously, and no auto-match lacks a player_id
  - normalization introduces no collisions on either side
  - the match report is printed in full: rate, review queue, unmatched
  - `dk_crosswalk`'s row count is identical before and after — the gate itself
    is proof that reporting doesn't write

**Part 2 — write (`--write`, optionally with `--apply`).** Writes the auto tiers
(plus any approved rows) and re-checks the result against the DB:

  - every written `dk_id` exists in `slate_players`
  - every written `player_id` exists in ops `dim_players`
  - no `REVIEW`/`AMBIGUOUS`/`NONE` name was written unless it was approved
  - re-running the write changes no row count (idempotency)
  - the unmatched report accounts for exactly what wasn't written

Exit code 0 = all gates passed; 1 = something failed (details printed).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from nba_dfs_stats_lab.db.connection import attach_ops, get_connection
from nba_dfs_stats_lab.db.schema import SchemaMigrationError, init_db
from nba_dfs_stats_lab.ingest.crosswalk import (
    AUTO_TIERS,
    ApprovalError,
    MatchReport,
    Tier,
    apply_approvals,
    coverage,
    match_players,
    normalize_name,
    print_coverage,
    print_match_report,
    print_unmatched,
    read_approvals,
    unmatched_report,
    write_crosswalk,
    write_review_csv,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    if not ok:
        _failures.append(label)
    return ok


def note(label: str, detail: str) -> None:
    print(f"  [note] {label} — {detail}")


def crosswalk_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[0]


# --- Part 1: the ops dependency ----------------------------------------------


def ops_gate(conn: sqlite3.Connection) -> None:
    """The ops DB is a read-only, query-time dependency — prove it every run.

    Phase 1 proved this once on 2026-07-26. Phase 5 is the first phase that
    actually reads ops in anger, so the probe is re-run here rather than being
    taken on trust from a gate three phases ago.
    """
    print("\nOps DB (read-only ATTACH):")
    count = conn.execute("SELECT COUNT(*) FROM ops.dim_players").fetchone()[0]
    check("dim_players is readable and non-empty", count > 0, f"{count} players")

    try:
        conn.execute("CREATE TABLE ops.__probe (x INTEGER)")
    except sqlite3.OperationalError as exc:
        check(
            "probe write to ops is rejected", "readonly database" in str(exc), str(exc)
        )
    else:
        conn.execute("DROP TABLE ops.__probe")
        check(
            "probe write to ops is rejected",
            False,
            "the ATTACH is WRITABLE — stop and fix",
        )


# --- Part 1: matching ---------------------------------------------------------


def collision_gate(conn: sqlite3.Connection) -> None:
    """Normalization must not merge two distinct players on either side."""
    print("\nNormalization safety:")
    for label, rows in (
        (
            "ops dim_players",
            conn.execute("SELECT PLAYER_NAME FROM ops.dim_players").fetchall(),
        ),
        (
            "slate_players",
            conn.execute(
                "SELECT DISTINCT name FROM slate_players WHERE name IS NOT NULL"
            ).fetchall(),
        ),
    ):
        buckets: dict[str, set[str]] = {}
        for (name,) in rows:
            buckets.setdefault(normalize_name(name), set()).add(name)
        collisions = {k: v for k, v in buckets.items() if len(v) > 1}
        check(
            f"{label}: no two names collapse to one key",
            not collisions,
            f"{len(rows)} names -> {len(buckets)} keys"
            + (f"; collisions: {list(collisions.items())[:3]}" if collisions else ""),
        )


def match_gate(conn: sqlite3.Connection, report: MatchReport) -> None:
    print("\nMatching:")
    names = conn.execute(
        "SELECT COUNT(DISTINCT name) FROM slate_players "
        "WHERE name IS NOT NULL AND TRIM(name) <> ''"
    ).fetchone()[0]
    check(
        "every distinct slate_players name is tiered exactly once",
        len(report.matches) == names and len({m.name for m in report.matches}) == names,
        f"{len(report.matches)} matches for {names} names",
    )

    counts = report.counts()
    check(
        "tier counts sum to the name count",
        sum(counts.values()) == len(report.matches),
        ", ".join(f"{k} {v}" for k, v in counts.items()),
    )
    check(
        "every auto-matched name resolved to a player_id",
        all(m.player_id is not None for m in report.auto),
        f"{len(report.auto)} auto-matched",
    )
    check(
        "no REVIEW/AMBIGUOUS/NONE name carries a decision",
        all(m.player_id is None for m in report.matches if m.tier not in AUTO_TIERS),
        "a proposal must not look like a decision",
    )
    check(
        "no auto-matched name is ambiguous",
        counts[Tier.AMBIGUOUS.value] == 0
        or not any(m.approvable for m in report.matches if m.tier is Tier.AMBIGUOUS),
        f"{counts[Tier.AMBIGUOUS.value]} ambiguous name(s)",
    )

    auto_ids, total_ids = report.dk_id_coverage()
    total_rows = conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0]
    check(
        "the dk_ids behind the tiers account for every slate_players row",
        total_ids == total_rows,
        f"{total_ids} dk_ids vs {total_rows} rows",
    )
    note(
        "auto-match rate",
        f"{len(report.auto)}/{len(report.matches)} names ({report.match_rate():.1%})",
    )
    note(
        "dk_id coverage",
        f"{auto_ids}/{total_ids} rows ({auto_ids / total_ids:.1%})"
        if total_ids
        else "n/a",
    )

    # Not a pass/fail: what's left over is Jonny's decision, and the gate's job
    # is to show it, not to hold an opinion about how many there should be.
    if report.needs_review:
        note(
            "awaiting review",
            f"{len(report.needs_review)} name(s) — listed in full below",
        )
    if report.unmatched:
        unmatched_ids = sum(len(m.dk_ids) for m in report.unmatched)
        note(
            "unmatched",
            f"{len(report.unmatched)} name(s), {unmatched_ids} slate_players row(s)",
        )


# --- Part 2: writing ----------------------------------------------------------


def write_gate(
    conn: sqlite3.Connection, report: MatchReport, approved_names: set[str]
) -> None:
    print("\nAfter write:")
    written = conn.execute(
        "SELECT dk_id, player_id, display_name FROM dk_crosswalk"
    ).fetchall()

    orphans = conn.execute(
        "SELECT COUNT(*) FROM dk_crosswalk x "
        "LEFT JOIN slate_players sp ON sp.dk_id = x.dk_id WHERE sp.dk_id IS NULL"
    ).fetchone()[0]
    check(
        "every dk_crosswalk.dk_id exists in slate_players",
        orphans == 0,
        f"{orphans} orphan(s)",
    )

    unknown_players = conn.execute(
        "SELECT COUNT(*) FROM dk_crosswalk x "
        "LEFT JOIN ops.dim_players d ON d.PLAYER_ID = x.player_id WHERE d.PLAYER_ID IS NULL"
    ).fetchone()[0]
    check(
        "every dk_crosswalk.player_id exists in ops.dim_players",
        unknown_players == 0,
        f"{unknown_players} unknown player_id(s)",
    )

    # The gate's whole point: nothing unapproved got in.
    allowed = {m.name for m in report.auto} | approved_names
    written_names = {name for _, _, name in written}
    leaked = sorted(written_names - allowed)
    check(
        "no unapproved low-confidence name was written",
        not leaked,
        f"{len(leaked)} leaked: {leaked[:5]}"
        if leaked
        else f"{len(written_names)} name(s) written",
    )

    # One dk_id maps to one player: the PK guarantees it, so this catches a
    # future writer that widens the table rather than a bug in this one.
    dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT dk_id FROM dk_crosswalk GROUP BY dk_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    check(
        "each dk_id maps to exactly one player_id", dupes == 0, f"{dupes} duplicate(s)"
    )

    # A name must not map to two different ops players across its dk_ids.
    split = conn.execute(
        "SELECT COUNT(*) FROM (SELECT display_name FROM dk_crosswalk "
        "GROUP BY display_name HAVING COUNT(DISTINCT player_id) > 1)"
    ).fetchone()[0]
    check(
        "each name maps to one player_id across all its dk_ids",
        split == 0,
        f"{split} split name(s)",
    )

    before = crosswalk_rows(conn)
    write_crosswalk(conn, report.auto)
    check(
        "re-writing the same matches changes no row count",
        crosswalk_rows(conn) == before,
        f"{before} rows",
    )

    stats = coverage(conn)
    accounted = stats["names"] - stats["names_covered"]
    check(
        "the unmatched report accounts for exactly what wasn't written",
        len(unmatched_report(conn)) == accounted,
        f"{accounted} name(s) uncovered",
    )
    print_coverage(stats, "Coverage")


# --- main ---------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 5 crosswalk gate.")
    parser.add_argument(
        "--review", metavar="PATH", help="export the review queue to a CSV"
    )
    parser.add_argument(
        "--write", action="store_true", help="write the auto-matched tiers"
    )
    parser.add_argument(
        "--apply", metavar="PATH", help="also write the approved rows of a reviewed CSV"
    )
    args = parser.parse_args(argv)

    if args.apply and not args.write:
        print("--apply requires --write", file=sys.stderr)
        return 2

    conn = get_connection()
    try:
        try:
            init_db(conn)
        except SchemaMigrationError as exc:
            print(f"schema migration failed: {exc}", file=sys.stderr)
            return 1
        try:
            attach_ops(conn)
        except sqlite3.OperationalError as exc:
            print(f"could not attach the ops DB read-only: {exc}", file=sys.stderr)
            return 1

        if conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0:
            print(
                "slate_players is empty — run the Phase 4 backfill first:\n"
                "  uv run python -m nba_dfs_stats_lab.ingest.orchestrator --all",
                file=sys.stderr,
            )
            return 1

        print("=== Phase 5 gate: crosswalk ===")
        ops_gate(conn)
        collision_gate(conn)

        before = crosswalk_rows(conn)
        report = match_players(conn)
        match_gate(conn, report)
        check(
            "matching wrote nothing to dk_crosswalk",
            crosswalk_rows(conn) == before,
            f"{before} rows before and after",
        )

        print_match_report(report)

        if args.review:
            rows = write_review_csv(report, Path(args.review))
            print(
                f"\nWrote {rows} candidate row(s) to {args.review} — mark `approve` = y "
                f"on the ones you accept, then re-run with --write --apply {args.review}"
            )

        approved_names: set[str] = set()
        if args.write:
            to_write = list(report.auto)
            if args.apply:
                try:
                    approvals = read_approvals(Path(args.apply))
                    resolved = apply_approvals(report, approvals)
                except ApprovalError as exc:
                    print(f"\napproval file rejected: {exc}", file=sys.stderr)
                    return 2
                to_write.extend(resolved)
                approved_names = {m.name for m in resolved}
                print(
                    f"\n{len(approved_names)} approved name(s) read from {args.apply}"
                )
            written = write_crosswalk(conn, to_write)
            print(
                f"\nWrote {written} dk_crosswalk row(s) from {len(to_write)} name(s)."
            )
            write_gate(conn, report, approved_names)
            print_unmatched(unmatched_report(conn))
        else:
            print(
                "\n(Report only — nothing written. Re-run with --write once the review "
                "list above is settled.)"
            )
    finally:
        conn.close()

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("All Phase 5 gate checks PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
