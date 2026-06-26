"""Smoke test — verifies the package installs and imports correctly."""
from pathlib import Path

import nba_dfs_stats_lab
from nba_dfs_stats_lab.config import ANALYTICS_DB, DATA_DIR, REPO_ROOT


def test_package_importable():
    assert nba_dfs_stats_lab is not None


def test_config_paths_are_path_objects():
    assert isinstance(REPO_ROOT, Path)
    assert isinstance(DATA_DIR, Path)
    assert isinstance(ANALYTICS_DB, Path)


def test_analytics_db_under_repo():
    assert ANALYTICS_DB == REPO_ROOT / "data" / "analytics.db"
