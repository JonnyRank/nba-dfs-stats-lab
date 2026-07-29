"""Salary source: validate/normalize contract and end-to-end ingest."""

import io

import pandas as pd
import pytest

from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.salary import (
    ingest_salary,
    normalize_salary,
    read_salary,
    validate_salary,
)
from nba_dfs_stats_lab.ingest.schemas import SlateValidationError

# The real file is fully quoted, so the numeric columns arrive as strings.
CSV_HEADER = '"DFS ID","Name","Position","Team","Opponent","Salary","Actual_FPTs"\n'
CSV_ROWS = (
    '"43090603","Victor Wembanyama","C/UTIL","SAS","OKC","10800","84"\n'
    '"43090605","Shai Gilgeous-Alexander","PG/G/UTIL","OKC","SAS","10000","58.25"\n'
    '"43090637","De\'Aaron Fox","PG/G/UTIL","SAS","OKC","6200","31.5"\n'
)
SLATE_ID = "2026-05-18_classic_main"


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "Main-2026-05-18.csv"
    p.write_text(CSV_HEADER + CSV_ROWS)
    return p


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path / "analytics.db")
    init_db(conn)
    yield conn
    conn.close()


# --- validate -----------------------------------------------------------------


def test_valid_file_passes(csv_path):
    report = validate_salary(read_salary(csv_path))
    assert report.ok
    assert report.row_count == 3
    assert report.errors == []


