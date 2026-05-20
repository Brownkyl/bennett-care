"""Tests for bennett_care.ingest."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from bennett_care.ingest import (
    Dose,
    MedChange,
    SeizureLog,
    count_flag,
    load_log,
)


# --------------------------------------------------------------------------- #
# load_log basics                                                             #
# --------------------------------------------------------------------------- #


def test_load_returns_seizure_log(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert isinstance(log, SeizureLog)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_log(tmp_path / "does-not-exist.xlsx")


# --------------------------------------------------------------------------- #
# Daily series                                                                #
# --------------------------------------------------------------------------- #


def test_daily_index_excludes_unmonitored_and_uncertain(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert pd.Timestamp("2026-01-06") not in log.daily.index
    assert pd.Timestamp("2026-01-07") not in log.daily.index
    # The 5 monitored days remain.
    assert len(log.daily) == 5


def test_excluded_dates_property(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert list(log.excluded_dates) == [
        pd.Timestamp("2026-01-06"),
        pd.Timestamp("2026-01-07"),
    ]


def test_daily_count_recomputed_from_cluster_detail(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    # 01-05: All Data says 5; Cluster Detail sum is 4. Use 4 (source of truth).
    assert log.daily.loc[pd.Timestamp("2026-01-05"), "count"] == 4
    assert log.daily.loc[pd.Timestamp("2026-01-05"), "daily_total_recorded"] == 5


def test_zero_seizure_days_are_zero_not_missing(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    for d in ["2026-01-02", "2026-01-03", "2026-01-04"]:
        assert log.daily.loc[pd.Timestamp(d), "count"] == 0


def test_matching_day_has_no_mismatch(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert not log.daily.loc[pd.Timestamp("2026-01-01"), "mismatch"]
    assert pd.Timestamp("2026-01-01") not in log.mismatches.index


def test_mismatch_surfaced_with_diff(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert log.daily.loc[pd.Timestamp("2026-01-05"), "mismatch"]
    row = log.mismatches.loc[pd.Timestamp("2026-01-05")]
    assert row["count"] == 4
    assert row["daily_total_recorded"] == 5
    assert row["diff"] == -1


def test_latest_and_earliest_date(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert log.earliest_date == pd.Timestamp("2026-01-01")
    assert log.latest_date == pd.Timestamp("2026-01-05")


# --------------------------------------------------------------------------- #
# Clusters                                                                    #
# --------------------------------------------------------------------------- #


def test_excluded_dates_removed_from_clusters(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert log.clusters["Date"].isin(log.excluded_dates).sum() == 0
    assert len(log.clusters) == 5  # 7 raw - 2 excluded


def test_seizure_type_normalized(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    # Originals: "Atonic", "atonic ", "Tonic", "MYOCLONIC", "atonic"
    assert sorted(log.clusters["Seizure Type"].unique()) == ["atonic", "myoclonic", "tonic"]


def test_flag_tokens_split_on_comma(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    multi = log.clusters.loc[log.clusters["Date"] == pd.Timestamp("2026-01-05")].iloc[0]
    assert multi["flag_tokens"] == frozenset({"rescue_meds_given", "ended_in_tonic"})


def test_flag_tokens_empty_for_blank(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    no_flags = log.clusters.iloc[0]  # 01-01 cluster #1, Flags=None
    assert no_flags["flag_tokens"] == frozenset()


def test_count_flag_single_token(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    # rescue_meds_given appears on: cluster #2 on 01-01, and the multi-flag cluster on 01-05
    assert count_flag(log.clusters, "rescue_meds_given") == 2


def test_count_flag_multiple_tokens(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    # Only one cluster carries school_data (school_data_pending absent in fixture)
    assert count_flag(log.clusters, ["school_data", "school_data_pending"]) == 1


def test_start_time_to_hour(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    hours = sorted(log.clusters["hour"].dropna().tolist())
    # Fixture times on monitored days: 8, 10, 14 (01-01) and 9, 17 (01-05)
    assert hours == [8.0, 9.0, 10.0, 14.0, 17.0]


# --------------------------------------------------------------------------- #
# Med changes                                                                 #
# --------------------------------------------------------------------------- #


def test_med_changes_count_and_order(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert [c.date for c in log.med_changes] == [
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-03"),
        pd.Timestamp("2026-01-05"),
    ]


def test_med_change_drug_carry_forward(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    clobazam_change = log.med_changes[0]
    # "Clobazam 1mL am; 4mL pm" — drug name carries forward to second fragment
    assert clobazam_change.regimen == {
        "Clobazam": (Dose(amount_ml=1.0, timing="am"), Dose(amount_ml=4.0, timing="pm"))
    }
    assert clobazam_change.unparsed_fragments == ()


def test_med_change_three_times_daily(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    keppra_change = log.med_changes[1]
    assert keppra_change.regimen == {"Keppra": (Dose(amount_ml=2.0, timing="3x"),)}


def test_med_change_decimal_amount(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    epidiolex_change = log.med_changes[2]
    assert epidiolex_change.regimen == {
        "Epidiolex": (Dose(amount_ml=0.9, timing="am"), Dose(amount_ml=1.5, timing="pm"))
    }


def test_blank_med_rows_skipped(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    # 4 rows have None in Meds; only 3 should produce MedChange entries.
    assert len(log.med_changes) == 3


def test_med_change_raw_text_preserved(sample_log_path: Path) -> None:
    log = load_log(sample_log_path)
    assert log.med_changes[0].raw == "Clobazam 1mL am; 4mL pm"


def test_unparsed_fragment_captured(tmp_path: Path) -> None:
    """An unrecognized fragment should not crash and should be reported."""
    path = tmp_path / "weird_meds.xlsx"
    all_data = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-02-01"]),
            "Daily Total": [0],
            "Meds (first day of updated meds noted)": [
                "Clobazam 1mL am; some narrative; 4mL pm"
            ],
        }
    )
    cluster_detail = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-02-01"]),
            "Cluster #": [1],
            "Start Time": ["9:00 AM"],
            "Duration (min)": [1.0],
            "Seizure Count": [0],
            "Seizure Type": ["atonic"],
            "Day Status": ["logged"],
            "Verified": [None],
            "Flags": [None],
            "Notes": [None],
        }
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        all_data.to_excel(writer, sheet_name="All Data", index=False)
        cluster_detail.to_excel(writer, sheet_name="Cluster Detail", index=False)

    log = load_log(path)
    change = log.med_changes[0]
    assert "Clobazam" in change.regimen
    assert "some narrative" in change.unparsed_fragments
