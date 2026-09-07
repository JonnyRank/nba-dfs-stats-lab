"""Local gate verification for Phase 6 (ops reconciliation) — run on Windows.

Usage:
    uv run python scripts/verify_phase6.py            # the gate: report only, writes nothing
    uv run python scripts/verify_phase6.py --write     # then apply and re-check the DB

Two stages, like Phase 5. The default run puts the full change list in front of
Jonny **before** anything writes; `--write` applies it and re-checks the result
against the database rather than against the in-memory report.

**Part 1 — computed (default), nothing written.**

  - the ops DB attaches read-only, and a probe write to it is rejected
  - `fantasy_logs` and `map_teams` are readable and non-empty
  - no correction lacks an `ops_value`, and `dk_value + delta == ops_value`
  - every resolved `game_date` is within ±1 day of its slate date
  - every shifted matchup is shifted **as a whole** — the 907-row trap
  - the 237 uncrosswalked rows are untouched and still 0
  - the off-slate population is 209 rows, 109 of them corrections
  - the action and reason censuses match what the spec pinned
  - `fpts_audit`'s row count is identical before and after — the gate itself is
    proof that reporting doesn't write

**Part 2 — after `--write`.**

  - every `slate_players` row has exactly one `fpts_audit` row, and vice versa
  - every audit `action` is one of the four, and they sum to the census
  - every corrected row's `actual_fpts` now equals its `ops_value`
  - every other row's `actual_fpts` still equals its pristine `dk_value`
  - `actual_fpts` has no NULLs (D2: this phase introduces none)
  - re-running changes no value and no audit row count
  - ops row counts are identical to what they were at startup

Exit code 0 = all gates passed; 1 = something failed (details printed).
"""

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nba_dfs_stats_lab.db.connection import attach_ops, get_connection  # noqa: E402
from nba_dfs_stats_lab.db.schema import SchemaMigrationError, init_db  # noqa: E402
from nba_dfs_stats_lab.ingest.reconcile import (  # noqa: E402
    ACTIONS,
    REASONS,
    ReconcileError,
    ReconcileReport,
    apply_corrections,
    audit_rollup,
    print_audit,
    print_report,
    print_write_preview,
    reconcile,
    write_audit,
)

# Pinned from the 2026-08-09 survey, re-derived 2026-09-07 against the same
# snapshot. These are the two checks that fail on *new data* rather than on a
# code defect — a new slate or a refreshed ops snapshot moves them. That is a
# prompt to look, not a bug: re-derive with Appendix A of
# docs/phase6-ops-reconciliation.md, and if the new rows fall into an existing
# class, re-pin the numbers here.
EXPECTED_ACTIONS = {
    "corrected": 930,
    "unchanged": 31_032,
    "no_ops_row": 19_772,
    "unmapped": 237,
}

EXPECTED_REASONS = {
    None: 30_795,  # agreed on the slate date; nothing to say about them
    "dnp": 19_678,
    "float_noise": 680,
    "no_crosswalk": 237,
    "date_shift": 232,
    "off_slate": 172,
    "stat_correction": 80,
    "dk_unscored": 61,
    "no_ops_game": 36,
}

EXPECTED_OFF_SLATE_ROWS = 209
EXPECTED_OFF_SLATE_CORRECTED = 109

_failures: list[str] = []  # reset at the top of main(); see the note there


def check(label: str, ok: bool, detail: str = "") -> bool:
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{suffix}")
    if not ok:
        _failures.append(label)
    return ok


def note(label: str, detail: str) -> None:
    print(f"  [note] {label} — {detail}")


def audit_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM fpts_audit").fetchone()[0]


def fpts_digest(conn: sqlite3.Connection) -> tuple:
    """A digest of `slate_players.actual_fpts` that a changed value must move.

    Sum alone would miss a swap between two rows (the stat corrections cancel
    within a game, so a swapped pair is exactly the plausible bug); the sum of
    squares does not.
    """
    return conn.execute(
        "SELECT COUNT(*), TOTAL(actual_fpts), TOTAL(actual_fpts * actual_fpts), "
        "SUM(actual_fpts IS NULL) FROM slate_players"
    ).fetchone()


