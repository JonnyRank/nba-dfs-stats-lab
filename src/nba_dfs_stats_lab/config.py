from pathlib import Path

REPO_ROOT       = Path(__file__).resolve().parents[2]
DATA_DIR        = REPO_ROOT / "data"
ANALYTICS_DB    = DATA_DIR / "analytics.db"

# The G:\ paths below are Windows-only: on other platforms Path() keeps the
# backslashes as literal characters, so these constants resolve correctly only
# on the local (Windows) machine. Cloud/CI sessions must pass their own paths.
OPS_DB          = Path(r"G:\My Drive\Documents\bigdataball\bigdataball-backup\backup_nba_fantasy_logs.db")
PROJECTIONS_DIR = Path(r"G:\My Drive\Documents\CSV-Exports\projections")
SALARY_DIR      = Path(r"G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Classic-Slates")
LINEUPS_DIR     = Path(r"G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Classic-Ranked-Lineups")