def test_missing_required_column_fails(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text('"DFS ID","Name","Position","Team","Opponent","Salary"\n"1","A","PG","X","Y","5000"\n')
    report = validate_salary(read_salary(p))
    assert not report.ok
    assert any("Actual_FPTs" in e for e in report.errors)


def test_duplicate_dk_id_fails(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000","20"\n"1","B","SG","X","Y","4000","10"\n')
    report = validate_salary(read_salary(p))
    assert not report.ok
    assert any("duplicate" in e for e in report.errors)


def test_missing_team_warns_but_passes(tmp_path):
    # 36 rows across the real 409 files have no Team/Opponent — nullable.
    p = tmp_path / "noteam.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","","","5000","20"\n')
    report = validate_salary(read_salary(p))
    assert report.ok
    assert any("Team" in w for w in report.warnings)


def test_unplayed_slate_passes(tmp_path):
    # Actual_FPTs is empty until the slate is played — a warning, not an error.
    p = tmp_path / "unplayed.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000",\n')
    report = validate_salary(read_salary(p))
    assert report.ok
    assert any("Actual_FPTs" in w for w in report.warnings)


def test_garbled_salary_fails(tmp_path):
    p = tmp_path / "garbled.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","five thousand","20"\n')
    report = validate_salary(read_salary(p))
    assert not report.ok
    assert any("Salary" in e and "non-numeric" in e for e in report.errors)


# --- normalize ----------------------------------------------------------------


def test_normalize_shape_and_types(csv_path):
    out = normalize_salary(read_salary(csv_path), SLATE_ID)
    assert list(out.columns) == [
        "slate_id", "dk_id", "name", "positions", "team", "opp", "salary", "actual_fpts",
    ]
    assert (out["slate_id"] == SLATE_ID).all()
    assert out["dk_id"].tolist() == [43090603, 43090605, 43090637]
    assert out["salary"].dtype == "Int64"  # quoted strings coerced to ints
    assert out["positions"][1] == "PG/G/UTIL"  # kept raw, not split
    assert out["actual_fpts"][1] == 58.25


# --- ingest (end-to-end) ------------------------------------------------------


def test_ingest_writes_rows(csv_path, conn):
    assert ingest_salary(csv_path, SLATE_ID, conn) == 3
    row = conn.execute(
        "SELECT name, positions, team, salary, actual_fpts FROM slate_players WHERE dk_id = ?",
        (43090603,),
    ).fetchone()
    assert row == ("Victor Wembanyama", "C/UTIL", "SAS", 10800, 84.0)


def test_ingest_reload_idempotent(csv_path, conn):
    ingest_salary(csv_path, SLATE_ID, conn)
    ingest_salary(csv_path, SLATE_ID, conn)
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 3


def test_ingest_unplayed_slate_writes_null_actuals(tmp_path, conn):
    p = tmp_path / "unplayed.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000",\n')
    assert ingest_salary(p, SLATE_ID, conn) == 1
    assert conn.execute("SELECT actual_fpts FROM slate_players").fetchone()[0] is None


# --- off-slate games (every player on both sides at 0) ------------------------
#
# The generic contract cannot catch these: the cells hold `0`, not blank, and
# across all 409 real files there is not one blank Actual_FPTs. See the docstring
# on `check_zero_scored_games`.


def _player(dk_id: int, team: str, opp: str, fpts: str) -> str:
    return f"{dk_id},Player {dk_id},PG/G/UTIL,{team},{opp},5000,{fpts}\n"


def _game(start: int, home: str, away: str, fpts: list[str]) -> str:
    """Two rows per side, one line each, with the given actuals in order."""
    return "".join(
        _player(start + i, home if i < 2 else away, away if i < 2 else home, fpts[i])
        for i in range(4)
    )


PLAYED_GAME = _game(200, "BOS", "NYK", ["42.5", "0", "31.25", "18"])
OFF_SLATE_GAME = _game(300, "LAC", "POR", ["0", "0", "0", "0"])


def zero_game_warnings(csv_text: str) -> list[str]:
    report = validate_salary(pd.read_csv(io.StringIO(csv_text)))
    assert report.ok, report.errors  # never blocks the load
    return [w for w in report.warnings if "scored 0" in w or "every team" in w]


def test_both_sides_at_zero_warns():
    warnings = zero_game_warnings(CSV_HEADER + PLAYED_GAME + OFF_SLATE_GAME)
    assert len(warnings) == 1
    assert "1 game(s)" in warnings[0]
    assert "LAC vs POR (4 players)" in warnings[0]


def test_a_real_zero_alongside_real_scores_does_not_warn():
    # BOS has a 0 in PLAYED_GAME — a DNP or a garbage-time line, and common:
    # the unaffected teams on 2026-02-02 carry 6 to 9 each.
    assert zero_game_warnings(CSV_HEADER + PLAYED_GAME) == []


def test_one_side_at_zero_does_not_warn():
    # A blowout where one team's rostered players all sat is implausible but
    # possible; both sides scoring nothing is not. Only the pair is the signal.
    lopsided = _game(400, "MEM", "PHI", ["0", "0", "44.5", "22.25"])
    assert zero_game_warnings(CSV_HEADER + lopsided) == []


def test_a_third_zeroed_team_does_not_invent_a_game():
    """`other` being zeroed is not enough — the pairing has to reciprocate.

    LAC/POR are genuinely off-slate. SAC is also all-zero and names POR, but
    POR names LAC. Without a reciprocity check "POR vs SAC" is reported as a
    game that never existed, and POR's roster is counted into two games at once.
    """
    stray = _player(600, "SAC", "POR", "0") + _player(601, "SAC", "POR", "0")
    warnings = zero_game_warnings(CSV_HEADER + PLAYED_GAME + OFF_SLATE_GAME + stray)
    assert len(warnings) == 1
    assert "1 game(s)" in warnings[0]
    assert "LAC vs POR (4 players)" in warnings[0]
    assert "SAC" not in warnings[0]


def test_a_team_with_two_opponents_is_not_rolled_up():
    # Every row for a team carries the same Opponent on a real slate, so two
    # means the file disagrees with itself. Counting its roster into both games
    # would report more players than the file has rows.
    contradictory = (
        _player(700, "LAL", "GSW", "0")
        + _player(701, "LAL", "SAS", "0")
        + _player(702, "GSW", "LAL", "0")
        + _player(703, "SAS", "LAL", "0")
    )
    assert zero_game_warnings(CSV_HEADER + PLAYED_GAME + contradictory) == []


def test_two_off_slate_games_are_both_reported():
    second = _game(500, "DAL", "MIL", ["0", "0", "0", "0"])
    warnings = zero_game_warnings(CSV_HEADER + PLAYED_GAME + OFF_SLATE_GAME + second)
    assert "2 game(s)" in warnings[0]
    assert "DAL vs MIL" in warnings[0] and "LAC vs POR" in warnings[0]


def test_whole_slate_at_zero_reports_once():
    # Early-2025-12-07 is in exactly this state — 68 rows, no results recorded.
    # Reported as one slate-level fact rather than a line per game.
    warnings = zero_game_warnings(CSV_HEADER + OFF_SLATE_GAME)
    assert len(warnings) == 1
    assert "every team on the slate scored 0 across all 4 rows" in warnings[0]


def test_unplayed_slate_is_not_an_off_slate_game():
    # All actuals NULL means "not played yet", a different state entirely. NaN
    # never equals 0, so it must not be swept in here — validate_frame already
    # warns about the missing values.
    unplayed = _game(600, "LAC", "POR", ["", "", "", ""])
    report = validate_salary(pd.read_csv(io.StringIO(CSV_HEADER + unplayed)))
    assert report.ok
    assert not any("scored 0" in w or "every team" in w for w in report.warnings)
    assert any("Actual_FPTs" in w for w in report.warnings)


def test_blank_team_rows_do_not_form_a_phantom_team():
    # 36 rows of one real file had no Team/Opponent. They can't be attributed to
    # a game, so they must not group into a "" team that then reads as zeroed.
    blanks = _player(700, "", "", "0") + _player(701, "", "", "0")
    assert zero_game_warnings(CSV_HEADER + PLAYED_GAME + blanks) == []


def test_missing_team_column_does_not_crash_the_check():
    # validate_frame already errors on the missing column; the check must return
    # rather than KeyError past it.
    no_team = '"DFS ID","Name","Position","Opponent","Salary","Actual_FPTs"\n"1","A","PG","NYK","5000","0"\n'
    report = validate_salary(pd.read_csv(io.StringIO(no_team)))
    assert not report.ok
    assert any("Team" in e for e in report.errors)


def test_off_slate_rows_still_load(tmp_path, conn):
    # A warning, not an error: surface, don't drop. The rows are otherwise valid
    # and the exclusion decision belongs to the ops-reconciliation pass.
    p = tmp_path / "Main-2026-02-02.csv"
    p.write_text(CSV_HEADER + PLAYED_GAME + OFF_SLATE_GAME)
    assert ingest_salary(p, SLATE_ID, conn) == 8
    zeros = conn.execute(
        "SELECT COUNT(*) FROM slate_players WHERE team IN ('LAC','POR')"
    ).fetchone()[0]
    assert zeros == 4


def test_ingest_invalid_writes_nothing(tmp_path, conn):
    p = tmp_path / "dup.csv"
    p.write_text(CSV_HEADER + '"1","A","PG","X","Y","5000","20"\n"1","B","SG","X","Y","4000","10"\n')
    with pytest.raises(SlateValidationError) as exc_info:
        ingest_salary(p, SLATE_ID, conn)
    assert not exc_info.value.report.ok
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0
