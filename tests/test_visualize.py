"""Smoke tests for bennett_care.visualize.

We don't assert visual content — only that PNGs are written, are non-trivial in
size, and contain a PNG header. Visual inspection happens by opening the files.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bennett_care.ingest import Dose, MedChange, SeizureLog
from bennett_care.visualize import ChartPaths, render_all_charts


PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _make_log(
    *,
    days: int = 200,
    cluster_dates: list[int] | None = None,
    typed_count: int = 5,
    med_changes_at: list[int] | None = None,
) -> SeizureLog:
    dates = pd.date_range("2026-01-01", periods=days, freq="D")
    rng = np.random.default_rng(0)
    counts = rng.poisson(lam=10, size=days)
    daily = pd.DataFrame(
        {
            "count": counts,
            "daily_total_recorded": counts,
            "mismatch": np.zeros(days, dtype=bool),
        },
        index=dates,
    )
    daily.index.name = "Date"

    cluster_dates = cluster_dates if cluster_dates is not None else list(range(0, days, 3))
    n_clusters = len(cluster_dates)
    cluster_df = pd.DataFrame(
        {
            "Date": [dates[i] for i in cluster_dates],
            "Cluster #": list(range(1, n_clusters + 1)),
            "Start Time": ["9:00 AM"] * n_clusters,
            "Duration (min)": [1.0] * n_clusters,
            "Seizure Count": [1] * n_clusters,
            "Seizure Type": ["atonic"] * min(typed_count, n_clusters)
            + [""] * max(0, n_clusters - typed_count),
            "Day Status": ["logged"] * n_clusters,
            "Verified": ["Y"] * n_clusters,
            "Flags": [None] * n_clusters,
            "Notes": [None] * n_clusters,
            "flag_tokens": [frozenset()] * n_clusters,
            "hour": [9.0] * n_clusters,
        }
    )

    changes: list[MedChange] = []
    for i, offset in enumerate(med_changes_at or []):
        amount = 1.0 + 0.5 * i
        changes.append(
            MedChange(
                date=dates[offset],
                raw=f"Clobazam {amount}mL am",
                regimen={"Clobazam": (Dose(amount, "am"),)},
            )
        )

    return SeizureLog(
        daily=daily,
        clusters=cluster_df,
        med_changes=changes,
        mismatches=pd.DataFrame(),
        excluded_dates=pd.DatetimeIndex([]),
    )


def _assert_png(path: Path) -> None:
    assert path.exists(), f"Expected PNG at {path}"
    data = path.read_bytes()
    assert len(data) > 1000, f"PNG at {path} is suspiciously small ({len(data)} bytes)"
    assert data[: len(PNG_HEADER)] == PNG_HEADER, f"File at {path} is not a PNG"


def test_render_all_charts_writes_four_pngs(tmp_path: Path) -> None:
    log = _make_log(med_changes_at=[60, 120, 180])
    out = tmp_path / "charts"
    result = render_all_charts(log, output_dir=out, lookback_days=90, rolling_lookback_days=180)
    assert isinstance(result, ChartPaths)
    _assert_png(result.weekly_trend)
    _assert_png(result.rolling_avg)
    _assert_png(result.hour_distribution)
    _assert_png(result.type_distribution)


def test_render_handles_no_med_changes(tmp_path: Path) -> None:
    log = _make_log(med_changes_at=[])
    result = render_all_charts(log, output_dir=tmp_path)
    _assert_png(result.weekly_trend)


def test_render_handles_no_typed_clusters(tmp_path: Path) -> None:
    log = _make_log(typed_count=0)
    result = render_all_charts(log, output_dir=tmp_path)
    _assert_png(result.type_distribution)


def test_render_handles_empty_cluster_window(tmp_path: Path) -> None:
    # Clusters all happen long before the lookback window — the hour chart
    # should render an empty-state image, not error.
    log = _make_log(cluster_dates=[0, 1, 2], days=200)
    result = render_all_charts(log, output_dir=tmp_path, lookback_days=30)
    _assert_png(result.hour_distribution)


def test_render_creates_output_dir(tmp_path: Path) -> None:
    log = _make_log()
    nested = tmp_path / "nested" / "charts"
    render_all_charts(log, output_dir=nested)
    assert nested.is_dir()


def test_weekly_trend_filename_is_weekly_trend(tmp_path: Path) -> None:
    log = _make_log()
    result = render_all_charts(log, output_dir=tmp_path)
    assert result.weekly_trend.name == "weekly_trend.png"
    assert result.hour_distribution.name == "hour_distribution.png"
