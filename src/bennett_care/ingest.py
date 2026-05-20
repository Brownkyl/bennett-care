"""Parse the seizure log Excel file into structured objects.

Public API: :func:`load_log`.

See ``CLAUDE.md`` ("Data model" + "Phase 1 design decisions") for the
contract this module fulfills:

* Daily seizure counts are recomputed from ``Cluster Detail`` (source of truth).
* Mismatches with the ``Daily Total`` column are surfaced, not raised.
* Dates with Day Status in {``unmonitored``, ``uncertain``} are excluded.
* ``Flags`` are exact-token matched after splitting on ``,``.
* ``Meds`` entries are semicolon-separated; drug name carries forward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pandas as pd

ALL_DATA_SHEET = "All Data"
CLUSTER_DETAIL_SHEET = "Cluster Detail"

EXCLUDED_DAY_STATUSES: frozenset[str] = frozenset({"unmonitored", "uncertain"})

MEDS_COLUMN = "Meds (first day of updated meds noted)"
DAILY_TOTAL_COLUMN = "Daily Total"

# Matches "Drugname 1.5 mL am" or "1.5mL pm" (drug optional, inherits previous when omitted).
_DOSE_PATTERN = re.compile(
    r"""
    ^\s*
    (?:(?P<drug>[A-Za-z][A-Za-z\-]*(?:\s+[A-Za-z][A-Za-z\-]*)*)\s+)?
    (?P<amount>\d+(?:\.\d+)?)
    \s*m[lL]\s+
    (?P<timing>[A-Za-z0-9]+)
    \s*$
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Dose:
    """A single dose: amount in mL at a given timing label (``am``, ``pm``, ``3x``, ...)."""

    amount_ml: float
    timing: str


@dataclass(frozen=True)
class MedChange:
    """A medication change recorded on ``date``.

    ``regimen`` maps normalized drug name to the doses listed for that drug on that date.
    Drug names are Title-cased for stable identity (``clobazam`` and ``Clobazam`` merge).
    """

    date: pd.Timestamp
    raw: str
    regimen: dict[str, tuple[Dose, ...]]
    unparsed_fragments: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class SeizureLog:
    """Parsed seizure log.

    Attributes:
        daily: DataFrame indexed by Date with columns ``count`` (recomputed from
            Cluster Detail), ``daily_total_recorded`` (from All Data), and
            ``mismatch`` (bool). Excludes dates whose Day Status is excluded.
        clusters: Cluster Detail rows that survived the Day Status filter. Columns
            are the original Excel columns plus ``flag_tokens`` (frozenset of
            individual tokens) and ``hour`` (int 0-23 from Start Time, or NaN).
        med_changes: ordered list of MedChange records.
        mismatches: DataFrame of dates where Daily Total ≠ recomputed count.
        excluded_dates: DatetimeIndex of dates dropped due to Day Status.
    """

    daily: pd.DataFrame
    clusters: pd.DataFrame
    med_changes: list[MedChange]
    mismatches: pd.DataFrame
    excluded_dates: pd.DatetimeIndex

    @property
    def latest_date(self) -> pd.Timestamp:
        return self.daily.index.max()

    @property
    def earliest_date(self) -> pd.Timestamp:
        return self.daily.index.min()


def load_log(path: str | Path) -> SeizureLog:
    """Load and parse the seizure log workbook at ``path``."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Seizure log not found: {path}")

    all_data = _read_all_data(path)
    cluster_detail_raw = _read_cluster_detail(path)

    excluded_dates = _excluded_dates(cluster_detail_raw)
    clusters = _clean_clusters(cluster_detail_raw, excluded_dates)
    daily = _compute_daily_series(all_data, clusters, excluded_dates)
    mismatches = _mismatches(daily)
    med_changes = _parse_med_changes(all_data)

    return SeizureLog(
        daily=daily,
        clusters=clusters,
        med_changes=med_changes,
        mismatches=mismatches,
        excluded_dates=excluded_dates,
    )


# --------------------------------------------------------------------------- #
# Sheet readers                                                               #
# --------------------------------------------------------------------------- #


def _read_all_data(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=ALL_DATA_SHEET)
    # Drop the trailing "Unnamed: N" columns Excel pads in.
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed:")]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["Date"]).reset_index(drop=True)
    return df


def _read_cluster_detail(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=CLUSTER_DETAIL_SHEET)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["Date"]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# Day Status / exclusions                                                     #
# --------------------------------------------------------------------------- #


def _excluded_dates(cluster_detail: pd.DataFrame) -> pd.DatetimeIndex:
    status = cluster_detail["Day Status"].fillna("").str.strip().str.lower()
    mask = status.isin(EXCLUDED_DAY_STATUSES)
    return pd.DatetimeIndex(sorted(cluster_detail.loc[mask, "Date"].unique()))


def _clean_clusters(cluster_detail: pd.DataFrame, excluded: pd.DatetimeIndex) -> pd.DataFrame:
    df = cluster_detail.loc[~cluster_detail["Date"].isin(excluded)].copy()
    df["Seizure Type"] = df["Seizure Type"].fillna("").astype(str).str.strip().str.lower()
    df["flag_tokens"] = df["Flags"].apply(_split_flags)
    df["hour"] = df["Start Time"].apply(_parse_hour)
    df["Seizure Count"] = pd.to_numeric(df["Seizure Count"], errors="coerce")
    df["Duration (min)"] = pd.to_numeric(df["Duration (min)"], errors="coerce")
    return df.reset_index(drop=True)


def _split_flags(value: object) -> frozenset[str]:
    if pd.isna(value):
        return frozenset()
    text = str(value)
    return frozenset(tok.strip() for tok in text.split(",") if tok.strip())


def _parse_hour(value: object) -> float:
    """Best-effort hour extraction from Start Time. Returns NaN on failure."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return float("nan")
    if hasattr(value, "hour") and not isinstance(value, str):
        try:
            return float(value.hour)
        except Exception:
            pass
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        return float("nan")
    return float(parsed.hour)


