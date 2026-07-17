"""Generic contract machinery, exercised with a synthetic Phase-3-shaped schema.

The projections schema is all-numeric, so these tests pin the behaviors the
salary/lineups schemas will rely on: str columns, and missing-key reporting.
"""

import pandas as pd

from nba_dfs_stats_lab.ingest.schemas import (
    ColumnSpec,
    SourceSchema,
    normalize_frame,
    validate_frame,
)

SYNTH = SourceSchema(
    name="synthetic",
    table="slate_players",
    key="dk_id",
    columns=(
        ColumnSpec("DFS ID", "dk_id", "int", nullable=False),
        ColumnSpec("Name", "name", "str"),
        ColumnSpec("Salary", "salary", "int"),
    ),
)


def test_str_column_validates_and_normalizes():
    df = pd.DataFrame({"DFS ID": [1, 2], "Name": ["Jamal Shead", None], "Salary": [4000, 5100]})
    report = validate_frame(df, SYNTH)
    assert report.ok
    assert any("Name" in w for w in report.warnings)  # null in nullable str -> warning

    out = normalize_frame(df, SYNTH, "s1")
    assert out["name"].dtype == "string"
    assert out["name"][0] == "Jamal Shead"
    assert pd.isna(out["name"][1])  # NA survives; the writer maps it to NULL


def test_missing_int_normalizes_to_na():
    df = pd.DataFrame({"DFS ID": [1], "Name": ["A"], "Salary": [None]})
    out = normalize_frame(df, SYNTH, "s1")
    assert out["salary"].dtype == "Int64"
    assert pd.isna(out["salary"][0])


def test_missing_keys_not_double_reported_as_duplicates():
    # Two rows with a missing key: one "missing" error, no bogus "duplicate" error.
    df = pd.DataFrame({"DFS ID": [None, None], "Name": ["A", "B"], "Salary": [1, 2]})
    report = validate_frame(df, SYNTH)
    assert not report.ok
    assert any("missing" in e for e in report.errors)
    assert not any("duplicate" in e for e in report.errors)
