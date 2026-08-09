"""scripts/verify_phase5.py — the gate flow, driven against synthetic stand-ins.

The gate script is the artifact Jonny runs by hand, so what matters is that its
PASS/FAIL lines and exit codes are right — including on the failure paths, where
an uncaught exception would print a traceback instead of a verdict. Everything
here runs against a temporary analytics DB with a temporary ops DB attached, so
the file needs neither `G:\\` nor a backfilled `data/analytics.db`.

The write half is exercised here rather than on the real DB on purpose: the
Phase 5 gate is "show the low-confidence list *before* writing", so the real run
stops at the report and the write is Jonny's call.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from nba_dfs_stats_lab.db.schema import init_db

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_phase5.py"

OPS_PLAYERS = (
    (2544, "LeBron James"),
    (1630163, "MarJon Beauchamp"),
    (1629057, "Robert Williams III"),
    (1642949, "Yanic Konan Niederhauser"),
    (202334, "Ed Davis"),
)

SLATE_PLAYERS = (
    ("2026-05-18_classic_main", 1, "LeBron James"),  # exact
    ("2026-05-18_classic_main", 2, "MarJon Beauchamp"),  # exact
    ("2026-05-18_classic_main", 3, "Robert Williams"),  # normalized
    ("2026-05-18_classic_main", 4, "Yanic Niederhauser"),  # review
    ("2026-05-18_classic_main", 5, "Thomas Sorber"),  # unmatched
    ("2026-03-13_classic_night", 6, "LeBron James"),  # same name, new dk_id
)


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location("verify_phase5", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_phase5"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["verify_phase5"]


def build_db(tmp_path, slate_players=SLATE_PLAYERS, ops_players=OPS_PLAYERS):
    ops_path = tmp_path / "ops.db"
    ops = sqlite3.connect(ops_path)
    ops.execute("CREATE TABLE dim_players (PLAYER_ID INTEGER PRIMARY KEY, PLAYER_NAME TEXT)")
    ops.executemany("INSERT INTO dim_players VALUES (?, ?)", ops_players)
    ops.commit()
    ops.close()

    db_path = tmp_path / "analytics.db"
    conn = sqlite3.connect(db_path, uri=True)
    init_db(conn)
    conn.executemany(
        "INSERT INTO slate_players (slate_id, dk_id, name, salary) VALUES (?, ?, ?, 5000)",
        slate_players,
    )
    conn.commit()
    conn.close()
    return db_path, ops_path


@pytest.fixture
def gate(verify, monkeypatch, tmp_path):
    """Point the script at a temp analytics DB and a temp read-only ops DB."""
    db_path, ops_path = build_db(tmp_path)

    def fake_connection():
        conn = sqlite3.connect(db_path, uri=True)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def fake_attach(conn, *args, **kwargs):
        conn.execute("ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",))

    monkeypatch.setattr(verify, "get_connection", fake_connection)
    monkeypatch.setattr(verify, "attach_ops", fake_attach)
    return db_path


def rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def count(db_path, table="dk_crosswalk"):
    return rows(db_path, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608 — literal table name


# --- the default (report-only) gate -------------------------------------------


def test_report_gate_passes_and_writes_nothing(verify, gate, capsys):
    assert verify.main([]) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "All Phase 5 gate checks PASSED." in out
    assert count(gate) == 0


def test_report_shows_the_match_rate_and_the_review_list(verify, gate, capsys):
    verify.main([])
    out = capsys.readouterr().out
    # The two things the gate exists to put in front of Jonny. 5 distinct names
    # over 6 rows (LeBron plays two slates), 3 of them auto-matched.
    assert "3 / 5 names (60.0%)" in out
    assert "Yanic Niederhauser" in out
    assert "Yanic Konan Niederhauser" in out
    assert "nothing below is written" in out
    # And the unmatched name, with its near miss for context.
    assert "Thomas Sorber" in out


def test_probe_write_to_ops_is_rejected(verify, gate, capsys):
    verify.main([])
    assert "[PASS] probe write to ops is rejected" in capsys.readouterr().out


def test_empty_slate_players_stops_with_a_pointer_to_the_backfill(
    verify, monkeypatch, tmp_path, capsys
):
    db_path, ops_path = build_db(tmp_path, slate_players=())
    monkeypatch.setattr(verify, "get_connection", lambda: sqlite3.connect(db_path, uri=True))
    monkeypatch.setattr(
        verify,
        "attach_ops",
        lambda conn, *a, **k: conn.execute(
            "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
        ),
    )
    assert verify.main([]) == 1
    assert "orchestrator --all" in capsys.readouterr().err


# --- the review round-trip ----------------------------------------------------


def test_review_export_then_apply(verify, gate, tmp_path, capsys):
    queue = tmp_path / "review.csv"
    assert verify.main(["--review", str(queue)]) == 0
    capsys.readouterr()
    assert queue.exists()
    assert count(gate) == 0  # exporting is still not writing

    # Stand in for Jonny ticking the box.
    text = queue.read_text(encoding="utf-8")
    queue.write_text(
        text.replace("\n,Yanic Niederhauser,", "\ny,Yanic Niederhauser,"), encoding="utf-8"
    )

    assert verify.main(["--write", "--apply", str(queue)]) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "1 approved name(s)" in out
    # 3 auto-matched names over 4 dk_ids, plus the 1 approved dk_id.
    assert count(gate) == 5
    assert (4, 1642949) in rows(gate, "SELECT dk_id, player_id FROM dk_crosswalk")


def test_write_without_apply_writes_only_the_auto_tiers(verify, gate, capsys):
    assert verify.main(["--write"]) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert count(gate) == 4  # the review and unmatched names are excluded
    written = {name for (name,) in rows(gate, "SELECT DISTINCT display_name FROM dk_crosswalk")}
    assert "Yanic Niederhauser" not in written
    assert "Thomas Sorber" not in written


def test_write_is_idempotent(verify, gate, capsys):
    verify.main(["--write"])
    first = count(gate)
    assert verify.main(["--write"]) == 0
    capsys.readouterr()
    assert count(gate) == first


def test_apply_requires_write(verify, gate, tmp_path, capsys):
    assert verify.main(["--apply", str(tmp_path / "x.csv")]) == 2
    assert "--apply requires --write" in capsys.readouterr().err


def test_a_rejected_approval_file_exits_2_without_writing(verify, gate, tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    # An id that was never offered for this name — a typo, not a decision.
    bad.write_text("approve,dk_name,player_id\ny,Yanic Niederhauser,2544\n", encoding="utf-8")
    assert verify.main(["--write", "--apply", str(bad)]) == 2
    assert "never offered" in capsys.readouterr().err
    assert count(gate) == 0


def test_a_malformed_approval_file_exits_2_without_a_traceback(verify, gate, tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("dk_name,player_id\nYanic Niederhauser,1642949\n", encoding="utf-8")
    assert verify.main(["--write", "--apply", str(bad)]) == 2
    captured = capsys.readouterr()
    assert "missing column" in captured.err
    assert "Traceback" not in captured.err
    assert count(gate) == 0


# --- failure paths ------------------------------------------------------------


def test_a_normalization_collision_fails_the_gate(verify, monkeypatch, tmp_path, capsys):
    """Two ops players behind one key must stop the gate, not be picked between."""
    db_path, ops_path = build_db(
        tmp_path,
        slate_players=(("2026-05-18_classic_main", 1, "Gary Payton Jr."),),
        ops_players=((1, "Gary Payton II"), (2, "Gary Payton")),
    )
    monkeypatch.setattr(verify, "get_connection", lambda: sqlite3.connect(db_path, uri=True))
    monkeypatch.setattr(
        verify,
        "attach_ops",
        lambda conn, *a, **k: conn.execute(
            "ATTACH DATABASE ? AS ops", (f"file:{ops_path.as_posix()}?mode=ro",)
        ),
    )
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] ops dim_players: no two names collapse to one key" in out
    assert "FAILED" in out
    # ...and the colliding name is still reported as ambiguous, not auto-matched.
    assert "ambiguous 1" in out
    assert count(db_path) == 0


def test_gate_reports_a_writable_ops_attach_as_a_failure(verify, monkeypatch, tmp_path, capsys):
    """The one check whose failure means "stop everything" — so it must fail loudly."""
    db_path, ops_path = build_db(tmp_path)
    monkeypatch.setattr(verify, "get_connection", lambda: sqlite3.connect(db_path, uri=True))
    monkeypatch.setattr(  # deliberately NOT mode=ro
        verify,
        "attach_ops",
        lambda conn, *a, **k: conn.execute("ATTACH DATABASE ? AS ops", (str(ops_path),)),
    )
    assert verify.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] probe write to ops is rejected" in out
    assert "the ATTACH is WRITABLE" in out
