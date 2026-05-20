"""Raw extraction of free-text notes from the seizure log.

Public API: :func:`extract_notes`.

Surfaces every non-blank ``Notes`` / ``Diet`` cell in the lookback window so the
user can skim them verbatim before a clinic visit. Deliberately mechanical: no
keyword categorization, no paraphrase, no summarization — the clinical signal is
in the user's own text and must be read directly.

Per CLAUDE.md and the project's "logged days only" rule for this extractor:

* ``All Data.Notes`` / ``All Data.Diet`` cells are included only on dates whose
  Day Status is **not** ``unmonitored`` or ``uncertain``. Dates with no Cluster
  Detail rows count as logged.
* ``Cluster Detail.Notes`` rows inherit the ingest-layer Day Status filter:
  rows on excluded dates are already dropped from ``log.clusters``.
* The lookback window ends at ``log.latest_date`` (project-wide convention).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .ingest import ALL_DATA_SHEET, SeizureLog, load_log

DIET_COLUMN = "Diet"
NOTES_COLUMN = "Notes"
CLUSTER_NUM_COLUMN = "Cluster #"

_OUTPUT_COLUMNS = ["Date", "Source", "Notes"]


def extract_notes(log_path: str | Path, lookback_days: int) -> pd.DataFrame:
    """Return verbatim non-blank notes in the lookback window.

    Columns: ``Date``, ``Source``, ``Notes``. Sorted by Date descending, then
    Source ascending. ``Source`` is one of:

    * ``"All Data: Notes"``
    * ``"All Data: Diet"``
    * ``"Cluster Detail: Cluster N"`` (N = ``Cluster #`` from that row)
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")

    log = load_log(log_path)
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback_days - 1)

    rows: list[dict[str, object]] = []
    rows.extend(_extract_all_data_rows(Path(log_path), log, start, end))
    rows.extend(_extract_cluster_rows(log, start, end))

    out = pd.DataFrame(rows, columns=_OUTPUT_COLUMNS)
    if out.empty:
        return out
    return out.sort_values(["Date", "Source"], ascending=[False, True]).reset_index(drop=True)


def _extract_all_data_rows(
    path: Path,
    log: SeizureLog,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, object]]:
    all_data = pd.read_excel(path, sheet_name=ALL_DATA_SHEET)
    all_data["Date"] = pd.to_datetime(all_data["Date"], errors="coerce").dt.normalize()
    all_data = all_data.dropna(subset=["Date"])

    in_window = all_data["Date"].between(start, end)
    logged = ~all_data["Date"].isin(log.excluded_dates)
    subset = all_data.loc[in_window & logged]

    rows: list[dict[str, object]] = []
    for column, source in ((NOTES_COLUMN, "All Data: Notes"), (DIET_COLUMN, "All Data: Diet")):
        if column not in subset.columns:
            continue
        for _, row in subset.iterrows():
            text = _clean_cell(row[column])
            if text:
                rows.append({"Date": row["Date"], "Source": source, "Notes": text})
    return rows


def _extract_cluster_rows(
    log: SeizureLog,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, object]]:
    if NOTES_COLUMN not in log.clusters.columns:
        return []
    in_window = log.clusters["Date"].between(start, end)
    subset = log.clusters.loc[in_window, ["Date", CLUSTER_NUM_COLUMN, NOTES_COLUMN]]

    rows: list[dict[str, object]] = []
    for _, row in subset.iterrows():
        text = _clean_cell(row[NOTES_COLUMN])
        if not text:
            continue
        cluster_label = _cluster_label(row[CLUSTER_NUM_COLUMN])
        rows.append(
            {
                "Date": row["Date"],
                "Source": f"Cluster Detail: {cluster_label}",
                "Notes": text,
            }
        )
    return rows


def _clean_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    return text


def _cluster_label(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Cluster ?"
    try:
        return f"Cluster {int(value)}"
    except (TypeError, ValueError):
        return f"Cluster {value}"
