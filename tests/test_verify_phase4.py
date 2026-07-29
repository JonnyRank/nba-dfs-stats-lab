"""scripts/verify_phase4.py — the gate flow, driven against synthetic stand-ins.

The gate script is the artifact Jonny runs by hand, so what matters is that its
PASS/FAIL lines and exit codes are right — including on the failure paths, where
an uncaught exception would print a traceback instead of a verdict. These tests
reuse the synthetic source directories from conftest.py so the whole file runs
without the G:\\ drive.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from conftest import DUPLICATE_DK_ID_SALARY, MAIN_SLATE, rediscover
from nba_dfs_stats_lab.db.connection import get_connection

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_phase4.py"


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location("verify_phase4", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_phase4"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["verify_phase4"]


@pytest.fixture
def gate(verify, monkeypatch, discovery, tmp_path):
    db_path = tmp_path / "gate.db"
    monkeypatch.setattr(verify, "discover_slates", lambda: discovery)
    monkeypatch.setattr(verify, "get_connection", lambda: get_connection(db_path))
    return db_path


def rebind(verify, monkeypatch, sources):
    """Re-point the script's discovery after a test has mutated the source dirs."""
    d = rediscover(sources)
    monkeypatch.setattr(verify, "discover_slates", lambda: d)
    return d


def test_dry_run_gate_passes_and_writes_nothing(verify, gate, capsys):
    assert verify.main([]) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "All gate checks passed." in out
    for label in (
        "every source CSV resolved to a slate",
        "all three sources discovered",
        "no lineups slate is missing its salary CSV",
        "dry run wrote nothing",
        "every sampled slate validates",
        "the sample actually read files",
    ):
        assert f"[PASS] {label}" in out
    # The default run must not have loaded anything.
    assert "Re-run with --backfill" in out
    conn = get_connection(gate)
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0
    conn.close()


def test_dry_run_gate_reports_the_projections_only_slate_as_a_note(verify, gate, capsys):
    verify.main([])
    out = capsys.readouterr().out
    # A projections file with no salary CSV is a real data gap, but not a gate
    # failure — three real dates are in exactly this state.
    assert "[note] projections with no salary CSV" in out
    assert "2026-02-19_classic_main" in out


def test_backfill_gate_loads_and_passes_integrity(verify, gate, capsys):
    assert verify.main(["--backfill"]) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    for label in (
        "backfill completed with no failures",
        "every table holds exactly what the backfill wrote",
        "8 players per lineup everywhere",
        "no orphan rostered players",
        "no slot rows without a lineup header",
        "every lineup has its players",
        "re-ingest changed no row count",
    ):
        assert f"[PASS] {label}" in out

    conn = get_connection(gate)
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 24
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 16
    conn.close()


def test_backfill_is_skipped_when_the_dry_run_gate_failed(
    verify, gate, sources, monkeypatch, capsys
):
    (sources["salary_dir"] / "Main-2026-05-18.csv").write_text(DUPLICATE_DK_ID_SALARY)
    rebind(verify, monkeypatch, sources)
    assert verify.main(["--backfill"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] every sampled slate validates" in out
    assert "Skipping the backfill" in out
    # Nothing was written: the whole point of gating the backfill on the dry run.
    conn = get_connection(gate)
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0
    conn.close()


def test_unresolved_source_file_fails_the_gate(
    verify, gate, sources, monkeypatch, capsys
):
    (sources["salary_dir"] / "Bogus-2026-05-19.csv").write_text("x\n")
    rebind(verify, monkeypatch, sources)
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] every source CSV resolved to a slate" in out
    assert "Bogus-2026-05-19.csv" in out


def test_lineups_without_salary_fails_the_gate(
    verify, gate, sources, monkeypatch, capsys
):
    # This is the wrong-slate-write tripwire: a lineups file whose slate has no
    # salary CSV would be skipped at ingest, so the gate must say so up front.
    (sources["salary_dir"] / "Main-2026-05-18.csv").unlink()
    rebind(verify, monkeypatch, sources)
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] no lineups slate is missing its salary CSV" in out
    assert MAIN_SLATE in out


