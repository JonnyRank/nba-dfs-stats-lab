"""Orchestrator: cross-source discovery, per-slate dispatch, dry run, backfill.

Everything here runs against the synthetic source directories built by the
`sources` / `discovery` fixtures in conftest.py, so the whole module is
meaningful without the G:\\ drive. The cases worth pinning are the ones the real
data forces: partial coverage is normal, one bad file must not abort a backfill,
and a dry run must not touch the DB.
"""

import pytest

from nba_dfs_stats_lab.db.connection import get_connection
from nba_dfs_stats_lab.db.schema import init_db
from nba_dfs_stats_lab.ingest.orchestrator import (
    SlateSources,
    Status,
    backfill,
    discover_slates,
    ingest_day,
    ingest_slate,
    main,
)
# pytest's default (prepend) import mode puts tests/ on sys.path, so conftest is
# importable by name — the shared fixtures' helpers live there.
from conftest import (
    DUPLICATE_DK_ID_SALARY,
    MAIN_SLATE,
    MANIFEST_HEADER,
    PROJ_HEADER,
    projection_rows,
    rediscover,
)


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path / "analytics.db")
    init_db(conn)
    yield conn
    conn.close()


# --- Discovery ----------------------------------------------------------------


def test_discovery_unions_the_three_sources(discovery):
    assert set(discovery.slates) == {
        MAIN_SLATE,
        "2026-03-13_classic_night",
        "2026-01-04_classic_late",
        "2026-02-19_classic_main",
    }
    assert not discovery.skipped_files


def test_discovery_records_per_slate_coverage(discovery):
    assert discovery.slates[MAIN_SLATE].coverage == "salary+projections+lineups"
    assert discovery.slates["2026-03-13_classic_night"].coverage == "salary+projections"
    # `late` is a real slate type with salary only — not an error.
    assert discovery.slates["2026-01-04_classic_late"].coverage == "salary"
    assert discovery.slates["2026-02-19_classic_main"].coverage == "projections"
    assert discovery.coverage_counts()["salary"] == 1


def test_discovery_skips_unparseable_filenames(sources):
    (sources["salary_dir"] / "notes.csv").write_text("x\n")
    (sources["salary_dir"] / "Bogus-2026-05-19.csv").write_text("x\n")
    d = discover_slates(sources["salary_dir"], sources["proj_dir"], {})
    skipped = dict(d.skipped_files)
    assert set(skipped) == {"notes.csv", "Bogus-2026-05-19.csv"}
    assert "unknown slate type" in skipped["Bogus-2026-05-19.csv"]
    # The good files still resolved — one bad name isn't fatal to discovery.
    assert MAIN_SLATE in d.slates


def test_discovery_skips_a_second_file_claiming_the_same_slate(sources):
    # Projections spell main implicitly; a stray explicit "Main-" file would
    # resolve to the same slate_id and silently win or lose on sort order.
    (sources["proj_dir"] / "NBA-Projs-Main-2026-05-18.csv").write_text(PROJ_HEADER + projection_rows())
    d = discover_slates(sources["salary_dir"], sources["proj_dir"], {})
    skipped = dict(d.skipped_files)
    assert "NBA-Projs-Main-2026-05-18.csv" in skipped
    assert "already taken by" in skipped["NBA-Projs-Main-2026-05-18.csv"]


def test_discovery_rejects_a_missing_source_directory(tmp_path):
    # An absent dir globs to nothing, which would read as "this source has no
    # files" — on G:\ it almost always means the drive isn't mounted.
    with pytest.raises(FileNotFoundError, match="salary directory not found"):
        discover_slates(tmp_path / "nope", tmp_path, {})


# --- ingest_slate -------------------------------------------------------------


def test_ingests_every_source_a_slate_has(discovery, conn):
    result = ingest_slate(discovery.slates[MAIN_SLATE], conn)
    assert result.ok
    assert {o.source: o.status for o in result.outcomes} == {
        "salary": Status.LOADED,
        "projections": Status.LOADED,
        "lineups": Status.LOADED,
    }
    assert result.rows == {
        "slate_players": 8,
        "projections": 8,
        "lineups": 2,
        "lineup_players": 16,
    }


def test_absent_sources_are_reported_not_failed(discovery, conn):
    result = ingest_slate(discovery.slates["2026-01-04_classic_late"], conn)
    assert result.ok  # salary-only is the normal case, not a failure
    assert result.outcome("salary").status is Status.LOADED
    assert result.outcome("projections").status is Status.ABSENT
    assert result.outcome("lineups").status is Status.ABSENT
    assert result.rows == {"slate_players": 8}


def test_projections_load_without_a_salary_csv(discovery, conn):
    # 2026-02-19/-02-20/-02-22 have projections but no salary file at all.
    # Refusing them would discard data rather than surface the gap.
    result = ingest_slate(discovery.slates["2026-02-19_classic_main"], conn)
    assert result.ok
    assert result.outcome("projections").status is Status.LOADED
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0


