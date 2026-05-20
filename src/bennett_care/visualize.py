"""Matplotlib charts for the pre-visit summary report.

Phase 1 charts:
  * daily_totals   — bar chart of daily seizure counts in the lookback window,
                     with vertical lines at real med changes labeled
                     ``date: delta`` (auto-staggered when close together).
  * rolling_avg    — 14-day rolling mean of daily counts over the wider window.
  * tod_heatmap    — hour-of-day × day-of-week heatmap (viridis), from clusters
                     in the lookback window.
  * type_dist      — seizure-type pie of typed clusters in the window, captioned
                     with coverage ("X of Y typed").

Conventions (per CLAUDE.md):
  * 150 DPI PNGs, fixed dimensions.
  * Viridis colormap on the heatmap (perceptually uniform, colorblind-safe).
  * Charts saved to the directory the caller provides (gitignored at the project
    level). Files are not deleted after rendering; the caller decides retention.
  * No causal annotations on any chart — labels are factual.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .ingest import MedChange, SeizureLog
from .stats import format_regimen_delta, real_changes_with_prev

CHART_DPI: int = 150
DAILY_FIGSIZE: tuple[float, float] = (10.0, 4.5)
ROLLING_FIGSIZE: tuple[float, float] = (10.0, 4.0)
HEATMAP_FIGSIZE: tuple[float, float] = (8.0, 5.0)
PIE_FIGSIZE: tuple[float, float] = (6.0, 5.0)
HEATMAP_CMAP: str = "viridis"

_DAY_NAMES: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class ChartPaths:
    """Paths to the four Phase 1 charts. All four are always populated (some may render an empty-state image)."""

    daily_totals: Path
    rolling_avg: Path
    tod_heatmap: Path
    type_distribution: Path


def render_all_charts(
    log: SeizureLog,
    *,
    output_dir: str | Path,
    lookback_days: int = 90,
    rolling_lookback_days: int = 180,
) -> ChartPaths:
    """Render all Phase 1 charts into ``output_dir`` and return their paths."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(_rc_overrides()):
        daily = _render_daily_totals(log, out, lookback_days)
        rolling = _render_rolling_avg(log, out, rolling_lookback_days)
        heatmap = _render_tod_heatmap(log, out, lookback_days)
        types = _render_type_distribution(log, out, lookback_days)
    return ChartPaths(daily, rolling, heatmap, types)


def _rc_overrides() -> dict:
    return {
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": CHART_DPI,
        "savefig.dpi": CHART_DPI,
        "savefig.bbox": "tight",
    }


# --------------------------------------------------------------------------- #
# Daily totals                                                                #
# --------------------------------------------------------------------------- #


def _render_daily_totals(log: SeizureLog, out: Path, lookback: int) -> Path:
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)
    window = log.daily.loc[(log.daily.index >= start) & (log.daily.index <= end)]

    fig, ax = plt.subplots(figsize=DAILY_FIGSIZE)
    if not window.empty:
        ax.bar(window.index, window["count"], width=0.9, color="#5a8fbb", edgecolor="none")
        ymax = float(window["count"].max())
    else:
        ymax = 1.0

    changes_in_window = _changes_in_window(log, start, end)
    if changes_in_window:
        _draw_change_lines(ax, changes_in_window, ymax)

    # Title on the figure (not the axes) so it sits above the upward-reading
    # med-change labels rather than colliding with them.
    fig.suptitle(f"Daily seizure count, {start.date()} – {end.date()}", y=0.99)
    ax.set_ylabel("Seizures")
    ax.set_xlim(start - pd.Timedelta(days=1), end + pd.Timedelta(days=1))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate()
    fig.subplots_adjust(top=0.72, bottom=0.18)

    path = out / "daily_totals.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def _changes_in_window(
    log: SeizureLog, start: pd.Timestamp, end: pd.Timestamp
) -> list[tuple[MedChange, MedChange | None]]:
    pairs = real_changes_with_prev(log)
    return [(c, p) for c, p in pairs if start <= c.date <= end]


def _draw_change_lines(
    ax: plt.Axes,
    changes: list[tuple[MedChange, MedChange | None]],
    bar_ymax: float,
) -> None:
    """Draw vertical lines + vertical text labels for each med change.

    Vertical labels read bottom-to-top, occupying minimal horizontal width
    so they don't collide when changes are close in time. Stagger levels add
    a small horizontal nudge so adjacent labels remain distinguishable.
    """
    if bar_ymax <= 0:
        bar_ymax = 1.0
    ax.set_ylim(0, bar_ymax * 1.05)
    label_y = bar_ymax * 1.08  # just above bars, in figure-margin space
    levels = _stagger_levels([c.date for c, _ in changes], min_gap_days=5)
    label_color = "#b03a2e"
    for (change, prev), level in zip(changes, levels):
        ax.axvline(change.date, color=label_color, linestyle="--", linewidth=1, alpha=0.7)
        delta = format_regimen_delta(prev.regimen if prev else None, change.regimen)
        label = f"{change.date.strftime('%-m/%-d')}: {delta}"
        x_nudge = pd.Timedelta(days=level)  # small horizontal offset for tight clusters
        ax.text(
            change.date + x_nudge,
            label_y,
            label,
            rotation=90,
            fontsize=7,
            ha="center",
            va="bottom",
            color=label_color,
            clip_on=False,
        )


