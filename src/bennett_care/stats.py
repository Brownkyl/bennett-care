"""Pre/post statistics for the two most recent med changes.

Implements the locked Phase 1 decisions (see CLAUDE.md):

* Bootstrap BCa 95% CI on mean daily count, 10,000 resamples, fixed seed.
* Hedges' g (small-sample-corrected Cohen's d) on (post - pre).
* Mann-Whitney U two-sided p-value as a non-parametric supplement.
* Sample windows of 14, 28, 56 days. Pre = [D-W, D-1]; post = [D, D+W-1].
* Windows that cross another real med-change date are flagged but still computed.

This module reports facts only — no causal language, no thresholds for
"significant" or "responded." Interpretation is the user's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from scipy.stats import bootstrap, mannwhitneyu

from .ingest import Dose, MedChange, SeizureLog

BOOTSTRAP_SEED: int = 42
BOOTSTRAP_RESAMPLES: int = 10_000
DEFAULT_WINDOWS: tuple[int, ...] = (14, 28, 56)
DEFAULT_TOP_K: int = 2


@dataclass(frozen=True)
class MeanCI:
    """Mean with bootstrap 95% CI. low == high == mean when CI is undefined (n<2 or zero variance)."""

    mean: float
    low: float
    high: float
    n: int


@dataclass(frozen=True)
class PrePostWindow:
    """Statistics for a single window size around one med change.

    ``hedges_g`` is positive when post > pre (seizures increased), negative when post < pre.
    Both ``hedges_g`` and ``mann_whitney_p`` are None when either side has <2 samples.
    """

    days: int
    pre: MeanCI
    post: MeanCI
    hedges_g: float | None
    mann_whitney_p: float | None
    pre_contains_other_change: bool
    post_contains_other_change: bool


@dataclass(frozen=True)
class PrePostAnalysis:
    """Pre/post analysis for one med change across the configured window sizes."""

    change_date: pd.Timestamp
    regimen_str: str
    delta_str: str
    windows: dict[int, PrePostWindow]


# --------------------------------------------------------------------------- #
# Public                                                                      #
# --------------------------------------------------------------------------- #


def analyze_recent_changes(
    log: SeizureLog,
    *,
    k: int = DEFAULT_TOP_K,
    window_sizes: tuple[int, ...] = DEFAULT_WINDOWS,
) -> list[PrePostAnalysis]:
    """Analyze the ``k`` most recent *real* med changes (entries with non-empty regimen).

    Rescue-note entries (parsed regimen empty) are skipped per CLAUDE.md.
    Returns at most ``k`` analyses, ordered chronologically.
    """
    real_changes = [c for c in log.med_changes if c.regimen]
    if not real_changes:
        return []
    selected = real_changes[-k:]
    all_change_dates = [c.date for c in real_changes]

    analyses: list[PrePostAnalysis] = []
    for i, change in enumerate(real_changes):
        if change not in selected:
            continue
        prev = real_changes[i - 1] if i > 0 else None
        windows = {
            w: _pre_post_for_change(log.daily, change.date, w, all_change_dates)
            for w in window_sizes
        }
        analyses.append(
            PrePostAnalysis(
                change_date=change.date,
                regimen_str=format_regimen(change.regimen),
                delta_str=format_regimen_delta(prev.regimen if prev else None, change.regimen),
                windows=windows,
            )
        )
    return analyses


def mean_with_bootstrap_ci(
    values: Sequence[float] | np.ndarray,
    *,
    confidence: float = 0.95,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> MeanCI:
    """Mean and bootstrap BCa CI. Degenerate inputs (n<2 or zero variance) return a point estimate."""
    arr = np.asarray(values, dtype=float)
    n = arr.size
    if n == 0:
        nan = float("nan")
        return MeanCI(mean=nan, low=nan, high=nan, n=0)
    mean = float(np.mean(arr))
    if n < 2 or np.var(arr) == 0:
        return MeanCI(mean=mean, low=mean, high=mean, n=n)
    try:
        result = bootstrap(
            (arr,),
            np.mean,
            n_resamples=n_resamples,
            confidence_level=confidence,
            method="BCa",
            random_state=np.random.default_rng(seed),
        )
        low = float(result.confidence_interval.low)
        high = float(result.confidence_interval.high)
    except Exception:
        low = high = float("nan")
    return MeanCI(mean=mean, low=low, high=high, n=n)


def hedges_g(pre: Sequence[float] | np.ndarray, post: Sequence[float] | np.ndarray) -> float | None:
    """Small-sample-corrected Cohen's d on (post - pre).

    Positive when post > pre. Returns None if either side has <2 samples or pooled SD is zero.
    """
    pre_arr = np.asarray(pre, dtype=float)
    post_arr = np.asarray(post, dtype=float)
    n1, n2 = pre_arr.size, post_arr.size
    if n1 < 2 or n2 < 2:
        return None
    var1 = float(np.var(pre_arr, ddof=1))
    var2 = float(np.var(post_arr, ddof=1))
    pooled_sd = float(np.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2)))
    if pooled_sd == 0:
        return None
    cohens_d = (float(np.mean(post_arr)) - float(np.mean(pre_arr))) / pooled_sd
    correction = 1.0 - 3.0 / (4.0 * (n1 + n2) - 9.0)
    return cohens_d * correction


# --------------------------------------------------------------------------- #
# Internals                                                                   #
# --------------------------------------------------------------------------- #


def _pre_post_for_change(
    daily: pd.DataFrame,
    change_date: pd.Timestamp,
    days: int,
    all_change_dates: Iterable[pd.Timestamp],
) -> PrePostWindow:
    pre_start = change_date - pd.Timedelta(days=days)
    pre_end = change_date - pd.Timedelta(days=1)
    post_start = change_date
    post_end = change_date + pd.Timedelta(days=days - 1)

    pre_vals = _window_counts(daily, pre_start, pre_end)
    post_vals = _window_counts(daily, post_start, post_end)

    others = [d for d in all_change_dates if d != change_date]
    pre_overlap = any(pre_start <= d <= pre_end for d in others)
    post_overlap = any(post_start <= d <= post_end for d in others)

    return PrePostWindow(
        days=days,
        pre=mean_with_bootstrap_ci(pre_vals),
        post=mean_with_bootstrap_ci(post_vals),
        hedges_g=hedges_g(pre_vals, post_vals),
        mann_whitney_p=_mann_whitney(pre_vals, post_vals),
        pre_contains_other_change=pre_overlap,
        post_contains_other_change=post_overlap,
    )


def _window_counts(daily: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> np.ndarray:
    mask = (daily.index >= start) & (daily.index <= end)
    return daily.loc[mask, "count"].to_numpy(dtype=float)


def _mann_whitney(pre: np.ndarray, post: np.ndarray) -> float | None:
    if pre.size < 1 or post.size < 1:
        return None
    if pre.size < 2 and post.size < 2:
        return None
    try:
        result = mannwhitneyu(pre, post, alternative="two-sided")
        return float(result.pvalue)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Public helpers used by visualize + report                                   #
# --------------------------------------------------------------------------- #


def notable_days(
    daily: pd.DataFrame,
    *,
    threshold_sd: float = 2.0,
    window: int = 28,
) -> pd.DataFrame:
    """Days where the count exceeds (trailing 28-day mean + threshold_sd × trailing SD).

    Per CLAUDE.md: trailing window is *strictly* trailing (the day itself is excluded
    from its own mean/SD). Days with fewer than ``window`` prior monitored days are
    skipped silently (insufficient history). Returns a DataFrame indexed by date with
    columns ``count``, ``trailing_mean``, ``trailing_std``, ``z_score``.
    """
    counts = daily["count"]
    shifted = counts.shift(1)
    trailing_mean = shifted.rolling(window=window, min_periods=window).mean()
    trailing_std = shifted.rolling(window=window, min_periods=window).std()
    safe_std = trailing_std.where(trailing_std > 0)
    threshold = trailing_mean + threshold_sd * trailing_std
    is_notable = counts > threshold
    mask = is_notable & trailing_mean.notna() & trailing_std.notna()
    return pd.DataFrame(
        {
            "count": counts[mask].astype(int),
            "trailing_mean": trailing_mean[mask].round(2),
            "trailing_std": trailing_std[mask].round(2),
            "z_score": ((counts[mask] - trailing_mean[mask]) / safe_std[mask]).round(2),
        }
    )


def rescue_event_dates(log: SeizureLog) -> set[pd.Timestamp]:
    """All dates with rescue-event evidence: ``rescue_meds_given`` cluster flag OR
    a Meds-column entry that parsed to no regimen and mentions "rescue"."""
    flag_mask = log.clusters["flag_tokens"].apply(lambda s: "rescue_meds_given" in s)
    flag_dates = set(log.clusters.loc[flag_mask, "Date"])
    note_dates = {
        c.date
        for c in log.med_changes
        if not c.regimen and "rescue" in c.raw.lower()
    }
    return flag_dates | note_dates


def real_changes_with_prev(
    log: SeizureLog,
) -> list[tuple[MedChange, MedChange | None]]:
    """Return ``(change, previous_real_change_or_None)`` for every real med change.

    Rescue-note entries (empty regimen) are excluded — they don't represent
    regimen changes, so they're never the "previous" for delta computation.
    """
    real = [c for c in log.med_changes if c.regimen]
    return [(c, real[i - 1] if i > 0 else None) for i, c in enumerate(real)]


def format_regimen(regimen: dict[str, tuple[Dose, ...]]) -> str:
    """Render a regimen as ``"Drug A/B; Drug X/Y"``."""
    parts = []
    for drug, doses in regimen.items():
        dose_str = "/".join(f"{d.amount_ml}{d.timing}" for d in doses)
        parts.append(f"{drug} {dose_str}")
    return "; ".join(parts)


def format_regimen_delta(
    prev: dict[str, tuple[Dose, ...]] | None,
    curr: dict[str, tuple[Dose, ...]],
) -> str:
    """Compact human-readable delta from ``prev`` to ``curr`` regimen.

    Examples:
        "Epidiolex 1.4pm→1.5pm"
        "+ Rufinamide 0.5am/0.5pm"
        "- Topiramate"
    """
    if prev is None:
        return "initial regimen"

    diffs: list[str] = []
    all_drugs = set(prev) | set(curr)
    for drug in sorted(all_drugs):
        prev_doses = prev.get(drug)
        curr_doses = curr.get(drug)
        if prev_doses is None:
            doses_str = "/".join(f"{d.amount_ml}{d.timing}" for d in curr_doses or ())
            diffs.append(f"+ {drug} {doses_str}")
        elif curr_doses is None:
            diffs.append(f"- {drug}")
        elif prev_doses != curr_doses:
            for p, c in _align_doses(prev_doses, curr_doses):
                if p is None:
                    diffs.append(f"+ {drug} {c.amount_ml}{c.timing}")
                elif c is None:
                    diffs.append(f"- {drug} {p.amount_ml}{p.timing}")
                elif p.amount_ml != c.amount_ml:
                    diffs.append(f"{drug} {p.amount_ml}{p.timing}→{c.amount_ml}{c.timing}")
    return ", ".join(diffs) if diffs else "no change vs. previous"


def _align_doses(
    prev: tuple[Dose, ...], curr: tuple[Dose, ...]
) -> list[tuple[Dose | None, Dose | None]]:
    """Align doses across two regimens by ``timing`` so we can compare amounts at am/pm/etc."""
    by_timing_prev = {d.timing: d for d in prev}
    by_timing_curr = {d.timing: d for d in curr}
    pairs: list[tuple[Dose | None, Dose | None]] = []
    for timing in sorted(set(by_timing_prev) | set(by_timing_curr)):
        pairs.append((by_timing_prev.get(timing), by_timing_curr.get(timing)))
    return pairs
