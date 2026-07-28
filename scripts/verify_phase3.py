"""Local gate verification for Phase 3 (salary + lineups) — run on the Windows machine.

Usage:
    uv run python scripts/verify_phase3.py                          # newest slate with both sources
    uv run python scripts/verify_phase3.py 2026-05-18_classic_main  # a specific slate
    uv run python scripts/verify_phase3.py --list                   # slates available to load

Runs the Phase 3 gate from docs/ingestion-plan.md:

  - ingest one real slate's salary CSV into `slate_players`
  - ingest the same slate's keep-latest lineups CSV into `lineups` +
    `lineup_players`
  - re-ingest both and confirm the row counts are unchanged (idempotency)
  - integrity: exactly 8 players per lineup; zero rostered players missing
    from `slate_players`

Exit code 0 = all gates passed; 1 = something failed (details printed).
"""

import sqlite3
import sys
from pathlib import Path

from nba_dfs_stats_lab.config import SALARY_DIR
from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import init_db, migrate
from nba_dfs_stats_lab.ingest.filenames import parse_slate_id, salary_filename
from nba_dfs_stats_lab.ingest.lineups import (
    LineupsFile,
    ingest_lineups,
    latest_lineups_by_slate,
)
from nba_dfs_stats_lab.ingest.salary import ingest_salary

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    if not ok:
        _failures.append(label)
    return ok


def salary_path(slate_id: str) -> Path:
    parsed = parse_slate_id(slate_id)
    return Path(SALARY_DIR) / salary_filename(parsed.date, parsed.slate_type)


def loadable_slates() -> dict[str, LineupsFile]:
    """Slates with both a keep-latest lineups file and a salary CSV."""
    return {
        slate_id: f
        for slate_id, f in latest_lineups_by_slate().items()
        if salary_path(slate_id).exists()
    }


