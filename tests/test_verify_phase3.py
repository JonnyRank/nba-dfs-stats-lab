"""Discovery helpers in scripts/verify_phase3.py.

The gate script is the artifact Jonny runs by hand, and `salary_path` /
`loadable_slates` are the composition that turns a slate_id into real paths —
tested here because the pieces (`salary_filename`, `latest_lineups_by_slate`)
are only covered in isolation.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from nba_dfs_stats_lab.ingest.lineups import latest_lineups_by_slate as real_latest

SCRIPT =Path(__file__).resolve().parents[1] / "scripts" / "verify_phase3.py"

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
