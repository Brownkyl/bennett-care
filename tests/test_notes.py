"""Tests for the raw notes extractor.

Covers the "logged days only" filter, the lookback window anchor, blank-cell
skipping, source labeling, and the Date-desc / Source-asc sort order.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from bennett_care.notes import extract_notes


def _expected_columns() -> list[str]:
    return ["Date", "Source", "Notes"]


def test_returns_expected_columns(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    assert list(out.columns) == _expected_columns()


def test_excludes_notes_on_unmonitored_and_uncertain_dates(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    quotes = set(out["Notes"])
    assert "unmonitored sleepover" not in quotes
    assert "ambiguous day" not in quotes
    assert "unmonitored cluster should not appear" not in quotes
    assert "uncertain cluster should not appear" not in quotes


def test_excludes_notes_outside_lookback_window(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    assert "OUT OF WINDOW" not in set(out["Notes"])


def test_lookback_window_narrows_correctly(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=3)
    dates = set(out["Date"])
    assert min(dates) == pd.Timestamp("2026-01-08")
    assert max(dates) == pd.Timestamp("2026-01-10")
    assert "edge of window" not in set(out["Notes"])
    assert "zero day, fine" not in set(out["Notes"])


def test_includes_zero_seizure_day_notes(notes_log_path: Path) -> None:
    """A date with no Cluster Detail rows still counts as logged."""
    out = extract_notes(notes_log_path, lookback_days=7)
    assert "zero day, fine" in set(out["Notes"])


def test_blank_cells_skipped(notes_log_path: Path) -> None:
    """01-09 has None Notes and whitespace-only Diet — neither should appear."""
    out = extract_notes(notes_log_path, lookback_days=7)
    on_jan9 = out.loc[out["Date"] == pd.Timestamp("2026-01-09")]
    assert on_jan9.empty


def test_source_labels(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    sources = set(out["Source"])
    assert "All Data: Notes" in sources
    assert "All Data: Diet" in sources
    assert "Cluster Detail: Cluster 1" in sources


def test_cluster_note_preserved_verbatim(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    cluster_rows = out.loc[out["Source"] == "Cluster Detail: Cluster 1"]
    assert len(cluster_rows) == 1
    assert cluster_rows["Notes"].iloc[0] == "duration approx"
    assert cluster_rows["Date"].iloc[0] == pd.Timestamp("2026-01-08")


def test_diet_preserved_verbatim(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    diet_rows = out.loc[out["Source"] == "All Data: Diet"]
    assert len(diet_rows) == 1
    assert diet_rows["Notes"].iloc[0] == "ate well"


def test_sort_order_date_desc_then_source_asc(notes_log_path: Path) -> None:
    out = extract_notes(notes_log_path, lookback_days=7)
    # Dates should be non-increasing.
    dates = list(out["Date"])
    assert dates == sorted(dates, reverse=True)
    # Within 2026-01-10 (two rows: Diet then Notes alphabetically).
    on_jan10 = out.loc[out["Date"] == pd.Timestamp("2026-01-10")]
    assert list(on_jan10["Source"]) == ["All Data: Diet", "All Data: Notes"]


def test_expected_row_count_at_full_window(notes_log_path: Path) -> None:
    """6 rows: 01-10 (Notes+Diet), 01-08 (Notes+Cluster1), 01-05 (Notes), 01-04 (Notes)."""
    out = extract_notes(notes_log_path, lookback_days=7)
    assert len(out) == 6


def test_lookback_zero_raises(notes_log_path: Path) -> None:
    with pytest.raises(ValueError):
        extract_notes(notes_log_path, lookback_days=0)


def test_empty_log_returns_empty_frame(notes_log_path: Path) -> None:
    """Lookback of 1 day anchored at latest_date should still return a valid frame."""
    out = extract_notes(notes_log_path, lookback_days=1)
    assert list(out.columns) == _expected_columns()
    # Latest day (01-10) has both Notes and Diet set.
    assert set(out["Date"]) == {pd.Timestamp("2026-01-10")}
    assert len(out) == 2