def test_discovery_failure_is_a_fail_line_not_a_traceback(verify, gate, monkeypatch, capsys):
    def boom():
        raise FileNotFoundError(
            "lineups manifest not found ... uv run python scripts/match_lineups_to_slates.py"
        )

    monkeypatch.setattr(verify, "discover_slates", boom)
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] discovery" in out
    assert "match_lineups_to_slates" in out


def test_empty_discovery_fails_rather_than_passing_vacuously(verify, gate, monkeypatch, capsys):
    from nba_dfs_stats_lab.ingest.orchestrator import Discovery

    monkeypatch.setattr(verify, "discover_slates", lambda: Discovery(slates={}))
    assert verify.main([]) == 1
    assert "[FAIL] discovery found slates" in capsys.readouterr().out


def test_unmigratable_db_is_a_fail_line(verify, gate, capsys):
    # A v1 DB still holding rows: migrate() refuses rather than dropping data.
    conn = get_connection(gate)
    conn.executescript(
        "CREATE TABLE lineups (slate_id TEXT, final_rank INTEGER, proj_rank INTEGER,"
        " own_rank REAL, geo_rank INTEGER, PRIMARY KEY (slate_id, final_rank));"
        "INSERT INTO lineups VALUES ('s1', 1, 4, 4.5, 3);"
    )
    conn.commit()
    conn.close()

    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] schema migration" in out
    assert "Delete data/analytics.db" in out


def test_main_clears_failures_between_runs(verify, gate, sources, monkeypatch, capsys):
    """`_failures` is module-level, so `main()` has to reset it itself.

    Without this a second call in the same process inherits the first run's
    failures — skipping the backfill and returning 1 on a clean run.
    """
    (sources["salary_dir"] / "Bogus-2026-05-19.csv").write_text("x\n")
    rebind(verify, monkeypatch, sources)
    assert verify.main([]) == 1

    (sources["salary_dir"] / "Bogus-2026-05-19.csv").unlink()
    rebind(verify, monkeypatch, sources)
    assert verify.main([]) == 0
    assert "All gate checks passed." in capsys.readouterr().out


def test_sample_below_one_is_rejected(verify, gate):
    # --sample 0 samples nothing, so every dry-run check would pass vacuously —
    # including "the sample actually read files" (0 == 0).
    with pytest.raises(SystemExit) as exc:
        verify.main(["--sample", "0"])
    assert exc.value.code == 2


def test_backfill_gate_names_stale_slates(verify, gate, sources, monkeypatch, capsys):
    """A row-count mismatch must name what disagrees, not just dump the counts."""
    # A slate loaded from an older manifest that discovery no longer finds.
    conn = get_connection(gate)
    from nba_dfs_stats_lab.db.schema import init_db

    init_db(conn)
    conn.execute(
        "INSERT INTO slate_players (slate_id, dk_id, name) VALUES (?, ?, ?)",
        ("2026-01-01_classic_main", 999, "Ghost"),
    )
    conn.commit()
    conn.close()

    assert verify.main(["--backfill"]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] every table holds exactly what the backfill wrote" in out
    assert "2026-01-01_classic_main" in out
    assert "not in discovery" in out


def test_backfill_gate_reports_warnings(verify, gate, sources, monkeypatch, capsys):
    from conftest import NULL_TEAM_SALARY

    (sources["salary_dir"] / "Main-2026-05-18.csv").write_text(NULL_TEAM_SALARY)
    rebind(verify, monkeypatch, sources)
    assert verify.main(["--backfill"]) == 0
    out = capsys.readouterr().out
    assert "validation warning(s):" in out
    assert "Team: 1 missing value(s)" in out


def test_sample_covers_every_coverage_combination(verify, discovery):
    picked = verify.sample_slates(discovery, 1)
    covered = {discovery.slates[s].coverage for s in picked}
    assert covered == set(discovery.coverage_counts())
    assert len(picked) == len(covered)
