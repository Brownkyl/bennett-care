"""Tests for the literature-surveillance briefing pipeline.

Covers only the deterministic parts — age-eligibility, the cadence gate, the source
bundle and the renderer. The three model stages (triage, synthesis, verification) are
not exercised here; they need credentials and are non-deterministic by nature.

Age-eligibility is the piece that most needs testing: it is the one clinical fact in the
briefing that is computed rather than generated, and the whole point is that the model
cannot get it wrong.
"""

import os
from datetime import date

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")
os.environ.setdefault("GMAIL_FROM", "test@example.com")
os.environ.setdefault("GMAIL_TO", "test@example.com")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "surveillance"))

import run_surveillance as rs


TODAY = date(2026, 9, 4)   # patient is 3y6m on this date


class TestAgeParsing:
    @pytest.mark.parametrize("text,expected", [
        ("2 Years", 730.5),
        ("1 Year", 365.25),
        ("6 Months", 182.64),
        ("30 Days", 30.0),
    ])
    def test_parses_units(self, text, expected):
        assert rs._age_to_days(text) == pytest.approx(expected, rel=0.01)

    @pytest.mark.parametrize("text", ["", "N/A", "Child", None, "Years"])
    def test_unparseable_returns_none(self, text):
        assert rs._age_to_days(text) is None


class TestEligibility:
    def test_eligible_when_above_minimum(self):
        # LIGHTHOUSE clemizole: 2-55 years, patient is 3.5
        r = rs.eligibility_for_patient("2 Years", "55 Years", TODAY)
        assert r["verdict"] == "eligible_now"

    def test_not_yet_eligible_reports_when(self):
        # DISCOVER carisbamate opens at 4; patient turns 4 in Feb 2027
        r = rs.eligibility_for_patient("4 Years", "55 Years", TODAY)
        assert r["verdict"] == "eligible_later"
        assert "February 2027" in r["detail"]

    def test_cadet_dbs_excluded_under_five(self):
        # The "not available to children under five" case
        r = rs.eligibility_for_patient("5 Years", "14 Years", TODAY)
        assert r["verdict"] == "eligible_later"
        assert "2028" in r["detail"]

    def test_aged_out(self):
        r = rs.eligibility_for_patient("1 Year", "2 Years", TODAY)
        assert r["verdict"] == "aged_out"

    def test_boundary_exactly_on_birthday(self):
        # Minimum age 3 on the day the patient turns 3 -> eligible, not "later"
        assert rs.eligibility_for_patient("3 Years", "", date(2026, 2, 8))["verdict"] \
            == "eligible_now"

    def test_no_published_range(self):
        assert rs.eligibility_for_patient("", "", TODAY)["verdict"] == "unknown"

    def test_open_ended_maximum(self):
        assert rs.eligibility_for_patient("2 Years", "", TODAY)["verdict"] == "eligible_now"


class TestCadenceGate:
    def test_first_run_is_due(self):
        assert rs._due({}) is True

    def test_recent_run_is_not_due(self, monkeypatch):
        monkeypatch.setattr(rs, "FORCE_RUN", False)
        recent = (rs.datetime.now(rs.timezone.utc) - rs.timedelta(days=3)).isoformat()
        assert rs._due({"last_run": recent}) is False

    def test_old_run_is_due(self, monkeypatch):
        monkeypatch.setattr(rs, "FORCE_RUN", False)
        old = (rs.datetime.now(rs.timezone.utc) - rs.timedelta(days=14)).isoformat()
        assert rs._due({"last_run": old}) is True

    def test_corrupt_timestamp_runs_rather_than_stalling(self, monkeypatch):
        monkeypatch.setattr(rs, "FORCE_RUN", False)
        assert rs._due({"last_run": "not-a-date"}) is True


