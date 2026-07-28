"""scripts/verify_phase3.py — discovery helpers and the `main()` gate flow.

The gate script is the artifact Jonny runs by hand. `salary_path` /
`loadable_slates` are the composition that turns a slate_id into real paths
(the pieces are only covered in isolation elsewhere), and the `main()` tests
drive the whole script against synthetic stand-ins so its PASS/FAIL output and
exit codes are pinned without needing the G:\\ drive.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from nba_dfs_stats_lab.ingest.lineups import latest_lineups_by_slate as real_latest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_phase3.py"

CSV_HEADER = (
    "Final_Rank,Lineup_Score,Total_Projection,Total_Ownership,Geomean_Ownership,"
    "Proj_Rank,Own_Rank,Geo_Rank,PG,SG,SF,PF,C,G,F,UTIL\n"
)
MANIFEST_HEADER = (
    "relabeled_name,original_name,slate_id,generated_at,is_latest_for_slate,n_lineups\n"
)


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location("verify_phase3", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_phase3"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["verify_phase3"]


def test_salary_path_composes_dir_and_filename(verify, monkeypatch, tmp_path):
    monkeypatch.setattr(verify, "SALARY_DIR", tmp_path)
    assert verify.salary_path("2026-05-18_classic_main") == tmp_path / "Main-2026-05-18.csv"
    # Type is explicit in salary filenames even when it isn't "main".
    assert verify.salary_path("2026-03-13_classic_night") == tmp_path / "Night-2026-03-13.csv"


def test_salary_path_rejects_malformed_slate_id(verify):
    with pytest.raises(ValueError, match="malformed slate_id"):
        verify.salary_path("2026-05-18_main")


def test_loadable_slates_keeps_only_slates_with_both_sources(verify, monkeypatch, tmp_path):
    salary_dir, rel = tmp_path / "salary", tmp_path / "relabeled"
    salary_dir.mkdir()
    rel.mkdir()

    # Two slates have lineups; only one of them has its salary CSV.
    rows = [
        ("ranked-lineups-2026-05-18.csv", "2026-05-18_classic_main", "2026-05-18T16:51:50", 2),
        ("ranked-lineups-2026-03-13.csv", "2026-03-13_classic_night", "2026-03-13T20:58:56", 2),
    ]
    for name, *_ in rows:
        (rel / name).write_text(CSV_HEADER)
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        MANIFEST_HEADER + "".join(f"{n},{n},{s},{g},True,{c}\n" for n, s, g, c in rows)
    )
    (salary_dir / "Main-2026-05-18.csv").write_text("DFS ID\n")

    monkeypatch.setattr(verify, "SALARY_DIR", salary_dir)
    monkeypatch.setattr(
        verify, "latest_lineups_by_slate", lambda: real_latest(manifest, rel)
    )

    available = verify.loadable_slates()
    assert set(available) == {"2026-05-18_classic_main"}
    assert available["2026-05-18_classic_main"].n_lineups == 2


# --- main() against synthetic stand-ins ---------------------------------------

SALARY_HEADER = "DFS ID,Name,Position,Team,Opponent,Salary,Actual_FPTs\n"
SLOT_IDS = tuple(range(100, 108))
GATE_SLATE = "2026-05-18_classic_main"


def _lineup_row(rank: int) -> str:
    slots = ",".join(f"Player {i} ({i})" for i in SLOT_IDS)
    return f"{rank},36.5,245.9,244.2,29.5,4.5,342.5,329.0,{slots}\n"


@pytest.fixture
def gate_env(tmp_path, verify, monkeypatch):
    """A complete synthetic slate wired into the script's module globals.

    Returns the paths so individual tests can corrupt one piece and assert the
    script degrades into a [FAIL] line rather than a traceback.
    """
    from nba_dfs_stats_lab.db.connection import get_connection

    salary_dir, rel = tmp_path / "salary", tmp_path / "relabeled"
    salary_dir.mkdir()
    rel.mkdir()

    salary_csv = salary_dir / "Main-2026-05-18.csv"
    salary_csv.write_text(
        SALARY_HEADER
        + "".join(f"{i},Player {i},PG/G/UTIL,BOS,NYK,{5000 + i},{i / 10}\n" for i in SLOT_IDS)
    )
    lineups_csv = rel / "ranked-lineups-2026-05-18.csv"
    lineups_csv.write_text(CSV_HEADER + _lineup_row(1) + _lineup_row(2))
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        MANIFEST_HEADER
        + f"{lineups_csv.name},orig.csv,{GATE_SLATE},2026-05-18T16:51:50,True,2\n"
    )

    db_path = tmp_path / "analytics.db"
    monkeypatch.setattr(verify, "SALARY_DIR", salary_dir)
    monkeypatch.setattr(verify, "latest_lineups_by_slate", lambda: real_latest(manifest, rel))
    monkeypatch.setattr(verify, "get_connection", lambda: get_connection(db_path))
    return {
        "salary_csv": salary_csv,
        "lineups_csv": lineups_csv,
        "manifest": manifest,
        "relabeled": rel,
        "db_path": db_path,
    }


def run_gate(verify, monkeypatch, argv=()):
    monkeypatch.setattr(sys, "argv", ["verify_phase3.py", *argv])
    verify._failures.clear()
    return verify.main()


def test_main_all_pass_on_a_clean_slate(verify, gate_env, monkeypatch, capsys):
    code = run_gate(verify, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    assert "[FAIL]" not in out
    assert "All gate checks passed." in out
    # The gates that make this slate meaningful actually ran.
    for label in (
        "salary ingested",
        "lineups ingested",
        "manifest n_lineups matches the file",
        "8 players per lineup",
        "no orphan rostered players",
        "no slot rows without a lineup header",
    ):
        assert f"[PASS] {label}" in out


def test_main_list_mode_exits_zero_without_ingesting(verify, gate_env, monkeypatch, capsys):
    code = run_gate(verify, monkeypatch, ["--list"])
    out = capsys.readouterr().out
    assert code == 0
    assert GATE_SLATE in out
    assert "[PASS]" not in out and "[FAIL]" not in out


def test_main_reports_validation_error_as_fail_not_traceback(
    verify, gate_env, monkeypatch, capsys
):
    # A malformed source file is expected, not exceptional: ingest raises
    # SlateValidationError, and the gate must turn it into a [FAIL] line.
    gate_env["lineups_csv"].write_text(
        (CSV_HEADER + _lineup_row(1)).replace("Player 103 (103)", "Player 103")
    )
    code = run_gate(verify, monkeypatch)
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] source file validates" in out
    assert "no single dk_id" in out


def test_main_fails_when_a_rostered_player_is_missing_from_salary(
    verify, gate_env, monkeypatch, capsys
):
    # An integrity gate failing (not an exception) must still exit 1.
    rows = [
        f"{i},Player {i},PG/G/UTIL,BOS,NYK,{5000 + i},{i / 10}\n" for i in SLOT_IDS[:-1]
    ]
    gate_env["salary_csv"].write_text(SALARY_HEADER + "".join(rows))
    code = run_gate(verify, monkeypatch)
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] no orphan rostered players" in out
    assert "gate check(s) FAILED" in out


def test_main_reports_missing_manifest_as_fail(verify, gate_env, monkeypatch, capsys):
    gate_env["manifest"].unlink()
    code = run_gate(verify, monkeypatch)
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] lineups discovery" in out
    assert "match_lineups_to_slates" in out


def test_main_reports_unmigratable_db_as_fail(verify, gate_env, monkeypatch, capsys):
    # A v1 DB still holding rows: migrate() refuses rather than dropping data.
    from nba_dfs_stats_lab.db.connection import get_connection

    conn = get_connection(gate_env["db_path"])
    conn.executescript(
        "CREATE TABLE lineups (slate_id TEXT, final_rank INTEGER, proj_rank INTEGER,"
        " own_rank REAL, geo_rank INTEGER, PRIMARY KEY (slate_id, final_rank));"
        "INSERT INTO lineups VALUES ('s1', 1, 4, 4.5, 3);"
    )
    conn.commit()
    conn.close()

    code = run_gate(verify, monkeypatch)
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] schema migration" in out
    assert "Delete data/analytics.db" in out