def test_lineups_are_skipped_when_the_slate_has_no_players(sources, conn):
    # Lineups reference dk_ids that must exist in slate_players; loading them
    # for a slate with no salary would create the orphans Phase 3 gates on.
    (sources["salary_dir"] / "Main-2026-05-18.csv").unlink()
    d = rediscover(sources)
    result = ingest_slate(d.slates[MAIN_SLATE], conn)
    lineups = result.outcome("lineups")
    assert lineups.status is Status.SKIPPED
    assert "slate_players is empty" in lineups.detail
    assert result.ok  # skipped is not a failure
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 0


def test_invalid_file_fails_that_source_only(sources, conn):
    (sources["salary_dir"] / "Main-2026-05-18.csv").write_text(DUPLICATE_DK_ID_SALARY)
    d = rediscover(sources)
    result = ingest_slate(d.slates[MAIN_SLATE], conn)
    assert not result.ok
    assert result.outcome("salary").status is Status.INVALID
    assert "duplicate" in result.outcome("salary").detail
    # Projections are independent of salary and still loaded...
    assert result.outcome("projections").status is Status.LOADED
    # ...but lineups depend on it, so they're skipped rather than orphaned.
    assert result.outcome("lineups").status is Status.SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0


def test_unreadable_file_is_an_error_not_a_crash(sources, conn):
    (sources["salary_dir"] / "Main-2026-05-18.csv").write_text("")  # 0 bytes
    d = discover_slates(sources["salary_dir"], sources["proj_dir"], {})
    result = ingest_slate(d.slates[MAIN_SLATE], conn)
    assert result.outcome("salary").status is Status.ERROR
    assert "EmptyDataError" in result.outcome("salary").detail


# --- dry run ------------------------------------------------------------------


def test_dry_run_validates_without_writing(discovery, conn):
    result = ingest_slate(discovery.slates[MAIN_SLATE], conn, dry_run=True)
    assert result.ok
    assert all(o.status is Status.VALID for o in result.outcomes)
    # Row counts are what *would* be read, including the melt's 8-per-lineup.
    assert result.rows == {
        "slate_players": 8,
        "projections": 8,
        "lineups": 2,
        "lineup_players": 16,
    }
    for table in ("slate_players", "projections", "lineups", "lineup_players"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_dry_run_reports_invalid_files(sources, conn):
    (sources["proj_dir"] / "NBA-Projs-2026-05-18.csv").write_text(
        PROJ_HEADER + "100,A,BOS,NYK,32.5,1.1,not-a-number,12.5\n"
    )
    d = discover_slates(sources["salary_dir"], sources["proj_dir"], {})
    result = ingest_slate(d.slates[MAIN_SLATE], conn, dry_run=True)
    assert not result.ok
    assert result.outcome("projections").status is Status.INVALID
    assert "non-numeric" in result.outcome("projections").detail


def test_dry_run_skips_lineups_when_salary_would_not_load(sources, conn):
    # No DB state to consult in a dry run, so the salary *outcome* is the gate.
    (sources["salary_dir"] / "Main-2026-05-18.csv").unlink()
    d = rediscover(sources)
    result = ingest_slate(d.slates[MAIN_SLATE], conn, dry_run=True)
    assert result.outcome("lineups").status is Status.SKIPPED
    assert "salary is absent" in result.outcome("lineups").detail


def test_dry_run_warns_when_the_manifest_count_disagrees_with_the_file(sources, conn):
    sources["manifest"].write_text(
        MANIFEST_HEADER
        + f"{sources['lineups_csv'].name},orig.csv,{MAIN_SLATE},2026-05-18T16:51:50,True,99\n"
    )
    d = rediscover(sources)
    result = ingest_slate(d.slates[MAIN_SLATE], conn, dry_run=True)
    lineups = result.outcome("lineups")
    assert lineups.status is Status.VALID
    assert any("manifest says 99" in w for w in lineups.warnings)


# --- ingest_day ---------------------------------------------------------------


def test_ingest_day_builds_the_slate_id_and_loads(discovery, conn):
    result = ingest_day("2026-05-18", "main", conn, discovery=discovery)
    assert result.slate_id == MAIN_SLATE
    assert result.rows["slate_players"] == 8


def test_ingest_day_on_a_slate_with_no_files_reports_all_absent(discovery, conn):
    result = ingest_day("2026-04-01", "turbo", conn, discovery=discovery)
    assert result.slate_id == "2026-04-01_classic_turbo"
    assert result.ok
    assert all(o.status is Status.ABSENT for o in result.outcomes)
    assert result.rows == {}


def test_ingest_day_rejects_an_unknown_slate_type(discovery, conn):
    with pytest.raises(ValueError, match="unknown slate type"):
        ingest_day("2026-04-01", "showdown", conn, discovery=discovery)


def test_ingest_day_is_idempotent(discovery, conn):
    first = ingest_day("2026-05-18", "main", conn, discovery=discovery)
    second = ingest_day("2026-05-18", "main", conn, discovery=discovery)
    assert first.rows == second.rows
    assert conn.execute("SELECT COUNT(*) FROM lineup_players").fetchone()[0] == 16


# --- backfill -----------------------------------------------------------------


def test_backfill_loads_every_discovered_slate(discovery, conn):
    summary = backfill(conn, discovery=discovery)
    assert len(summary.results) == 4
    assert not summary.failures
    assert summary.totals == {
        "slate_players": 24,  # 3 salary files x 8
        "projections": 24,  # 3 projections files x 8
        "lineups": 2,
        "lineup_players": 16,
    }
    counts = summary.status_counts()
    assert counts["salary"]["loaded"] == 3
    assert counts["salary"]["absent"] == 1
    assert counts["lineups"]["absent"] == 3


def test_backfill_continues_past_a_bad_file(sources, conn):
    (sources["salary_dir"] / "Night-2026-03-13.csv").write_text(DUPLICATE_DK_ID_SALARY)
    d = rediscover(sources)
    summary = backfill(conn, discovery=d)
    assert len(summary.failures) == 1
    slate_id, outcome = summary.failures[0]
    assert slate_id == "2026-03-13_classic_night"
    assert outcome.source == "salary"
    # The other three slates still loaded.
    assert summary.totals["slate_players"] == 16


def test_backfill_dry_run_writes_nothing(discovery, conn):
    summary = backfill(conn, dry_run=True, discovery=discovery)
    assert summary.dry_run
    assert not summary.failures
    assert summary.totals["slate_players"] == 24
    assert conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0


def test_backfill_honours_a_slate_id_restriction_and_progress_hook(discovery, conn):
    seen = []
    summary = backfill(
        conn, discovery=discovery, slate_ids=[MAIN_SLATE], on_result=seen.append
    )
    assert [r.slate_id for r in summary.results] == [MAIN_SLATE]
    assert [r.slate_id for r in seen] == [MAIN_SLATE]


def test_backfill_accepts_a_slate_id_discovery_never_saw(discovery, conn):
    summary = backfill(conn, discovery=discovery, slate_ids=["2026-04-01_classic_turbo"])
    assert summary.results[0].ok
    assert all(o.status is Status.ABSENT for o in summary.results[0].outcomes)


# --- SlateSources -------------------------------------------------------------


def test_coverage_label_of_an_empty_slate():
    empty = SlateSources(slate_id=MAIN_SLATE, date="2026-05-18", slate_type="main")
    assert empty.present == ()
    assert empty.coverage == "none"


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, discovery, tmp_path):
    """Point `main()` at the synthetic discovery and a throwaway DB."""
    from nba_dfs_stats_lab.ingest import orchestrator

    db_path = tmp_path / "cli.db"
    monkeypatch.setattr(orchestrator, "discover_slates", lambda: discovery)
    monkeypatch.setattr(orchestrator, "get_connection", lambda: get_connection(db_path))
    return db_path


