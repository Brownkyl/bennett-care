"""Tests for bennett_care.stats."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bennett_care.ingest import Dose, MedChange, SeizureLog
from bennett_care.stats import (
    DEFAULT_WINDOWS,
    MeanCI,
    PrePostAnalysis,
    analyze_recent_changes,
    hedges_g,
    mean_with_bootstrap_ci,
    notable_days,
    rescue_event_dates,
)


# --------------------------------------------------------------------------- #
# Fixture: synthetic SeizureLog without going through Excel                    #
# --------------------------------------------------------------------------- #


def _make_log(
    *,
    days: int = 180,
    change_day: int = 90,
    pre_lam: float = 20.0,
    post_lam: float = 10.0,
    extra_changes: list[tuple[int, dict[str, tuple[Dose, ...]]]] | None = None,
    seed: int = 0,
) -> SeizureLog:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=days, freq="D")
    counts = np.concatenate(
        [
            rng.poisson(lam=pre_lam, size=change_day),
            rng.poisson(lam=post_lam, size=days - change_day),
        ]
    )
    daily = pd.DataFrame(
        {
            "count": counts,
            "daily_total_recorded": counts,
            "mismatch": np.zeros(days, dtype=bool),
        },
        index=dates,
    )
    daily.index.name = "Date"

    changes: list[MedChange] = [
        MedChange(
            date=dates[0],
            raw="Clobazam 1mL am",
            regimen={"Clobazam": (Dose(1.0, "am"),)},
        ),
        MedChange(
            date=dates[change_day],
            raw="Clobazam 2mL am",
            regimen={"Clobazam": (Dose(2.0, "am"),)},
        ),
    ]
    if extra_changes:
        for offset, regimen in extra_changes:
            changes.append(
                MedChange(date=dates[offset], raw="(extra)", regimen=regimen)
            )
    changes.sort(key=lambda c: c.date)

    return SeizureLog(
        daily=daily,
        clusters=pd.DataFrame(),
        med_changes=changes,
        mismatches=pd.DataFrame(),
        excluded_dates=pd.DatetimeIndex([]),
    )


# --------------------------------------------------------------------------- #
# mean_with_bootstrap_ci                                                      #
# --------------------------------------------------------------------------- #


def test_mean_ci_empty_returns_nan() -> None:
    res = mean_with_bootstrap_ci([])
    assert res.n == 0
    assert np.isnan(res.mean) and np.isnan(res.low) and np.isnan(res.high)


def test_mean_ci_single_value_collapses() -> None:
    res = mean_with_bootstrap_ci([7.0])
    assert res.n == 1
    assert res.mean == res.low == res.high == 7.0


def test_mean_ci_zero_variance_collapses() -> None:
    res = mean_with_bootstrap_ci([5.0] * 10)
    assert res.mean == res.low == res.high == 5.0
    assert res.n == 10


def test_mean_ci_reproducible_with_seed() -> None:
    a = mean_with_bootstrap_ci([1.0, 2.0, 3.0, 4.0, 5.0])
    b = mean_with_bootstrap_ci([1.0, 2.0, 3.0, 4.0, 5.0])
    assert a == b


def test_mean_ci_brackets_mean() -> None:
    rng = np.random.default_rng(7)
    values = rng.poisson(lam=20, size=40)
    res = mean_with_bootstrap_ci(values)
    assert res.low <= res.mean <= res.high
    assert res.n == 40


# --------------------------------------------------------------------------- #
# hedges_g                                                                    #
# --------------------------------------------------------------------------- #


def test_hedges_g_positive_when_post_higher() -> None:
    g = hedges_g([1, 2, 3, 4, 5], [10, 11, 12, 13, 14])
    assert g is not None and g > 0


def test_hedges_g_negative_when_post_lower() -> None:
    g = hedges_g([10, 11, 12, 13, 14], [1, 2, 3, 4, 5])
    assert g is not None and g < 0


def test_hedges_g_none_for_tiny_sample() -> None:
    assert hedges_g([1], [2, 3, 4]) is None
    assert hedges_g([1, 2], [3]) is None


def test_hedges_g_none_for_zero_pooled_sd() -> None:
    assert hedges_g([5, 5, 5], [5, 5, 5]) is None


def test_hedges_g_small_sample_correction_applied() -> None:
    """For n1=n2=5 the correction factor is 1 - 3/(4*10 - 9) = 1 - 3/31 ≈ 0.9032."""
    g = hedges_g([0, 0, 0, 0, 0], [1, 1, 1, 1, 1])
    # Cohen's d would be inf (zero variance in both groups) → expect None
    assert g is None


def test_hedges_g_value_matches_manual_calc() -> None:
    import math

    pre = np.array([10.0, 12.0, 14.0, 16.0, 18.0])
    post = np.array([4.0, 6.0, 8.0, 10.0, 12.0])
    g = hedges_g(pre, post)
    # ddof=1 var = 10 for both → pooled = sqrt(10)
    # d = (mean_post - mean_pre) / pooled_sd = (8 - 14)/sqrt(10) ≈ -1.8974
    # correction = 1 - 3/(4*10 - 9) = 28/31 ≈ 0.9032
    # g = d * correction ≈ -1.7138
    expected = (-6.0 / math.sqrt(10.0)) * (1.0 - 3.0 / 31.0)
    assert g is not None
    assert pytest.approx(g, rel=1e-4) == expected


# --------------------------------------------------------------------------- #
# analyze_recent_changes                                                      #
# --------------------------------------------------------------------------- #


def test_analyze_returns_top_k_real_changes() -> None:
    log = _make_log()
    out = analyze_recent_changes(log, k=2)
    assert len(out) == 2


def test_analyze_skips_rescue_note_entries() -> None:
    """An entry with empty regimen (parsed as rescue note) must not appear."""
    log = _make_log()
    log.med_changes.append(
        MedChange(
            date=log.daily.index[120],
            raw="Gave rescue meds at 8pm",
            regimen={},
            unparsed_fragments=("Gave rescue meds at 8pm",),
        )
    )
    log.med_changes.sort(key=lambda c: c.date)
    out = analyze_recent_changes(log, k=2)
    assert all(a.regimen_str != "" for a in out)
    # Most recent two real changes are still the original two.
    dates = [a.change_date for a in out]
    assert log.daily.index[0] in dates or log.daily.index[90] in dates


def test_analyze_window_sizes_present() -> None:
    log = _make_log()
    out = analyze_recent_changes(log)
    for analysis in out:
        assert set(analysis.windows.keys()) == set(DEFAULT_WINDOWS)


def test_analyze_detects_post_drop_with_negative_g() -> None:
    log = _make_log(pre_lam=30, post_lam=10)
    out = analyze_recent_changes(log, k=1)
    change_analysis = out[0]
    # The 56-day window has plenty of n; effect is large and negative.
    w56 = change_analysis.windows[56]
    assert w56.hedges_g is not None and w56.hedges_g < -1.0
    assert w56.mann_whitney_p is not None and w56.mann_whitney_p < 0.001


def test_analyze_flags_overlapping_windows() -> None:
    """A second change inside the post-window of the previous change should flip the overlap flag."""
    log = _make_log(
        change_day=90,
        extra_changes=[(100, {"Clobazam": (Dose(3.0, "am"),)})],
    )
    out = analyze_recent_changes(log, k=2)
    # The change on day 90 has another change 10 days later → post-14 and post-28 contain it.
    change_at_90 = [a for a in out if a.change_date == log.daily.index[90]][0]
    assert change_at_90.windows[14].post_contains_other_change is True
    assert change_at_90.windows[28].post_contains_other_change is True


def test_analyze_delta_string_for_dose_increase() -> None:
    log = _make_log()
    out = analyze_recent_changes(log, k=1)
    # Second change increased Clobazam am from 1.0 to 2.0.
    assert "Clobazam 1.0am→2.0am" in out[0].delta_str


def test_analyze_delta_initial_when_no_previous() -> None:
    log = _make_log()
    out = analyze_recent_changes(log, k=2)
    first = [a for a in out if a.change_date == log.daily.index[0]][0]
    assert first.delta_str == "initial regimen"


def test_analyze_empty_log_returns_empty_list() -> None:
    log = SeizureLog(
        daily=pd.DataFrame({"count": [], "daily_total_recorded": [], "mismatch": []}).astype(
            {"count": int, "daily_total_recorded": int, "mismatch": bool}
        ),
        clusters=pd.DataFrame(),
        med_changes=[],
        mismatches=pd.DataFrame(),
        excluded_dates=pd.DatetimeIndex([]),
    )
    assert analyze_recent_changes(log) == []


def test_analyze_returns_pre_post_analysis_dataclass() -> None:
    log = _make_log()
    out = analyze_recent_changes(log, k=1)
    assert isinstance(out[0], PrePostAnalysis)
    assert isinstance(out[0].windows[14].pre, MeanCI)


# --------------------------------------------------------------------------- #
# notable_days                                                                #
# --------------------------------------------------------------------------- #


def test_notable_days_flags_spike_above_trailing() -> None:
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    counts = np.full(60, 10, dtype=int)
    counts[40] = 50  # large spike well above mean+2sd of steady 10s
    daily = pd.DataFrame(
        {"count": counts, "daily_total_recorded": counts, "mismatch": [False] * 60},
        index=dates,
    )
    daily.index.name = "Date"
    result = notable_days(daily, window=28)
    assert dates[40] in result.index
    assert result.loc[dates[40], "count"] == 50


def test_notable_days_skips_early_days_without_enough_history() -> None:
    dates = pd.date_range("2026-01-01", periods=10, freq="D")
    counts = np.array([100, 1, 1, 1, 1, 1, 1, 1, 1, 1])  # day 0 is huge but no history
    daily = pd.DataFrame(
        {"count": counts, "daily_total_recorded": counts, "mismatch": [False] * 10},
        index=dates,
    )
    daily.index.name = "Date"
    result = notable_days(daily, window=28)
    assert result.empty  # no day has 28 days of prior history


def test_notable_days_uses_strictly_trailing_window() -> None:
    """The spiking day's own value must not be part of its own mean/sd."""
    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    counts = np.full(60, 10, dtype=int)
    counts[40] = 80
    daily = pd.DataFrame(
        {"count": counts, "daily_total_recorded": counts, "mismatch": [False] * 60},
        index=dates,
    )
    daily.index.name = "Date"
    result = notable_days(daily, window=28)
    # Trailing mean for day 40 = mean of days 12..39 = 10 (since the spike isn't included).
    assert result.loc[dates[40], "trailing_mean"] == 10.0