# --------------------------------------------------------------------------- #
# Daily series                                                                #
# --------------------------------------------------------------------------- #


def _compute_daily_series(
    all_data: pd.DataFrame,
    clusters: pd.DataFrame,
    excluded: pd.DatetimeIndex,
) -> pd.DataFrame:
    monitored = all_data.loc[~all_data["Date"].isin(excluded)].copy()
    monitored = monitored.set_index("Date").sort_index()

    # default sum(skipna=True) returns 0 for all-NaN groups, which is what we want
    # for dates that exist in Cluster Detail but have unrecorded Seizure Count values.
    recomputed = (
        clusters.groupby("Date")["Seizure Count"]
        .sum()
        .reindex(monitored.index, fill_value=0)
    )

    recorded = pd.to_numeric(monitored.get(DAILY_TOTAL_COLUMN), errors="coerce").fillna(0)

    daily = pd.DataFrame(
        {
            "count": recomputed.astype(int),
            "daily_total_recorded": recorded.astype(int),
        },
        index=monitored.index,
    )
    daily.index.name = "Date"
    daily["mismatch"] = daily["count"] != daily["daily_total_recorded"]
    return daily


def _mismatches(daily: pd.DataFrame) -> pd.DataFrame:
    return daily.loc[daily["mismatch"], ["count", "daily_total_recorded"]].assign(
        diff=lambda d: d["count"] - d["daily_total_recorded"]
    )


# --------------------------------------------------------------------------- #
# Med changes                                                                 #
# --------------------------------------------------------------------------- #


def _parse_med_changes(all_data: pd.DataFrame) -> list[MedChange]:
    if MEDS_COLUMN not in all_data.columns:
        return []
    rows = all_data.loc[all_data[MEDS_COLUMN].notna(), ["Date", MEDS_COLUMN]]
    changes: list[MedChange] = []
    for _, row in rows.iterrows():
        raw = str(row[MEDS_COLUMN]).strip()
        if not raw:
            continue
        regimen, unparsed = _parse_meds_entry(raw)
        changes.append(
            MedChange(
                date=row["Date"],
                raw=raw,
                regimen=regimen,
                unparsed_fragments=tuple(unparsed),
            )
        )
    changes.sort(key=lambda c: c.date)
    return changes


def _parse_meds_entry(text: str) -> tuple[dict[str, tuple[Dose, ...]], list[str]]:
    """Parse a Meds cell into ``{drug: (Dose, ...)}`` and a list of unparsed fragments."""
    regimen: dict[str, list[Dose]] = {}
    unparsed: list[str] = []
    current_drug: str | None = None
    for raw_fragment in text.split(";"):
        fragment = raw_fragment.strip()
        if not fragment:
            continue
        match = _DOSE_PATTERN.match(fragment)
        if not match:
            unparsed.append(fragment)
            continue
        drug_match = match.group("drug")
        if drug_match:
            current_drug = _normalize_drug(drug_match)
        if current_drug is None:
            unparsed.append(fragment)
            continue
        dose = Dose(
            amount_ml=float(match.group("amount")),
            timing=match.group("timing").lower(),
        )
        regimen.setdefault(current_drug, []).append(dose)
    return {drug: tuple(doses) for drug, doses in regimen.items()}, unparsed


def _normalize_drug(name: str) -> str:
    return " ".join(part.capitalize() for part in name.strip().split())


# --------------------------------------------------------------------------- #
# Convenience: flag counts                                                    #
# --------------------------------------------------------------------------- #


def count_flag(clusters: pd.DataFrame, token: str | Iterable[str]) -> int:
    """Count clusters carrying any of the given flag token(s)."""
    tokens = {token} if isinstance(token, str) else set(token)
    return int(clusters["flag_tokens"].apply(lambda s: bool(s & tokens)).sum())
