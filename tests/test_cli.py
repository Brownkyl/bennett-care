"""Tests for the click CLI."""

from __future__ import annotations

import csv
import zipfile
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


def test_visit_prep_end_to_end(sample_log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "visit-prep",
            "--log",
            str(sample_log_path),
            "--visit-date",
            "2026-08-04",
            "--lookback",
            "30",
            "--rolling-lookback",
            "30",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    docs = list(out.glob("bennett_visit_2026-08-04_*.docx"))
    assert len(docs) == 1
    assert zipfile.is_zipfile(docs[0])
    charts_dirs = list((out / "charts").iterdir())
    assert len(charts_dirs) == 1
    expected_pngs = {"weekly_trend.png", "rolling_avg.png", "hour_distribution.png", "type_distribution.png"}
    assert {p.name for p in charts_dirs[0].iterdir()} == expected_pngs


def test_visit_prep_rejects_bad_date(sample_log_path: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "visit-prep",
            "--log",
            str(sample_log_path),
            "--visit-date",
            "not-a-date",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert "YYYY-MM-DD" in result.output


def test_visit_prep_pdf_flag_invokes_soffice(
    sample_log_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """With --pdf, soffice is invoked and the PDF path appears in CLI output."""
    out = tmp_path / "out"
    captured: dict = {}

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/soffice" if name == "soffice" else None

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        docx = Path(cmd[-1])
        docx.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n%mock\n")

        class _Result:
            returncode = 0
            stderr = ""

        return _Result()

    monkeypatch.setattr("bennett_care.report.shutil.which", fake_which)
    monkeypatch.setattr("bennett_care.report.subprocess.run", fake_run)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "visit-prep",
            "--log", str(sample_log_path),
            "--visit-date", "2026-08-04",
            "--lookback", "30",
            "--rolling-lookback", "30",
            "--output", str(out),
            "--pdf",
        ],
    )
    assert result.exit_code == 0, result.output
    pdfs = list(out.glob("bennett_visit_2026-08-04_*.pdf"))
    assert len(pdfs) == 1
    assert "PDF:" in result.output
    assert captured["cmd"][0] == "/usr/local/bin/soffice"
    assert "--headless" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--convert-to") + 1] == "pdf"


def test_visit_prep_pdf_flag_errors_when_soffice_missing(
    sample_log_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """Missing `soffice` yields a clear error mentioning LibreOffice."""
    out = tmp_path / "out"
    monkeypatch.setattr("bennett_care.report.shutil.which", lambda name: None)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "visit-prep",
            "--log", str(sample_log_path),
            "--visit-date", "2026-08-04",
            "--lookback", "30",
            "--rolling-lookback", "30",
            "--output", str(out),
            "--pdf",
        ],
    )
    assert result.exit_code != 0
    assert "soffice" in result.output.lower()
    assert "libreoffice" in result.output.lower()


def test_dump_notes_writes_csv(notes_log_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "dump-notes",
            "--log",
            str(notes_log_path),
            "--lookback",
            "7",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(out.glob("bennett_notes_2026-01-10_*.csv"))
    assert len(files) == 1
    with files[0].open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 6
    assert set(rows[0].keys()) == {"Date", "Source", "Notes"}
    assert "Notes extracted: 6 entries" in result.output


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