# --------------------------------------------------------------------------- #
# rescue_event_dates                                                          #
# --------------------------------------------------------------------------- #


def test_rescue_event_dates_unions_flags_and_notes() -> None:
    dates = pd.date_range("2026-01-01", periods=10, freq="D")
    clusters = pd.DataFrame(
        {
            "Date": [dates[0], dates[2], dates[5]],
            "flag_tokens": [
                frozenset({"rescue_meds_given"}),
                frozenset(),
                frozenset({"rescue_meds_given", "ended_in_tonic"}),
            ],
        }
    )
    daily = pd.DataFrame(
        {"count": [0] * 10, "daily_total_recorded": [0] * 10, "mismatch": [False] * 10},
        index=dates,
    )
    daily.index.name = "Date"
    log = SeizureLog(
        daily=daily,
        clusters=clusters,
        med_changes=[
            MedChange(date=dates[1], raw="Gave rescue meds at 8pm", regimen={}),
            MedChange(date=dates[3], raw="general narrative note", regimen={}),
            MedChange(date=dates[7], raw="Clobazam 1mL am", regimen={"Clobazam": (Dose(1.0, "am"),)}),
        ],
        mismatches=pd.DataFrame(),
        excluded_dates=pd.DatetimeIndex([]),
    )
    result = rescue_event_dates(log)
    # Flag dates: 0, 5. Note-with-rescue date: 1. Plain note (no "rescue"): NOT included.
    # Real med change: NOT included.
    assert result == {dates[0], dates[1], dates[5]}
