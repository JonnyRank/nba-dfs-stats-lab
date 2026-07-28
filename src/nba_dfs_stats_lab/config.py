from pathlib import Path

REPO_ROOT       = Path(__file__).resolve().parents[2]
DATA_DIR        = REPO_ROOT / "data"
ANALYTICS_DB    = DATA_DIR / "analytics.db"

# The G:\ paths below are Windows-only: on other platforms Path() keeps the
# backslashes as literal characters, so these constants resolve correctly only
# on the local (Windows) machine. Cloud/CI sessions must pass their own paths.
OPS_DB          = Path(r"G:\My Drive\Documents\bigdataball\ops_snapshot_nba_fantasy_logs.db")
PROJECTIONS_DIR = Path(r"G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Projs-CSVs")
SALARY_DIR      = Path(r"G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Classic-Slates")
LINEUPS_DIR     = Path(r"G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Classic-Ranked-Lineups")

# Ingest reads lineups from the reconciled copies, NOT from LINEUPS_DIR: the
# optimizer named files by run time, so 26 dates and 5 slate types in
# LINEUPS_DIR are wrong. scripts/match_lineups_to_slates.py rebuilds these from
# LINEUPS_DIR by DK-ID match; manifest.csv carries the true generation time,
# which is what keep-latest must select on (the _HHMMSS suffix no longer works).
LINEUPS_MATCH_DIR     = DATA_DIR / "lineups_slate_match"
LINEUPS_RELABELED_DIR = LINEUPS_MATCH_DIR / "relabeled"
LINEUPS_MANIFEST      = LINEUPS_MATCH_DIR / "manifest.csv"
