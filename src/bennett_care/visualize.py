"""Matplotlib charts for the pre-visit summary report.

Phase 1 charts:
  * weekly_trend       — weekly (ISO-week sum) seizure totals in the lookback
                         window, with a dotted median-week reference line.
                         Med changes are NOT annotated on this chart; the
                         report places a horizontal med-change table beneath it.
  * rolling_avg        — 14-day rolling mean of daily counts over the wider window.
  * hour_distribution  — 24-bar chart of seizures by hour of day across the
                         lookback window (no day-of-week dimension).
  * type_dist          — seizure-type pie of typed clusters in the window,
                         captioned with coverage ("X of Y typed").

Conventions (per CLAUDE.md):
  * 150 DPI PNGs, fixed dimensions.
  * Charts saved to the directory the caller provides (gitignored at the project
    level). Files are not deleted after rendering; the caller decides retention.
  * No causal annotations on any chart — labels are factual.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .ingest import SeizureLog

CHART_DPI: int = 150
WEEKLY_FIGSIZE: tuple[float, float] = (10.0, 4.0)
ROLLING_FIGSIZE: tuple[float, float] = (10.0, 4.0)
HOUR_FIGSIZE: tuple[float, float] = (10.0, 4.0)
PIE_FIGSIZE: tuple[float, float] = (6.0, 5.0)


@dataclass(frozen=True)
class ChartPaths:
    """Paths to the four Phase 1 charts. All four are always populated (some may render an empty-state image)."""

    weekly_trend: Path
    rolling_avg: Path
    hour_distribution: Path
    type_distribution: Path


def render_all_charts(
    log: SeizureLog,
    *,
    output_dir: str | Path,
    lookback_days: int = 90,
    rolling_lookback_days: int = 90,
) -> ChartPaths:
    """Render all Phase 1 charts into ``output_dir`` and return their paths."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(_rc_overrides()):
        weekly = _render_weekly_trend(log, out, lookback_days)
        rolling = _render_rolling_avg(log, out, rolling_lookback_days)
        hour = _render_hour_distribution(log, out, lookback_days)
        types = _render_type_distribution(log, out, lookback_days)
    return ChartPaths(weekly, rolling, hour, types)


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
# Weekly trend                                                                #
# --------------------------------------------------------------------------- #


def _render_weekly_trend(log: SeizureLog, out: Path, lookback: int) -> Path:
    """Weekly (ISO-week sum) seizure totals across the lookback window.

    Daily noise is intentionally smoothed away; the report's medication-change
    table next to this chart provides the per-event context, so no vertical
    lines are drawn here.
    """
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)
    window = log.daily.loc[(log.daily.index >= start) & (log.daily.index <= end)]

    fig, ax = plt.subplots(figsize=WEEKLY_FIGSIZE)
    if not window.empty:
        weekly = window["count"].resample("W-SUN").sum()
        ax.bar(weekly.index, weekly.values, width=5.5, color="#5a8fbb", edgecolor="none")
        median_val = float(weekly.median())
        ax.axhline(median_val, color="#7d7d7d", linestyle=":", linewidth=1, alpha=0.8)
        ax.text(
            weekly.index[0],
            median_val,
            f" median: {median_val:.0f}/wk",
            va="bottom",
            ha="left",
            fontsize=8,
            color="#5d5d5d",
        )
        for x, y in zip(weekly.index, weekly.values):
            ax.text(x, y + max(weekly.values) * 0.015, f"{int(y)}",
                    ha="center", va="bottom", fontsize=8, color="#333")
    ax.set_title(f"Weekly seizure totals, {start.date()} – {end.date()}")
    ax.set_ylabel("Seizures per week")
    ax.set_xlabel("Week ending")
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.SU, interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate()
    fig.tight_layout()

    path = out / "weekly_trend.png"
    fig.savefig(path)
    plt.close(fig)
    return path


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
# Hour-of-day distribution                                                    #
# --------------------------------------------------------------------------- #


def _hour_label_12h(h: int) -> str:
    """Convert 0-23 hour to 12-hour AM/PM label (e.g., 0 -> "12 AM", 13 -> "1 PM")."""
    if h == 0:
        return "12 AM"
    if h < 12:
        return f"{h} AM"
    if h == 12:
        return "12 PM"
    return f"{h - 12} PM"


def _render_hour_distribution(log: SeizureLog, out: Path, lookback: int) -> Path:
    """Total seizure count per hour of day across the lookback window.

    Day-of-week dimension is deliberately collapsed away — the clinical signal
    is the time-of-day pattern, not weekday variation. X-axis labels use a
    12-hour clock with AM/PM suffixes.
    """
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)
    clusters = log.clusters
    in_window = clusters[
        (clusters["Date"] >= start)
        & (clusters["Date"] <= end)
        & clusters["hour"].notna()
    ]

    fig, ax = plt.subplots(figsize=HOUR_FIGSIZE)
    if in_window.empty:
        ax.text(0.5, 0.5, "No clusters with parseable time in window",
                ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
    else:
        by_hour = (
            in_window.assign(hour_int=in_window["hour"].astype(int))
            .groupby("hour_int")["Seizure Count"]
            .sum()
            .reindex(range(24), fill_value=0)
        )
        ax.bar(by_hour.index, by_hour.values, color="#5a8fbb", edgecolor="none", width=0.85)
        peak_hour = int(by_hour.idxmax())
        peak_val = int(by_hour.max())
        ax.bar([peak_hour], [peak_val], color="#b03a2e", edgecolor="none", width=0.85)
        ax.annotate(
            f"peak: {_hour_label_12h(peak_hour)} ({peak_val})",
            xy=(peak_hour, peak_val),
            xytext=(peak_hour, peak_val + max(by_hour.values) * 0.06),
            fontsize=9, ha="center", color="#b03a2e",
        )
        ax.set_xticks(range(0, 24))
        ax.set_xticklabels(
            [_hour_label_12h(h) for h in range(0, 24)],
            fontsize=8,
            rotation=45,
            ha="right",
        )
        ax.set_xlim(-0.6, 23.6)
        ax.set_xlabel("Time of day")
        ax.set_ylabel("Total seizures")
    ax.set_title(f"Seizures by hour of day, {start.date()} – {end.date()}")
    fig.tight_layout()

    path = out / "hour_distribution.png"
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
