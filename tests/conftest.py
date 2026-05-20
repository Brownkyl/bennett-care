"""Shared pytest fixtures.

Fixtures here build synthetic seizure-log workbooks so tests never touch the
real ``data/seizure_log.xlsx``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def sample_log_path(tmp_path: Path) -> Path:
    """A synthetic 7-day log exercising the contracts in ingest.py:

    * 01-01: 3 clusters summing to 3 seizures. Daily Total = 3. (no mismatch)
    * 01-02, 01-03, 01-04: zero-seizure days (no Cluster Detail rows). Daily Total = 0.
    * 01-05: 2 clusters summing to 4 seizures, but Daily Total recorded as 5. (mismatch)
    * 01-06: 1 cluster, Day Status = ``unmonitored``. Must be excluded.
    * 01-07: 1 cluster, Day Status = ``uncertain``. Must be excluded.

    Med changes recorded on 01-01 (Clobazam, drug-led + inherited), 01-03 (Keppra),
    and 01-05 (Epidiolex, multi-drug-style).
    """
    path = tmp_path / "sample.xlsx"
    dates = pd.to_datetime(
        [
            "2026-01-01",
            "2026-01-02",
            "2026-01-03",
            "2026-01-04",
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
        ]
    )
    all_data = pd.DataFrame(
        {
            "Date": dates,
            "Cluster 1": [1, None, None, None, 2, None, None],
            "Daily Total": [3, 0, 0, 0, 5, 5, 5],
            "7 Day Avg": [None] * 7,
            "14 Day Avg": [None] * 7,
            "Meds (first day of updated meds noted)": [
                "Clobazam 1mL am; 4mL pm",
                None,
                "Keppra 2mL 3x",
                None,
                "Epidiolex 0.9mL am; 1.5mL pm",
                None,
                None,
            ],
            "Diet": [None] * 7,
            "Notes": [None] * 7,
        }
    )

    cluster_detail = pd.DataFrame(
        {
            "Date": pd.to_datetime(
                [
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-05",
                    "2026-01-05",
                    "2026-01-06",
                    "2026-01-07",
                ]
            ),
            "Cluster #": [1, 2, 3, 1, 2, 1, 1],
            "Start Time": [
                "8:00 AM",
                "10:30 AM",
                "2:15 PM",
                "9:00 AM",
                "5:45 PM",
                "12:00 PM",
                "8:00 AM",
            ],
            "Duration (min)": [1.5, 2.0, 1.0, 0.5, 1.0, 1.0, 1.0],
            "Seizure Count": [1, 1, 1, 2, 2, 5, 5],
            "Seizure Type": [
                "Atonic",
                "atonic ",
                "Tonic",
                "MYOCLONIC",
                "atonic",
                "atonic",
                "tonic",
            ],
            "Day Status": [
                "logged",
                "logged",
                "logged",
                "logged",
                "logged",
                "unmonitored",
                "uncertain",
            ],
            "Verified": ["Y", "Y", "Y", "Y", None, None, None],
            "Flags": [
                None,
                "rescue_meds_given",
                "ended_in_tonic",
                "rescue_meds_given, ended_in_tonic",
                "school_data",
                None,
                None,
            ],
            "Notes": [None] * 7,
        }
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        all_data.to_excel(writer, sheet_name="All Data", index=False)
        cluster_detail.to_excel(writer, sheet_name="Cluster Detail", index=False)

    return path


@pytest.fixture
def notes_log_path(tmp_path: Path) -> Path:
    """Synthetic log for exercising the notes extractor.

    Latest date 2026-01-10. With ``lookback_days=7`` the window is
    [2026-01-04, 2026-01-10] inclusive.

    * 01-03 (logged): Notes set — outside the 7-day window
    * 01-04 (logged): Notes set — earliest in-window day
    * 01-05 (logged, no clusters): Notes set — zero-seizure day still counts
    * 01-06 (uncertain): Notes set + cluster Notes set — both must be excluded
    * 01-07 (unmonitored): Notes set + cluster Notes set — both must be excluded
    * 01-08 (logged): Notes set + 1 cluster with Notes set
    * 01-09 (logged): Notes blank, Diet whitespace-only — both skipped
    * 01-10 (logged): both Notes and Diet set
    """
    path = tmp_path / "notes_sample.xlsx"
    dates = pd.to_datetime(
        [
            "2026-01-03",
            "2026-01-04",
            "2026-01-05",
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
            "2026-01-09",
            "2026-01-10",
        ]
    )
    all_data = pd.DataFrame(
        {
            "Date": dates,
            "Cluster 1": [None, None, None, 1, 1, 1, None, None],
            "Daily Total": [0, 0, 0, 1, 1, 1, 0, 0],
            "Meds (first day of updated meds noted)": [None] * 8,
            "Diet": [None, None, None, None, None, None, "   ", "ate well"],
            "Notes": [
                "OUT OF WINDOW",
                "edge of window",
                "zero day, fine",
                "ambiguous day",
                "unmonitored sleepover",
                "rescue at 9pm",
                None,
                "Slept poorly",
            ],
        }
    )
    cluster_detail = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-01-06", "2026-01-07", "2026-01-08"]),
            "Cluster #": [1, 1, 1],
            "Start Time": ["9:00 AM", "9:00 AM", "9:00 AM"],
            "Duration (min)": [1.0, 1.0, 1.0],
            "Seizure Count": [1, 1, 1],
            "Seizure Type": ["atonic", "atonic", "atonic"],
            "Day Status": ["uncertain", "unmonitored", "logged"],
            "Verified": [None, None, "Y"],
            "Flags": [None, None, None],
            "Notes": [
                "uncertain cluster should not appear",
                "unmonitored cluster should not appear",
                "duration approx",
            ],
        }
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        all_data.to_excel(writer, sheet_name="All Data", index=False)
        cluster_detail.to_excel(writer, sheet_name="Cluster Detail", index=False)
    return path