def ops_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts for every ops table, to prove nothing there moved."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM ops.sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for table in tables:
        if not table.isidentifier():
            continue  # nothing in the snapshot needs quoting; skip rather than interpolate
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM ops.{table}").fetchone()[0]  # noqa: S608
    return counts


# --- Part 1: the ops dependency ----------------------------------------------


def ops_gate(conn: sqlite3.Connection) -> None:
    """Ops is a read-only, query-time dependency — prove it on every run.

    Re-proved here rather than taken on trust from the Phase 5 gate: Phase 6 is
    the first phase that writes anything *derived* from ops, so "we only read
    it" is the claim most worth re-testing.
    """
    print("\nOps DB (read-only ATTACH):")
    for table in ("fantasy_logs", "map_teams"):
        # attach_ops succeeds whenever the file opens, so a snapshot missing a
        # table gets this far. That should be a verdict, not a traceback.
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM ops.{table}").fetchone()[0]  # noqa: S608
        except sqlite3.OperationalError as exc:
            check(f"ops.{table} is readable and non-empty", False, str(exc))
        else:
            check(f"ops.{table} is readable and non-empty", count > 0, f"{count} rows")

    try:
        conn.execute("CREATE TABLE ops.__probe (x INTEGER)")
    except sqlite3.OperationalError as exc:
        check("probe write to ops is rejected", "readonly database" in str(exc), str(exc))
    else:
        conn.execute("DROP TABLE ops.__probe")
        check("probe write to ops is rejected", False, "the ATTACH is WRITABLE — stop and fix")

    # Every slate team must be translatable, or a game-side silently fails to
    # resolve and its players are all classified as DNPs. Asked of the data on
    # both sides rather than assumed from "map_teams has 30 rows". Guarded for
    # the same reason as the counts above: a snapshot without the table is a
    # verdict, not a traceback.
    try:
        unmapped = conn.execute(
            "SELECT COUNT(DISTINCT TRIM(team)) FROM slate_players sp "
            "WHERE team IS NOT NULL AND TRIM(team) <> '' "
            "AND NOT EXISTS (SELECT 1 FROM ops.map_teams m "
            "                 WHERE m.TEAM_ABBREVIATION = TRIM(sp.team))"
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:
        check("every slate_players team is in ops.map_teams", False, str(exc))
    else:
        check(
            "every slate_players team is in ops.map_teams",
            unmapped == 0,
            f"{unmapped} unmapped abbreviation(s)",
        )


# --- Part 1: the computed report ---------------------------------------------


def arithmetic_gate(report: ReconcileReport) -> None:
    """The corrections have to be arithmetically sound before they are applied."""
    print("\nCorrections (computed, not yet written):")
    corrections = report.corrections

    empty = [r for r in corrections if r.ops_value is None]
    check(
        "no correction lacks an ops_value",
        not empty,
        f"{len(empty)} correction(s) with nothing behind them"
        if empty
        else f"{len(corrections)} correction(s)",
    )

    # `delta` is a derived property, so this asks whether the three numbers the
    # audit will persist agree with each other. Exact equality would be wrong:
    # the float_noise rows exist precisely because these are binary floats.
    drift = [
        r
        for r in corrections
        if r.ops_value is not None
        and r.dk_value is not None
        and abs((r.dk_value + (r.delta or 0)) - r.ops_value) > 1e-9
    ]
    check(
        "dk_value + delta == ops_value on every corrected row",
        not drift,
        f"{len(drift)} row(s) drift" if drift else f"{len(corrections)} checked",
    )

    # An action outside the vocabulary would be written straight into the audit
    # and then be invisible to every census query in this file.
    unknown = sorted({r.action for r in report.rows} - set(ACTIONS))
    check("every action is one of the four", not unknown, f"unknown: {unknown}" if unknown else "")
    unknown_reasons = sorted({r.reason for r in report.rows if r.reason} - set(REASONS))
    check(
        "every reason is in the pinned vocabulary",
        not unknown_reasons,
        f"unknown: {unknown_reasons}" if unknown_reasons else "",
    )


def resolution_gate(report: ReconcileReport) -> None:
    """The game-date resolution — and the trap it exists to avoid."""
    print("\nGame-date resolution:")

    out_of_range = [
        r
        for r in report.rows
        if r.game_date is not None and abs(_days_between(r.slate_date, r.game_date)) > 1
    ]
    check(
        "every game_date is within ±1 day of its slate date",
        not out_of_range,
        f"{len(out_of_range)} row(s) outside the window"
        if out_of_range
        else f"{sum(1 for r in report.rows if r.game_date)} dated row(s)",
    )

    # The 907-row trap. A per-player ±1 window would give two players on the
    # same team in the same slate different game_dates; a matchup-level shift
    # cannot. Asked of the produced rows, not of the resolver that produced
    # them, so a future refactor that reintroduces per-player logic fails here.
    split = report.split_matchups()
    check(
        "every shifted matchup is shifted as a whole (the 907-row trap)",
        not split,
        f"{len(split)} matchup(s) with more than one game_date: {split[:3]}"
        if split
        else f"{len(report.shifted_rows())} shifted row(s), all matchup-wide",
    )

    unresolved = report.unresolved_sides()
    note(
        "unresolved matchups",
        ", ".join(f"{link.slate_date} {link.team} vs {link.opp}" for link in unresolved)
        or "none",
    )


def _days_between(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def census_gate(report: ReconcileReport) -> None:
    """The pinned censuses — the checks that fail on new data, by design."""
    print("\nCensus:")
    actions = report.action_counts()
    check(
        "the action census matches the pinned figures",
        actions == EXPECTED_ACTIONS,
        _diff(EXPECTED_ACTIONS, actions),
    )

    reasons = dict(report.reason_counts())
    check(
        "the reason census matches the pinned figures",
        reasons == EXPECTED_REASONS,
        _diff(EXPECTED_REASONS, reasons),
    )

    off = report.off_slate_rows()
    corrected = sum(1 for r in off if r.action == "corrected")
    check(
        f"the off-slate population is {EXPECTED_OFF_SLATE_ROWS} rows, "
        f"{EXPECTED_OFF_SLATE_CORRECTED} of them corrections",
        len(off) == EXPECTED_OFF_SLATE_ROWS and corrected == EXPECTED_OFF_SLATE_CORRECTED,
        f"{len(off)} row(s), {corrected} corrected",
    )

    # The 237 uncrosswalked rows are unreachable by construction — no player_id
    # means no ops lookup — so this asks whether the classifier honoured that.
    unmapped = [r for r in report.rows if r.player_id is None]
    reached = [r for r in unmapped if r.action != "unmapped" or r.ops_value is not None]
    nonzero = [r for r in unmapped if r.dk_value != 0]
    check(
        "no uncrosswalked row is corrected, and all still hold 0.0",
        not reached and not nonzero,
        f"{len(unmapped)} uncrosswalked row(s)"
        if not reached and not nonzero
        else f"{len(reached)} reached, {len(nonzero)} non-zero",
    )


def _diff(expected: dict, actual: dict) -> str:
    keys = sorted(set(expected) | set(actual), key=lambda k: (k is None, str(k)))
    parts = [
        f"{k or '(none)'} {actual.get(k, 0)}"
        + ("" if expected.get(k) == actual.get(k, 0) else f" (expected {expected.get(k, 0)})")
        for k in keys
    ]
    return ", ".join(parts)


# --- Part 2: after the write --------------------------------------------------


def write_gate(conn: sqlite3.Connection) -> None:
    """Everything below is asked of the database, not of the in-memory report."""
    print("\nAfter write:")

    missing, orphans = conn.execute(
        "SELECT (SELECT COUNT(*) FROM slate_players sp "
        "          LEFT JOIN fpts_audit a ON a.slate_id = sp.slate_id AND a.dk_id = sp.dk_id "
        "         WHERE a.dk_id IS NULL), "
        "       (SELECT COUNT(*) FROM fpts_audit a "
        "          LEFT JOIN slate_players sp ON sp.slate_id = a.slate_id AND sp.dk_id = a.dk_id "
        "         WHERE sp.dk_id IS NULL)"
    ).fetchone()
    check(
        "every slate_players row has exactly one fpts_audit row, and vice versa",
        missing == 0 and orphans == 0,
        f"{missing} uncensused, {orphans} orphan audit row(s)",
    )

    total = conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0]
    by_action = dict(conn.execute("SELECT action, COUNT(*) FROM fpts_audit GROUP BY action"))
    known = sum(by_action.get(a, 0) for a in ACTIONS)
    check(
        "every audit action is one of the four and they sum to the census",
        known == total and set(by_action) <= set(ACTIONS),
        f"{known} of {total} row(s); actions {sorted(by_action)}",
    )

    wrong = conn.execute(
        "SELECT COUNT(*) FROM fpts_audit a JOIN slate_players sp "
        "  ON sp.slate_id = a.slate_id AND sp.dk_id = a.dk_id "
        "WHERE a.action = 'corrected' "
        "  AND (a.ops_value IS NULL OR sp.actual_fpts IS NOT a.ops_value)"
    ).fetchone()[0]
    check(
        "every corrected row's actual_fpts now equals its ops_value",
        wrong == 0,
        f"{wrong} correction(s) not applied",
    )

    overreach = conn.execute(
        "SELECT COUNT(*) FROM fpts_audit a JOIN slate_players sp "
        "  ON sp.slate_id = a.slate_id AND sp.dk_id = a.dk_id "
        "WHERE a.action <> 'corrected' AND sp.actual_fpts IS NOT a.dk_value"
    ).fetchone()[0]
    check(
        "every uncorrected row's actual_fpts still equals its pristine dk_value",
        overreach == 0,
        f"{overreach} row(s) the UPDATE should not have touched",
    )

    nulls = conn.execute(
        "SELECT COUNT(*) FROM slate_players WHERE actual_fpts IS NULL"
    ).fetchone()[0]
    check("actual_fpts has no NULLs (D2)", nulls == 0, f"{nulls} NULL(s)")

    untouched = conn.execute(
        "SELECT COUNT(*) FROM fpts_audit a JOIN slate_players sp "
        "  ON sp.slate_id = a.slate_id AND sp.dk_id = a.dk_id "
        "WHERE a.player_id IS NULL AND (sp.actual_fpts <> 0 OR a.action <> 'unmapped')"
    ).fetchone()[0]
    check(
        "the uncrosswalked rows are untouched and still 0",
        untouched == 0,
        f"{untouched} row(s) reached without a player_id",
    )

    off_total, off_corrected = conn.execute(
        "SELECT COUNT(*), SUM(action = 'corrected') FROM fpts_audit WHERE off_slate = 1"
    ).fetchone()
    check(
        f"the stored off-slate flag is {EXPECTED_OFF_SLATE_ROWS} rows, "
        f"{EXPECTED_OFF_SLATE_CORRECTED} of them corrections",
        off_total == EXPECTED_OFF_SLATE_ROWS and (off_corrected or 0) == EXPECTED_OFF_SLATE_CORRECTED,
        f"{off_total} row(s), {off_corrected or 0} corrected",
    )

    split = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM fpts_audit a JOIN slate_players sp "
        "  ON sp.slate_id = a.slate_id AND sp.dk_id = a.dk_id "
        " WHERE a.game_date IS NOT NULL AND sp.team IS NOT NULL AND sp.opp IS NOT NULL "
        " GROUP BY a.slate_id, TRIM(sp.team), TRIM(sp.opp) "
        "HAVING COUNT(DISTINCT a.game_date) > 1)"
    ).fetchone()[0]
    check(
        "no stored matchup carries two game_dates (the 907-row trap)",
        split == 0,
        f"{split} split matchup(s)",
    )


