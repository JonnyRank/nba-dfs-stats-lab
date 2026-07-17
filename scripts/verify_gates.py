"""Local gate verification for Phases 1-2 — run this on the Windows machine.

Usage:
    uv run python scripts/verify_gates.py                      # newest Main-slate projections file
    uv run python scripts/verify_gates.py NBA-Projs-2026-02-28.csv

Runs the two deferred gates from docs/ingestion-plan.md:

  Phase 1 gate: init_db creates the five tables in data/analytics.db, and
    attach_ops opens the real ops DB read-only (a probe write must fail).
  Phase 2 gate: ingest one real Main slate's projections, show counts and
    sample rows, re-ingest the same file, and confirm the total row count
    is unchanged (idempotency).

Exit code 0 = all gates passed; 1 = something failed (details printed).
"""

import sqlite3
import sys
from pathlib import Path

from nba_dfs_stats_lab.config import OPS_DB, PROJECTIONS_DIR
from nba_dfs_stats_lab.db.connection import attach_ops, get_connection
from nba_dfs_stats_lab.db.schema import TABLES, init_db
from nba_dfs_stats_lab.ingest.filenames import build_slate_id, parse_projections_filename
from nba_dfs_stats_lab.ingest.projections import ingest_projections

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    if not ok:
        _failures.append(label)
    return ok


def phase1_gate(conn: sqlite3.Connection, ops_path: Path = OPS_DB) -> None:
    print("\nPhase 1 gate — schema + read-only ops attach")
    init_db(conn)
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [t for t in TABLES if t not in existing]
    check("all five tables exist", not missing, f"missing: {missing}" if missing else ", ".join(TABLES))

    if not check("ops DB file exists", Path(ops_path).exists(), str(ops_path)):
        return
    attach_ops(conn, ops_path)
    n_tables = conn.execute("SELECT COUNT(*) FROM ops.sqlite_master WHERE type = 'table'").fetchone()[0]
    check("ops DB attached and readable", n_tables > 0, f"{n_tables} tables visible")
    try:
        conn.execute("CREATE TABLE ops.__write_probe (x)")
        check("ops DB rejects writes", False, "probe write unexpectedly succeeded!")
        conn.execute("DROP TABLE ops.__write_probe")  # undo if mode=ro silently failed
    except sqlite3.OperationalError as exc:
        # Only a readonly failure proves mode=ro; a locked DB or leftover
        # probe table also raises OperationalError but proves nothing.
        is_read_only = "readonly" in str(exc).lower()
        check("ops DB rejects writes", is_read_only, f"write raised: {exc}")
    conn.execute("DETACH DATABASE ops")


def _newest_main_projections(projections_dir: Path) -> Path:
    """Newest projections file whose parsed slate type is main."""
    candidates = []
    for p in Path(projections_dir).glob("NBA-Projs-*.csv"):
        try:
            parsed = parse_projections_filename(p.name)
        except ValueError:
            continue  # not a projections file per the convention; skip
        if parsed.slate_type == "main":
            candidates.append((parsed.date, p))
    if not candidates:
        raise SystemExit(f"no Main-slate projections files found in {projections_dir}")
    return max(candidates)[1]


def phase2_gate(conn: sqlite3.Connection, csv_path: Path) -> None:
    print(f"\nPhase 2 gate — ingest {csv_path.name}")
    parsed = parse_projections_filename(csv_path.name)
    slate_id = build_slate_id(parsed.date, parsed.slate_type)
    print(f"  slate_id: {slate_id}")

    n_first = ingest_projections(csv_path, slate_id, conn)
    total, slates = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT slate_id) FROM projections"
    ).fetchone()
    check("first ingest wrote rows", n_first > 0, f"{n_first} rows ({total} total, {slates} slate(s))")

    n_second = ingest_projections(csv_path, slate_id, conn)
    total_after = conn.execute("SELECT COUNT(*) FROM projections").fetchone()[0]
    check(
        "re-ingest is idempotent",
        n_second == n_first and total_after == total,
        f"re-ingest wrote {n_second}, total {total} -> {total_after}",
    )

    print("\n  sample rows:")
    cur = conn.execute(
        "SELECT slate_id, dk_id, minutes, fppm, proj_pts, proj_own"
        " FROM projections WHERE slate_id = ? LIMIT 5",
        (slate_id,),
    )
    print("  " + " | ".join(d[0] for d in cur.description))
    for row in cur:
        print("  " + " | ".join(str(v) for v in row))


def main() -> int:
    csv_path = (
        Path(PROJECTIONS_DIR) / sys.argv[1] if len(sys.argv) > 1
        else _newest_main_projections(PROJECTIONS_DIR)
    )
    conn = get_connection()
    try:
        phase1_gate(conn)
        phase2_gate(conn, csv_path)
    finally:
        conn.close()

    if _failures:
        print(f"\n{len(_failures)} gate check(s) FAILED: {_failures}")
        return 1
    print("\nAll gate checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