def test_cli_list_reports_the_inventory(cli, capsys):
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "4 slate(s) discovered" in out
    assert "salary+projections+lineups" in out
    assert "every CSV in both source directories resolved to a slate" in out


def test_cli_refuses_a_full_write_without_all(cli, capsys):
    assert main([]) == 2
    assert "Refusing to write all 4 slates without --all" in capsys.readouterr().err
    assert not cli.exists() or _empty(cli)


def _empty(db_path) -> bool:
    conn = get_connection(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM slate_players").fetchone()[0] == 0
    except Exception:
        return True
    finally:
        conn.close()


def test_cli_dry_run_needs_no_all_flag_and_writes_nothing(cli, capsys):
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN — nothing written" in out
    assert _empty(cli)


def test_cli_backfill_writes_and_reports_totals(cli, capsys):
    assert main(["--all", "--per-slate"]) == 0
    out = capsys.readouterr().out
    assert MAIN_SLATE in out
    assert "no failures." in out
    assert not _empty(cli)


def test_cli_date_filter_restricts_the_run(cli, capsys):
    assert main(["--date", "2026-05-18"]) == 0
    out = capsys.readouterr().out
    assert "backfill: 1 slate(s)" in out


def test_cli_exits_nonzero_when_a_slate_fails(cli, sources, monkeypatch, capsys):
    from nba_dfs_stats_lab.ingest import orchestrator

    (sources["salary_dir"] / "Main-2026-05-18.csv").write_text(DUPLICATE_DK_ID_SALARY)
    d = rediscover(sources)
    monkeypatch.setattr(orchestrator, "discover_slates", lambda: d)
    assert main(["--all"]) == 1
    assert "1 failure(s):" in capsys.readouterr().out


def test_cli_reports_discovery_failure_without_a_traceback(monkeypatch, capsys):
    from nba_dfs_stats_lab.ingest import orchestrator

    def boom():
        raise FileNotFoundError("lineups manifest not found ... match_lineups_to_slates.py")

    monkeypatch.setattr(orchestrator, "discover_slates", boom)
    assert main(["--list"]) == 1
    assert "discovery failed:" in capsys.readouterr().err
