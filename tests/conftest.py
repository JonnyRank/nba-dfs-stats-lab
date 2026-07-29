"""Shared synthetic source directories for the orchestrator and its gate script.

Phase 4 is the first phase whose unit under test spans all three sources, so the
fixture that builds a miniature version of the three source directories is used
by both `test_orchestrator.py` and `test_verify_phase4.py`. It lives here rather
than being imported across test modules.

The coverage mix deliberately mirrors the real data's asymmetry — most slates
have salary only, a few have all three — because "partial coverage is normal"
is the orchestrator's central behaviour.
"""

import pytest

from nba_dfs_stats_lab.ingest.lineups import latest_lineups_by_slate
from nba_dfs_stats_lab.ingest.orchestrator import discover_slates

SALARY_HEADER = "DFS ID,Name,Position,Team,Opponent,Salary,Actual_FPTs\n"
PROJ_HEADER = "ID,Player,Team,Opponent,Minutes,FPPM,Projection,Own_Proj\n"
LINEUPS_HEADER = (
    "Final_Rank,Lineup_Score,Total_Projection,Total_Ownership,Geomean_Ownership,"
    "Proj_Rank,Own_Rank,Geo_Rank,PG,SG,SF,PF,C,G,F,UTIL\n"
)
MANIFEST_HEADER = (
    "relabeled_name,original_name,slate_id,generated_at,is_latest_for_slate,n_lineups\n"
)

SLOT_IDS = tuple(range(100, 108))
MAIN_SLATE = "2026-05-18_classic_main"

DUPLICATE_DK_ID_SALARY = (
    SALARY_HEADER + "100,A,PG,BOS,NYK,5000,20\n100,B,SG,BOS,NYK,4000,10\n"
)



def salary_rows(null_team_ids: tuple[int, ...] = ()) -> str:
    """The 8 rostered players. `null_team_ids` blanks Team/Opponent on those rows.

    A blank Team/Opponent validates with a warning rather than failing — the real
    data has exactly that on 36 rows of one file. Keeping the full 8-player set
    means a warning fixture still satisfies the lineup-join invariants.
    """
    return "".join(
        f"{i},Player {i},PG/G/UTIL,{'' if i in null_team_ids else 'BOS'},"
        f"{'' if i in null_team_ids else 'NYK'},{5000 + i},{i / 10}\n"
        for i in SLOT_IDS
    )


# Valid, but with a null Team/Opponent on one row — loads with a warning.
NULL_TEAM_SALARY = SALARY_HEADER + salary_rows(null_team_ids=(SLOT_IDS[0],))


def projection_rows() -> str:
    return "".join(f"{i},Player {i},BOS,NYK,32.5,1.1,35.2,12.5\n" for i in SLOT_IDS)


def lineup_row(rank: int) -> str:
    slots = ",".join(f"Player {i} ({i})" for i in SLOT_IDS)
    return f"{rank},36.5,245.9,244.2,29.5,4.5,342.5,329.0,{slots}\n"


@pytest.fixture
def sources(tmp_path):
    """Three source dirs whose coverage mirrors the real data's asymmetry.

    - 2026-05-18 main:   all three sources
    - 2026-03-13 night:  salary + projections
    - 2026-01-04 late:   salary only — Jonny has no projections or lineups for
      any of the three Late-* files (confirmed 2026-07-28)
    - 2026-02-19 main:   projections only — no salary CSV exists for that date
    """
    salary_dir = tmp_path / "salary"
    proj_dir = tmp_path / "projections"
    rel_dir = tmp_path / "relabeled"
    for d in (salary_dir, proj_dir, rel_dir):
        d.mkdir()

    for name in ("Main-2026-05-18.csv", "Night-2026-03-13.csv", "Late-2026-01-04.csv"):
        (salary_dir / name).write_text(SALARY_HEADER + salary_rows())
    for name in (
        "NBA-Projs-2026-05-18.csv",
        "NBA-Projs-Night-2026-03-13.csv",
        "NBA-Projs-2026-02-19.csv",
    ):
        (proj_dir / name).write_text(PROJ_HEADER + projection_rows())

    lineups_csv = rel_dir / "ranked-lineups-2026-05-18.csv"
    lineups_csv.write_text(LINEUPS_HEADER + lineup_row(1) + lineup_row(2))
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        MANIFEST_HEADER + f"{lineups_csv.name},orig.csv,{MAIN_SLATE},2026-05-18T16:51:50,True,2\n"
    )

    return {
        "salary_dir": salary_dir,
        "proj_dir": proj_dir,
        "rel_dir": rel_dir,
        "manifest": manifest,
        "lineups_csv": lineups_csv,
    }


def rediscover(sources):
    """Discovery over the synthetic dirs — call again after mutating a file."""
    return discover_slates(
        sources["salary_dir"],
        sources["proj_dir"],
        latest_lineups_by_slate(sources["manifest"], sources["rel_dir"]),
    )


@pytest.fixture
def discovery(sources):
    return rediscover(sources)
