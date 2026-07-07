"""Filename parsing and slate_id construction — the bug-prone pure functions."""

import pytest

from nba_dfs_stats_lab.ingest.filenames import (
    build_slate_id,
    parse_lineups_filename,
    parse_projections_filename,
    parse_salary_filename,
)

# --- salary: <Type>-<YYYY-MM-DD>.csv (Type explicit, incl. Main) --------------


def test_salary_main_explicit():
    p = parse_salary_filename("Main-2026-02-28.csv")
    assert (p.date, p.slate_type, p.ts) == ("2026-02-28", "main", None)


@pytest.mark.parametrize("raw,expected", [
    ("Early", "early"), ("Turbo", "turbo"), ("Afternoon", "afternoon"),
    ("Night", "night"), ("MAIN", "main"),
])
def test_salary_type_normalized_lowercase(raw, expected):
    assert parse_salary_filename(f"{raw}-2026-01-15.csv").slate_type == expected


def test_salary_type_is_required():
    with pytest.raises(ValueError, match="does not match"):
        parse_salary_filename("2026-01-15.csv")


def test_salary_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown slate type"):
        parse_salary_filename("Showdown-2026-01-15.csv")


# --- projections: NBA-Projs-[<Type>-]<YYYY-MM-DD>.csv (absent => main) --------


def test_projections_type_absent_defaults_main():
    p = parse_projections_filename("NBA-Projs-2026-02-28.csv")
    assert (p.date, p.slate_type) == ("2026-02-28", "main")


def test_projections_type_explicit():
    p = parse_projections_filename("NBA-Projs-Turbo-2026-02-28.csv")
    assert (p.date, p.slate_type) == ("2026-02-28", "turbo")


def test_projections_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown slate type"):
        parse_projections_filename("NBA-Projs-Showdown-2026-02-28.csv")


def test_projections_wrong_prefix_rejected():
    with pytest.raises(ValueError, match="does not match"):
        parse_projections_filename("Projs-2026-02-28.csv")


# --- lineups: ranked-lineups-[<Type>-]<YYYY-MM-DD>[_<HHMMSS>].csv -------------


def test_lineups_bare_defaults_main_no_ts():
    p = parse_lineups_filename("ranked-lineups-2026-02-28.csv")
    assert (p.date, p.slate_type, p.ts) == ("2026-02-28", "main", None)


def test_lineups_with_type_and_ts():
    p = parse_lineups_filename("ranked-lineups-Night-2026-02-28_142530.csv")
    assert (p.date, p.slate_type, p.ts) == ("2026-02-28", "night", "142530")


def test_lineups_ts_without_type():
    p = parse_lineups_filename("ranked-lineups-2026-02-28_090100.csv")
    assert (p.date, p.slate_type, p.ts) == ("2026-02-28", "main", "090100")


def test_lineups_ts_sorts_for_keep_latest():
    # keep-latest compares the raw ts strings; zero-padded HHMMSS sorts correctly
    early = parse_lineups_filename("ranked-lineups-2026-02-28_091500.csv")
    late = parse_lineups_filename("ranked-lineups-2026-02-28_170000.csv")
    assert max(early.ts, late.ts) == late.ts


def test_lineups_malformed_ts_rejected():
    with pytest.raises(ValueError, match="does not match"):
        parse_lineups_filename("ranked-lineups-2026-02-28_9999.csv")


# --- shared validation ---------------------------------------------------------


def test_impossible_date_rejected():
    with pytest.raises(ValueError, match="invalid date"):
        parse_salary_filename("Main-2026-13-40.csv")


# --- build_slate_id -------------------------------------------------------------


def test_build_slate_id():
    assert build_slate_id("2026-02-28", "main") == "2026-02-28_classic_main"


def test_build_slate_id_lowercases_type():
    assert build_slate_id("2026-02-28", "Turbo") == "2026-02-28_classic_turbo"


def test_build_slate_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown slate type"):
        build_slate_id("2026-02-28", "showdown")


def test_build_slate_id_rejects_bad_date():
    with pytest.raises(ValueError):
        build_slate_id("2026-2-8", "main")