def idempotency_gate(conn: sqlite3.Connection) -> None:
    """Re-run the whole pass and prove nothing moves.

    Not a formality: `load_slate` is delete-then-insert, so re-ingesting a slate
    reverts its reconciliation, and `_pristine_dk_value` is what makes a second
    pass reproduce the first instead of capturing the ops value as DK's.
    """
    print("\nIdempotency:")
    before_digest, before_rows = fpts_digest(conn), audit_rows(conn)
    before_pristine = conn.execute("SELECT TOTAL(dk_value), TOTAL(ops_value) FROM fpts_audit").fetchone()

    second = reconcile(conn)
    write_audit(conn, second.rows)
    apply_corrections(conn, second.rows)

    check(
        "re-running changes no actual_fpts value",
        fpts_digest(conn) == before_digest,
        f"{before_digest} -> {fpts_digest(conn)}",
    )
    check(
        "re-running changes no audit row count",
        audit_rows(conn) == before_rows,
        f"{before_rows} row(s)",
    )
    # The sharp end of §3.4: a second pass that read `actual_fpts` back as the
    # pristine value would leave dk_value == ops_value on all 930 corrections,
    # moving this total by the sum of the deltas.
    check(
        "the pristine dk_value survived the second pass",
        conn.execute("SELECT TOTAL(dk_value), TOTAL(ops_value) FROM fpts_audit").fetchone()
        == before_pristine,
        f"{before_pristine} -> "
        f"{conn.execute('SELECT TOTAL(dk_value), TOTAL(ops_value) FROM fpts_audit').fetchone()}",
    )


