"""scripts/verify_phase6.py — the gate flow, driven against synthetic stand-ins.

The gate script is the artifact Jonny runs by hand, so what matters is that its
PASS/FAIL lines and exit codes are right — including on the failure paths, where
an uncaught exception would print a traceback instead of a verdict.

Two things this file has to work around, both deliberate in the script:

- the censuses are pinned to the real DB's figures, so they cannot pass against
  a fixture. The tests monkeypatch the three `EXPECTED_*` constants to the
  fixture's own numbers, then separately prove that a *drifted* census FAILs —
  which is the behaviour those checks exist for.
- the write half is exercised here rather than on the real DB. The Phase 6 gate
  is "show the change list before writing", so the real run stops at the report
  and the write is Jonny's call.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from nba_dfs_stats_lab.db.schema import init_db

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_phase6.py"

OPS_LOGS = [
    ("2026-05-18", 1, "Boston Celtics", "New York Knicks", 30.0),
    ("2026-05-18", 3, "New York Knicks", "Boston Celtics", 52.0),  # DK says 54.0
    ("2026-05-19", 4, "Oklahoma City Thunder", "San Antonio Spurs", 84.0),  # +1 day
    ("2026-05-18", 7, "Los Angeles Clippers", "Portland Trail Blazers", 52.0),
    ("2026-05-18", 8, "Portland Trail Blazers", "Los Angeles Clippers", 55.0),
]

TEAM_MAP = [
    ("Boston Celtics", "BOS"),
    ("New York Knicks", "NYK"),
    ("Oklahoma City Thunder", "OKC"),
    ("San Antonio Spurs", "SAS"),
    ("Los Angeles Clippers", "LAC"),
    ("Portland Trail Blazers", "POR"),
]

MAIN = "2026-05-18_classic_main"

SLATE_ROWS = [
    (1, "Exact Agree", "BOS", "NYK", 30.0),  # unchanged
    (3, "Stat Correction", "NYK", "BOS", 54.0),  # corrected
    (4, "Two Day Slate", "OKC", "SAS", 84.0),  # unchanged, date_shift
    (5, "Did Not Play", "OKC", "SAS", 0.0),  # no_ops_row / dnp
    (7, "Off Slate A", "LAC", "POR", 0.0),  # corrected, off_slate
    (8, "Off Slate B", "POR", "LAC", 0.0),  # corrected, off_slate
    (11, "Uncrosswalked", "BOS", "NYK", 0.0),  # unmapped
]

FIXTURE_ACTIONS = {"corrected": 3, "unchanged": 2, "no_ops_row": 1, "unmapped": 1}
FIXTURE_REASONS = {
    None: 1,
    "stat_correction": 1,
    "date_shift": 1,
    "dnp": 1,
    "off_slate": 2,
    "no_crosswalk": 1,
}
FIXTURE_OFF_SLATE = (2, 2)  # (rows flagged, of which corrected)


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location("verify_phase6", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_phase6"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["verify_phase6"]


def build_db(tmp_path, slate_rows=SLATE_ROWS, crosswalk=True):
    ops_path = tmp_path / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.executescript(
        """
        CREATE TABLE fantasy_logs (
          DATE TEXT, PLAYER_ID INTEGER, TEAM TEXT, OPPONENT TEXT,
          MINUTES REAL, DK_POINTS REAL);
        CREATE TABLE map_teams (RAW_TEAM_NAME TEXT, TEAM_ABBREVIATION TEXT);
        """
    )
    ops.executemany(
        "INSERT INTO fantasy_logs (DATE, PLAYER_ID, TEAM, OPPONENT, MINUTES, DK_POINTS) "
        "VALUES (?, ?, ?, ?, 20.0, ?)",
        OPS_LOGS,
    )
    ops.executemany("INSERT INTO map_teams VALUES (?, ?)", TEAM_MAP)
    ops.commit()
    ops.close()

    db_path = tmp_path / "analytics.db"
    conn = sqlite3.connect(db_path, uri=True)
    init_db(conn)
    conn.executemany(
        "INSERT INTO slate_players (slate_id, dk_id, name, team, opp, salary, actual_fpts) "
        "VALUES (?, ?, ?, ?, ?, 5000, ?)",
        [(MAIN, *row) for row in slate_rows],
    )
    if crosswalk:
        conn.executemany(
            "INSERT INTO dk_crosswalk (dk_id, player_id, display_name) VALUES (?, ?, ?)",
            [(dk, dk, name) for dk, name, *_ in slate_rows if dk != 11],
        )
    conn.commit()
    conn.close()
    return db_path, ops_path


@pytest.fixture
def gate(verify, monkeypatch, tmp_path):
    """Point the script at a temp analytics DB with a temp read-only ops DB."""
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    return verify


def _wire(verify, monkeypatch, db_path, ops_path, pin_fixture=True):
    def fake_connection():
        conn = sqlite3.connect(db_path, uri=True)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def fake_attach(conn, *args, **kwargs):
        conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))

    monkeypatch.setattr(verify, "get_connection", fake_connection)
    monkeypatch.setattr(verify, "attach_ops", fake_attach)
    if pin_fixture:
        monkeypatch.setattr(verify, "EXPECTED_ACTIONS", FIXTURE_ACTIONS)
        monkeypatch.setattr(verify, "EXPECTED_REASONS", FIXTURE_REASONS)
        monkeypatch.setattr(verify, "EXPECTED_OFF_SLATE_ROWS", FIXTURE_OFF_SLATE[0])
        monkeypatch.setattr(verify, "EXPECTED_OFF_SLATE_CORRECTED", FIXTURE_OFF_SLATE[1])
    return db_path


def read_db(db_path):
    conn = sqlite3.connect(db_path, uri=True)
    try:
        yield_rows = {
            "fpts": dict(conn.execute("SELECT dk_id, actual_fpts FROM slate_players")),
            "audit": dict(conn.execute("SELECT dk_id, action FROM fpts_audit")),
        }
    finally:
        conn.close()
    return yield_rows


# --- The report-only stage -----------------------------------------------------


def test_report_only_run_passes_and_writes_nothing(gate, capsys, tmp_path):
    assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "All Phase 6 gate checks PASSED." in out
    assert "FAIL" not in out
    assert "Report only" in out
    state = read_db(tmp_path / "analytics.db")
    assert state["audit"] == {}
    assert state["fpts"][3] == 54.0  # the correction was computed, not applied


def test_the_probe_write_to_ops_is_rejected(gate, capsys):
    gate.main([])
    out = capsys.readouterr().out
    assert "[PASS] probe write to ops is rejected" in out


def test_the_shifted_game_is_reported(gate, capsys):
    gate.main([])
    out = capsys.readouterr().out
    assert "2026-05-18 -> 2026-05-19" in out


def test_an_empty_slate_players_is_a_verdict_not_a_traceback(verify, monkeypatch, tmp_path):
    db_path, ops_path = build_db(tmp_path, slate_rows=[])
    _wire(verify, monkeypatch, db_path, ops_path)
    assert verify.main([]) == 1


def test_an_empty_crosswalk_is_a_verdict_not_a_traceback(verify, monkeypatch, tmp_path, capsys):
    db_path, ops_path = build_db(tmp_path, crosswalk=False)
    _wire(verify, monkeypatch, db_path, ops_path)
    assert verify.main([]) == 1
    assert "dk_crosswalk is empty" in capsys.readouterr().err


def test_a_missing_ops_table_fails_rather_than_raising(verify, monkeypatch, tmp_path, capsys):
    db_path, ops_path = build_db(tmp_path)
    ops = sqlite3.connect(ops_path)
    ops.execute("DROP TABLE map_teams")
    ops.commit()
    ops.close()
    _wire(verify, monkeypatch, db_path, ops_path)

    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] ops.map_teams is readable and non-empty" in out
    assert "FAILED (" in out


def test_an_unmapped_team_abbreviation_fails(verify, monkeypatch, tmp_path, capsys):
    # A team missing from map_teams resolves no game-side, so every one of its
    # players is silently classified as a DNP. That has to be loud.
    db_path, ops_path = build_db(tmp_path)
    conn = sqlite3.connect(db_path, uri=True)
    conn.execute("UPDATE slate_players SET team = 'ZZZ' WHERE dk_id = 1")
    conn.commit()
    conn.close()
    _wire(verify, monkeypatch, db_path, ops_path)

    assert verify.main([]) == 1
    assert "[FAIL] every slate_players team is in ops.map_teams" in capsys.readouterr().out


# --- The censuses: they must be able to FAIL -----------------------------------


def test_a_drifted_action_census_fails(verify, monkeypatch, tmp_path, capsys):
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    monkeypatch.setattr(verify, "EXPECTED_ACTIONS", {**FIXTURE_ACTIONS, "corrected": 999})

    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] the action census matches the pinned figures" in out
    assert "expected 999" in out


def test_a_drifted_reason_census_fails(verify, monkeypatch, tmp_path, capsys):
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    monkeypatch.setattr(verify, "EXPECTED_REASONS", {**FIXTURE_REASONS, "dk_unscored": 61})

    assert verify.main([]) == 1
    assert "[FAIL] the reason census matches the pinned figures" in capsys.readouterr().out


def test_a_drifted_off_slate_population_fails(verify, monkeypatch, tmp_path, capsys):
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    monkeypatch.setattr(verify, "EXPECTED_OFF_SLATE_ROWS", 209)

    assert verify.main([]) == 1
    assert "[FAIL] the off-slate population" in capsys.readouterr().out


def test_slate_scoping_skips_the_pinned_census(gate, capsys):
    # The censuses are pinned to the whole DB, so a --slate run must not pretend
    # to check them — it says so rather than failing or passing vacuously.
    assert gate.main(["--slate", MAIN]) == 0
    assert "[note] census — skipped" in capsys.readouterr().out


# --- The write stage -----------------------------------------------------------


def test_write_applies_the_corrections_and_passes_every_check(gate, capsys, tmp_path):
    assert gate.main(["--write"]) == 0
    out = capsys.readouterr().out
    assert "All Phase 6 gate checks PASSED." in out
    assert "FAIL" not in out

    state = read_db(tmp_path / "analytics.db")
    assert state["fpts"][3] == 52.0  # the stat correction landed
    assert state["fpts"][7] == 52.0  # the off-slate box score landed
    assert state["fpts"][1] == 30.0  # already agreed
    assert state["fpts"][5] == 0.0  # DNP zero stays 0
    assert state["fpts"][11] == 0.0  # uncrosswalked, unreachable
    assert len(state["audit"]) == len(SLATE_ROWS)  # a full census


def test_write_is_idempotent_across_two_invocations(gate, capsys, tmp_path):
    assert gate.main(["--write"]) == 0
    first = read_db(tmp_path / "analytics.db")
    capsys.readouterr()

    assert gate.main(["--write"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert read_db(tmp_path / "analytics.db") == first

    conn = sqlite3.connect(tmp_path / "analytics.db", uri=True)
    # The pristine DK value is still 54.0 after two full passes, not the 52.0
    # now sitting in slate_players.
    assert conn.execute("SELECT dk_value FROM fpts_audit WHERE dk_id = 3").fetchone()[0] == 54.0
    conn.close()


def test_a_stale_audit_row_fails_the_census_check(verify, monkeypatch, tmp_path, capsys):
    # An audit row with no slate_players row behind it — what a census that only
    # ever upserted would accumulate when a slate is re-ingested with fewer
    # players. It breaks "COUNT(fpts_audit) == COUNT(slate_players)", which is
    # the property that makes the census self-verifying.
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    assert verify.main(["--write"]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(db_path, uri=True)
    conn.execute(
        "INSERT INTO fpts_audit (slate_id, dk_id, dk_value, action, off_slate) "
        "VALUES (?, 999, 0.0, 'unchanged', 0)",
        (MAIN,),
    )
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))
    verify._failures.clear()
    verify.write_gate(conn)
    conn.close()
    out = capsys.readouterr().out
    assert "[FAIL] every slate_players row has exactly one fpts_audit row" in out
    assert "1 orphan audit row(s)" in out


def test_write_audit_clears_the_stale_row_on_the_next_pass(verify, monkeypatch, tmp_path, capsys):
    # ...and the delete-then-insert in write_audit is what removes it, so the
    # gate above only ever fires on a genuinely broken write path.
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    verify.main(["--write"])
    conn = sqlite3.connect(db_path, uri=True)
    conn.execute(
        "INSERT INTO fpts_audit (slate_id, dk_id, dk_value, action, off_slate) "
        "VALUES (?, 999, 0.0, 'unchanged', 0)",
        (MAIN,),
    )
    conn.commit()
    conn.close()
    capsys.readouterr()

    assert verify.main(["--write"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_an_applied_correction_that_gets_reverted_is_caught(verify, monkeypatch, tmp_path, capsys):
    # Simulates the UPDATE missing a row: the audit says corrected, the table
    # still holds DK's value. Check 6 exists for exactly this.
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    assert verify.main(["--write"]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(db_path, uri=True)
    conn.execute("UPDATE slate_players SET actual_fpts = 54.0 WHERE dk_id = 3")
    conn.commit()
    conn.close()

    verify._failures.clear()
    conn = sqlite3.connect(db_path, uri=True)
    conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))
    verify.write_gate(conn)
    conn.close()
    out = capsys.readouterr().out
    assert "[FAIL] every corrected row's actual_fpts now equals its ops_value" in out


def test_a_null_actual_fpts_fails_the_no_nulls_check(verify, monkeypatch, tmp_path, capsys):
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    verify.main(["--write"])
    capsys.readouterr()

    conn = sqlite3.connect(db_path, uri=True)
    conn.execute("UPDATE slate_players SET actual_fpts = NULL WHERE dk_id = 5")
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))
    verify._failures.clear()
    verify.write_gate(conn)
    conn.close()
    assert "[FAIL] actual_fpts has no NULLs (D2)" in capsys.readouterr().out


def test_a_matchup_split_across_two_game_dates_fails(verify, monkeypatch, tmp_path, capsys):
    # The 907-row trap, injected: one player of a matchup on a different date
    # than his team-mates. dk_ids 4 and 5 are both OKC/SAS, whose game resolves
    # to 2026-05-19 as a whole. Nothing in this codebase produces a split — the
    # check exists so a future refactor that reintroduces per-player date logic
    # is caught rather than silently mis-attributing scores.
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)
    verify.main(["--write"])
    capsys.readouterr()

    conn = sqlite3.connect(db_path, uri=True)
    conn.execute("UPDATE fpts_audit SET game_date = '2026-05-18' WHERE dk_id = 5")
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))
    verify._failures.clear()
    verify.write_gate(conn)
    conn.close()
    assert "[FAIL] no stored matchup carries two game_dates" in capsys.readouterr().out


def test_failures_do_not_leak_between_runs(gate, capsys, tmp_path):
    # `_failures` is module state; a run that inherited an earlier run's
    # failures would return 1 on clean data and make the suite order-dependent.
    gate._failures.append("a failure from somewhere else")
    assert gate.main([]) == 0
    assert "All Phase 6 gate checks PASSED." in capsys.readouterr().out


# --- PR #11 review round -------------------------------------------------------


def test_write_with_slate_is_refused_and_writes_nothing(verify, monkeypatch, tmp_path, capsys):
    # The gate's checks and its idempotency re-run are whole-DB, so honouring
    # --slate here would write every slate while the caller asked for one.
    db_path, ops_path = build_db(tmp_path)
    _wire(verify, monkeypatch, db_path, ops_path)

    assert verify.main(["--write", "--slate", MAIN]) == 2
    err = capsys.readouterr().err
    assert "--write cannot be combined with --slate" in err
    assert "ingest.reconcile --write --slate" in err
    assert read_db(db_path)["audit"] == {}  # nothing landed


def test_a_second_slate_is_untouched_by_a_scoped_module_write(tmp_path):
    # The counterpart: the module CLI *does* scope correctly, which is where
    # the refusal above points. Proven at the API level.
    from nba_dfs_stats_lab.ingest.reconcile import apply_corrections, reconcile, write_audit

    db_path, ops_path = build_db(tmp_path)
    conn = sqlite3.connect(db_path, uri=True)
    conn.execute(
        "INSERT INTO slate_players (slate_id, dk_id, name, team, opp, salary, actual_fpts) "
        "VALUES ('2026-05-20_classic_main', 3, 'Stat Correction', 'NYK', 'BOS', 5000, 54.0)"
    )
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))

    report = reconcile(conn, slate_ids=[MAIN])
    write_audit(conn, report.rows)
    apply_corrections(conn, report.rows)

    assert {s for (s,) in conn.execute("SELECT DISTINCT slate_id FROM fpts_audit")} == {MAIN}
    other = conn.execute(
        "SELECT actual_fpts FROM slate_players WHERE slate_id = '2026-05-20_classic_main'"
    ).fetchone()[0]
    assert other == 54.0  # untouched
    conn.close()


def test_a_correction_on_a_shifted_game_fails(verify, monkeypatch, tmp_path, capsys):
    # A postponement looks identical to the resolver — right matchup, wrong
    # game — and surfaces as a correction on a shifted date. Today the shifted
    # rows all already agree, which is the evidence the resolver matched the
    # right game; this makes that evidence a check.
    rows = [r for r in SLATE_ROWS if r[0] != 4]
    rows.append((4, "Two Day Slate", "OKC", "SAS", 70.0))  # ops has 84.0 at +1
    db_path, ops_path = build_db(tmp_path, slate_rows=rows)
    _wire(verify, monkeypatch, db_path, ops_path, pin_fixture=False)

    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] no correction lands on a date-shifted game" in out
    assert "2026-05-18->2026-05-19" in out


def test_the_shifted_check_passes_when_shifted_rows_agree(gate, capsys):
    assert gate.main([]) == 0
    assert "[PASS] no correction lands on a date-shifted game" in capsys.readouterr().out