def _stagger_levels(dates: Iterable[pd.Timestamp], min_gap_days: int) -> list[int]:
    """Assign each date a non-negative stagger level so neighbors don't collide."""
    levels: list[int] = []
    last_at_level: dict[int, pd.Timestamp] = {}
    for d in dates:
        for level in range(20):
            last = last_at_level.get(level)
            if last is None or (d - last).days >= min_gap_days:
                levels.append(level)
                last_at_level[level] = d
                break
        else:
            levels.append(0)
    return levels


# --------------------------------------------------------------------------- #
# 14-day rolling average                                                      #
# --------------------------------------------------------------------------- #


def _render_rolling_avg(log: SeizureLog, out: Path, lookback: int) -> Path:
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)
    window = log.daily.loc[(log.daily.index >= start) & (log.daily.index <= end)]
    rolling = window["count"].rolling(window=14, min_periods=7).mean()

    fig, ax = plt.subplots(figsize=ROLLING_FIGSIZE)
    if not rolling.dropna().empty:
        ax.plot(rolling.index, rolling.values, color="#5a8fbb", linewidth=1.8)
        ax.fill_between(rolling.index, 0, rolling.values, color="#5a8fbb", alpha=0.18)
    ax.set_title(f"14-day rolling mean daily count, {start.date()} – {end.date()}")
    ax.set_ylabel("Mean seizures / day")
    ax.set_xlim(start, end)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    fig.autofmt_xdate()

    path = out / "rolling_avg.png"
    fig.savefig(path)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Time-of-day heatmap                                                         #
# --------------------------------------------------------------------------- #


def _render_tod_heatmap(log: SeizureLog, out: Path, lookback: int) -> Path:
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)

    clusters = log.clusters
    in_window = clusters[
        (clusters["Date"] >= start)
        & (clusters["Date"] <= end)
        & clusters["hour"].notna()
    ].copy()

    fig, ax = plt.subplots(figsize=HEATMAP_FIGSIZE)
    if in_window.empty:
        ax.text(0.5, 0.5, "No clusters with parseable time in window",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
    else:
        in_window["dow"] = in_window["Date"].dt.dayofweek
        in_window["hour_int"] = in_window["hour"].astype(int)
        pivot = (
            in_window.groupby(["dow", "hour_int"])["Seizure Count"]
            .sum()
            .unstack(fill_value=0)
            .reindex(index=range(7), columns=range(24), fill_value=0)
        )
        im = ax.imshow(pivot.values, cmap=HEATMAP_CMAP, aspect="auto")
        ax.set_xticks(range(0, 24, 2))
        ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)], fontsize=8)
        ax.set_yticks(range(7))
        ax.set_yticklabels(_DAY_NAMES)
        ax.set_xlabel("Hour of day")
        ax.set_title(f"Seizures by hour × day-of-week, {start.date()} – {end.date()}")
        cbar = fig.colorbar(im, ax=ax, shrink=0.85)
        cbar.set_label("Total seizures")

    path = out / "tod_heatmap.png"
    fig.savefig(path)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Seizure-type distribution                                                   #
# --------------------------------------------------------------------------- #


def _render_type_distribution(log: SeizureLog, out: Path, lookback: int) -> Path:
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)

    in_window = log.clusters[
        (log.clusters["Date"] >= start) & (log.clusters["Date"] <= end)
    ]
    typed = in_window[in_window["Seizure Type"].astype(str).str.len() > 0]

    n_typed = int(len(typed))
    n_total = int(len(in_window))
    coverage_pct = (100.0 * n_typed / n_total) if n_total else 0.0

    fig, ax = plt.subplots(figsize=PIE_FIGSIZE)
    if n_typed == 0:
        ax.text(0.5, 0.5, "No typed clusters in window",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
    else:
        counts = (
            typed.groupby("Seizure Type")["Seizure Count"]
            .sum()
            .sort_values(ascending=False)
        )
        colors = plt.get_cmap("tab10")(np.linspace(0, 1, len(counts)))
        ax.pie(
            counts.values,
            labels=counts.index,
            autopct="%1.0f%%",
            startangle=90,
            colors=colors,
            wedgeprops={"edgecolor": "white", "linewidth": 1},
        )
        ax.set_aspect("equal")

    title = f"Seizure type distribution, {start.date()} – {end.date()}"
    subtitle = (
        f"Based on {n_typed} of {n_total} clusters "
        f"({coverage_pct:.0f}%) with a Seizure Type recorded."
    )
    ax.set_title(f"{title}\n{subtitle}", fontsize=10)

    path = out / "type_distribution.png"
    fig.savefig(path)
    plt.close(fig)
    return path