# --- main ---------------------------------------------------------------------


def main(argv=None) -> int:
    # Module state: Jonny runs this once per process, but the tests call main()
    # several times and an inherited failure would make a clean run return 1 —
    # and make the suite order-dependent.
    _failures.clear()
    parser = argparse.ArgumentParser(description="Phase 6 ops-reconciliation gate.")
    parser.add_argument(
        "--write", action="store_true", help="apply the corrections and write the audit"
    )
    parser.add_argument(
        "--slate", metavar="SLATE_ID", action="append", help="restrict to a slate (repeatable)"
    )
    args = parser.parse_args(argv)

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
        if conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[0] == 0:
            print(
                "dk_crosswalk is empty — build it first (Phase 5):\n"
                "  uv run python scripts/verify_phase5.py --write "
                "--apply docs/crosswalk-approvals.csv",
                file=sys.stderr,
            )
            return 1

        print("=== Phase 6 gate: ops reconciliation ===")
        ops_before = ops_counts(conn)
        ops_gate(conn)

        audit_before = audit_rows(conn)
        try:
            report = reconcile(conn, slate_ids=args.slate)
        except ReconcileError as exc:
            print(f"reconcile refused: {exc}", file=sys.stderr)
            return 2
        except sqlite3.OperationalError as exc:
            # A snapshot missing a table the pass needs. The ops checks above
            # have already said which one; carrying on to a traceback here would
            # bury their verdicts under a stack trace, so this becomes the last
            # FAIL line and the run ends with the usual summary.
            check("the reconcile pass runs against this ops snapshot", False, str(exc))
            print()
            print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
            return 1

        arithmetic_gate(report)
        resolution_gate(report)
        if args.slate:
            note("census", f"skipped — pinned to the full DB, and --slate {args.slate} was given")
        else:
            census_gate(report)
        check(
            "reporting wrote nothing to fpts_audit",
            audit_rows(conn) == audit_before,
            f"{audit_before} row(s) before and after",
        )

        print_report(report)

        if args.write:
            # Audit first: a crash between the two leaves the pristine dk_value
            # recoverable on the next run. See reconcile._pristine_dk_value.
            try:
                written = write_audit(conn, report.rows)
                updated = apply_corrections(conn, report.rows)
            except ReconcileError as exc:
                print(f"\nwrite refused: {exc}", file=sys.stderr)
                return 2
            print(f"\nWrote {written} fpts_audit row(s); updated {updated} actual_fpts value(s).")
            write_gate(conn)
            idempotency_gate(conn)
            print_audit(audit_rollup(conn))
        else:
            print_write_preview(report, "uv run python scripts/verify_phase6.py --write")
            print(
                "  It then re-checks the result against the database rather than against\n"
                "  the report above: the census both ways, that the UPDATE neither missed\n"
                "  a row nor over-reached, that actual_fpts has no NULLs, and that a\n"
                "  second full pass moves nothing.\n"
            )
            print("(Report only — nothing written.)")

        print("\nOps DB unchanged:")
        after = ops_counts(conn)
        check(
            "every ops table has the row count it started with",
            after == ops_before,
            _diff(ops_before, after) if after != ops_before else f"{len(after)} table(s)",
        )
    finally:
        conn.close()

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("All Phase 6 gate checks PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