class TestSourceBundle:
    def test_includes_computed_eligibility_verbatim(self):
        trial = {
            "id": "NCT05066217", "title": "Clemizole", "status": "RECRUITING",
            "phase": "PHASE3", "reason": "test", "sites": ["Site A, Atlanta, US"],
            "abstract": "summary text", "criteria": "no planned epilepsy surgery",
            "elig_label": "ELIGIBLE NOW",
            "eligibility": {"verdict": "eligible_now", "detail": "Meets the age range"},
        }
        out = rs._source_bundle([], [trial], {})
        assert "ELIGIBLE NOW" in out
        assert "use verbatim" in out
        assert "no planned epilepsy surgery" in out

    def test_full_text_included_when_available(self):
        paper = {"id": "123", "source_id": "PMID:123", "title": "T", "journal": "J",
                 "date": "2026", "abstract": "abs"}
        assert "FULL TEXT" in rs._source_bundle([paper], [], {"123": "methods body"})
        assert "FULL TEXT" not in rs._source_bundle([paper], [], {})


class TestRenderer:
    @pytest.fixture
    def briefing(self):
        return {
            "headline": "LGS treatment update", "period": "August 20–September 4, 2026",
            "lede": "Two developments matter.",
            "sections": [{"heading": "Surgery", "paragraphs": ["Para one.", "Para two."],
                          "source_ids": ["PMID:123", "NCT05066217"]}],
            "bottom_line": ["This matters most."],
            "negatives": ["No new EMAS-specific drug data."],
            "numeric_claims": [],
        }

    def test_renders_prose_not_cards(self, briefing):
        html = rs.build_email_html(briefing, "September 4, 2026",
                                   {"screened": 10, "kept": 3}, [], [])
        assert "Para one." in html and "Para two." in html
        assert "What matters most" in html
        assert "No new EMAS-specific drug data." in html
        # the old card layout is gone
        assert "HIGH" not in html and "MEDIUM" not in html

    def test_source_ids_become_links(self, briefing):
        html = rs.build_email_html(briefing, "x", {}, [], [])
        assert "pubmed.ncbi.nlm.nih.gov/123/" in html
        assert "clinicaltrials.gov/study/NCT05066217" in html

    def test_unsupported_figures_are_surfaced(self, briefing):
        html = rs.build_email_html(
            briefing, "x", {}, [{"figure": "69.6%", "supported": False,
                                 "comment": "not in source"}], [])
        assert "could not be verified" in html
        assert "69.6%" in html and "not in source" in html

    def test_eligibility_table_rendered_from_computed_values(self, briefing):
        trials = [{"id": "NCT06924086", "title": "CADET DBS", "elig_label": "NOT YET ELIGIBLE",
                   "eligibility": {"verdict": "eligible_later", "detail": "min 5y"}}]
        html = rs.build_email_html(briefing, "x", {}, [], trials)
        assert "NOT YET ELIGIBLE" in html
        assert "computed from date of birth" in html

    def test_escapes_html_in_model_output(self, briefing):
        briefing["lede"] = "5 < 10 & <script>alert(1)</script>"
        html = rs.build_email_html(briefing, "x", {}, [], [])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestDeidentification:
    @pytest.mark.parametrize("leak", [
        "Bennett", "Ribeiro", "Pinto", "Atlanta", "CHOA", "Feb 7 2023", "98.5",
    ])
    def test_no_identifiers_in_outbound_context(self, leak):
        assert leak not in rs.PATIENT_CONTEXT

    def test_dob_is_local_only(self):
        """DOB drives eligibility math but must never appear in the API payload."""
        assert "2023-02-07" not in rs.PATIENT_CONTEXT
        assert rs.PATIENT_DOB == date(2023, 2, 7)


class TestSubjectLine:
    """The first live run shipped a subject with the period printed twice."""

    def test_no_duplicate_period_when_headline_contains_it(self):
        b = {"headline": "EMAS/LGS literature briefing: August 21 – September 6, 2026",
             "period": "August 21–September 6, 2026"}
        s = rs.build_subject(b, [])
        assert s.count("2026") == 1
        assert s == b["headline"]

    def test_period_appended_when_headline_lacks_it(self):
        s = rs.build_subject({"headline": "LGS treatment update",
                              "period": "August 21–September 6, 2026"}, [])
        assert s == "LGS treatment update — August 21–September 6, 2026"

    def test_unverified_count_flagged(self):
        s = rs.build_subject({"headline": "Update", "period": ""},
                             [{"figure": "x"}, {"figure": "y"}])
        assert s.endswith("[2 unverified figure(s)]")

    def test_missing_period_is_tolerated(self):
        assert rs.build_subject({"headline": "Update"}, []) == "Update"
