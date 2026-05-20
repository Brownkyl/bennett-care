"""Tests for the click CLI."""

from __future__ import annotations

import csv
from pathlib import Path

from click.testing import CliRunner

from bennett_care.cli import cli


def test_inspect_mismatches_writes_csv(sample_log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "inspect-mismatches",
            "--log",
            str(sample_log_path),
            "--top",
            "10",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(out.glob("mismatches_top10_*.csv"))
    assert len(files) == 1
    with files[0].open() as fh:
        rows = list(csv.DictReader(fh))
    # Sample fixture has exactly one mismatch (2026-01-05).
    assert len(rows) == 1
    assert rows[0]["cluster_detail_sum"] == "4"
    assert rows[0]["all_data_daily_total"] == "5"
    assert rows[0]["diff"] == "-1"
    assert rows[0]["abs_diff"] == "1"


def test_inspect_mismatches_no_mismatches(tmp_path: Path) -> None:
    """When the log has no mismatches, the command reports cleanly with no CSV."""
    import pandas as pd

    path = tmp_path / "clean.xlsx"
    all_data = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-03-01", "2026-03-02"]),
            "Daily Total": [1, 0],
        }
    )
    clusters = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-03-01"]),
            "Cluster #": [1],
            "Start Time": ["9:00 AM"],
            "Duration (min)": [1.0],
            "Seizure Count": [1],
            "Seizure Type": ["atonic"],
            "Day Status": ["logged"],
            "Verified": ["Y"],
            "Flags": [None],
            "Notes": [None],
        }
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        all_data.to_excel(writer, sheet_name="All Data", index=False)
        clusters.to_excel(writer, sheet_name="Cluster Detail", index=False)

    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["inspect-mismatches", "--log", str(path), "--output", str(out)],
    )
    assert result.exit_code == 0
    assert "No mismatches to dump." in result.output
    assert not out.exists() or not list(out.glob("*.csv"))