def salary_gate(conn: sqlite3.Connection, slate_id: str) -> None:
    path = salary_path(slate_id)
    print(f"\nSalary — {path.name}")
    n_first = ingest_salary(path, slate_id, conn)
    total = conn.execute(
        "SELECT COUNT(*) FROM slate_players WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    check("salary ingested", n_first > 0 and total == n_first, f"{n_first} players")

    n_second = ingest_salary(path, slate_id, conn)
    total_after = conn.execute(
        "SELECT COUNT(*) FROM slate_players WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    check(
        "salary re-ingest is idempotent",
        n_second == n_first and total_after == total,
        f"re-ingest wrote {n_second}, total {total} -> {total_after}",
    )

    row = conn.execute(
        "SELECT COUNT(*), COUNT(actual_fpts), MIN(salary), MAX(salary)"
        " FROM slate_players WHERE slate_id = ?",
        (slate_id,),
    ).fetchone()
    check(
        "salary columns populated",
        row[0] > 0 and row[2] is not None,
        f"{row[1]}/{row[0]} rows have actual_fpts; salary {row[2]}-{row[3]}",
    )


def lineups_gate(conn: sqlite3.Connection, slate_id: str, lineups_file: LineupsFile) -> None:
    print(f"\nLineups — {lineups_file.path.name} (generated {lineups_file.generated_at})")
    first = ingest_lineups(lineups_file.path, slate_id, conn)
    check(
        "lineups ingested",
        first.lineups > 0 and first.lineup_players == first.lineups * 8,
        f"{first.lineups} lineups, {first.lineup_players} lineup_players",
    )

    second = ingest_lineups(lineups_file.path, slate_id, conn)
    totals = conn.execute(
        "SELECT (SELECT COUNT(*) FROM lineups WHERE slate_id = ?),"
        "       (SELECT COUNT(*) FROM lineup_players WHERE slate_id = ?)",
        (slate_id, slate_id),
    ).fetchone()
    check(
        "lineups re-ingest is idempotent",
        second == first and totals == tuple(first),
        f"re-ingest wrote {tuple(second)}, table totals {totals}",
    )


def integrity_gate(conn: sqlite3.Connection, slate_id: str) -> None:
    print("\nIntegrity checks")

    # 1. Exactly 8 players per lineup.
    bad = conn.execute(
        "SELECT final_rank, COUNT(*) c FROM lineup_players WHERE slate_id = ?"
        " GROUP BY final_rank HAVING c <> 8",
        (slate_id,),
    ).fetchall()
    n_lineups = conn.execute(
        "SELECT COUNT(*) FROM lineups WHERE slate_id = ?", (slate_id,)
    ).fetchone()[0]
    check(
        "8 players per lineup",
        not bad,
        f"{n_lineups} lineups all at 8" if not bad else f"{len(bad)} bad, e.g. {bad[:3]}",
    )

    # 2. No rostered player missing from slate_players.
    orphans = conn.execute(
        "SELECT COUNT(*) FROM lineup_players lp"
        " LEFT JOIN slate_players sp ON lp.slate_id = sp.slate_id AND lp.dk_id = sp.dk_id"
        " WHERE lp.slate_id = ? AND sp.dk_id IS NULL",
        (slate_id,),
    ).fetchone()[0]
    check("no orphan rostered players", orphans == 0, f"{orphans} orphan(s)")

    # 3. Every lineup header has slot rows, and vice versa.
    mismatch = conn.execute(
        "SELECT COUNT(*) FROM lineups l"
        " WHERE l.slate_id = ? AND NOT EXISTS ("
        "   SELECT 1 FROM lineup_players lp"
        "   WHERE lp.slate_id = l.slate_id AND lp.final_rank = l.final_rank)",
        (slate_id,),
    ).fetchone()[0]
    check("every lineup has its players", mismatch == 0, f"{mismatch} lineup(s) with no slots")

    print("\n  sample lineup (final_rank 1):")
    cur = conn.execute(
        "SELECT lp.slot, lp.dk_id, sp.name, sp.team, sp.salary, sp.actual_fpts"
        " FROM lineup_players lp"
        " JOIN slate_players sp ON lp.slate_id = sp.slate_id AND lp.dk_id = sp.dk_id"
        " WHERE lp.slate_id = ? AND lp.final_rank = 1"
        # Roster order, not alphabetical. The commas delimit each slot so that
        # 'G' matches ",G," and not the G inside "PG".
        " ORDER BY instr(',PG,SG,SF,PF,C,G,F,UTIL,', ',' || lp.slot || ',')",
        (slate_id,),
    )
    print("  " + " | ".join(d[0] for d in cur.description))
    for row in cur:
        print("  " + " | ".join(str(v) for v in row))

    header = conn.execute(
        "SELECT lineup_score, total_projection, total_ownership, proj_rank, own_rank, geo_rank"
        " FROM lineups WHERE slate_id = ? AND final_rank = 1",
        (slate_id,),
    ).fetchone()
    print(f"\n  lineup 1 header: {header}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    conn = get_connection()
    try:
        for action in migrate(conn):
            print(f"  schema migration: {action}")
        init_db(conn)

        available = loadable_slates()
        if "--list" in sys.argv:
            print(f"{len(available)} slate(s) with both a salary CSV and a lineups file:")
            for slate_id in sorted(available):
                f = available[slate_id]
                print(f"  {slate_id}  {f.n_lineups:>5} lineups  {f.path.name}")
            return 0

        slate_id = args[0] if args else max(available)
        if slate_id not in available:
            print(f"slate {slate_id!r} has no salary CSV and/or no lineups file.")
            print("Run with --list to see what's available.")
            return 1
        print(f"Phase 3 gate — slate {slate_id}")

        salary_gate(conn, slate_id)
        lineups_gate(conn, slate_id, available[slate_id])
        integrity_gate(conn, slate_id)
    finally:
        conn.close()

    if _failures:
        print(f"\n{len(_failures)} gate check(s) FAILED: {_failures}")
        return 1
    print("\nAll gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
