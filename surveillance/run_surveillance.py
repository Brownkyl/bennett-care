#!/usr/bin/env python3
"""
Bennett Literature Surveillance — biweekly synthesized briefing.

Queries PubMed, medRxiv, bioRxiv and ClinicalTrials.gov, then writes a narrative
briefing rather than a scored list of papers. Emailed via Gmail from GitHub Actions.

Pipeline:
  1. collect      PubMed (Entrez-date window) + preprints + trial status changes
  2. triage       per-item keep/drop against the open clinical decisions
  3. full text    open-access bodies for kept papers, where available
  4. synthesize   one pass over everything, producing themed narrative sections
  5. verify       every numeric figure checked back against its cited source
  6. send         prose email; unverified figures flagged at the top

Two things are deliberately NOT left to the model: trial age-eligibility (computed in
code from date of birth) and the numeric verification pass. Both exist because the
briefing format trades a larger hallucination surface for readability.

State lives at surveillance/state.json (committed to the repo).
"""

import json
import os
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import requests

# ---------------------------------------------------------------------------
# Configuration — all secrets come from environment (GitHub Secrets)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_FROM         = os.environ["GMAIL_FROM"]   # e.g. youremail@gmail.com
GMAIL_TO = [a.strip() for a in os.environ["GMAIL_TO"].split(",")]     # can be same address
NCBI_API_KEY       = os.environ.get("NCBI_API_KEY", "")  # optional but increases rate limit

STATE_FILE = Path(__file__).parent / "state.json"

# PubMed is searched on Entrez date (when the record became visible), NOT publication
# date. Measured against these query vectors, the median publication→index lag is 12
# days and 54% of records are indexed more than 7 days after their pubdate. A 7-day
# publication-date window never sees those: the record does not exist during the week
# its pubdate falls in, and by the time it is indexed the pubdate has aged out of the
# window. Entrez date is monotonic with visibility, which is what a rolling scan needs.
# The extra 3 days of overlap are absorbed by the seen-set.
SEARCH_LOOKBACK_DAYS = 16

# The digest is a synthesized briefing, not a list. A single week yields 6-10 items,
# which is too thin to find themes in — you get a narrative-shaped list instead of a
# narrative. Two weeks gives the synthesis pass enough material to say "three
# developments worth paying attention to."
#
# GitHub Actions cron has no biweekly expression, so the workflow still fires weekly and
# this gate decides whether a run is due. Keying off the last successful run (rather than
# ISO-week parity) means a failed or skipped week doesn't push the cadence out to a month.
MIN_DAYS_BETWEEN_RUNS = 12
FORCE_RUN = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")

# Anything over this cap is left unfetched AND unmarked, so it is picked up next run
# rather than silently dropped.
PUBMED_FETCH_CAP = 100

# Used only to compute trial age-eligibility deterministically, in code. This never goes
# to the API — the model is told "eligible / not until <date>", never asked to work it out.
PATIENT_DOB = date(2023, 2, 7)

CLAUDE_MODEL = "claude-opus-5"

EMERALD_NCT = "NCT07010471"   # relutrigine EMERALD trial

# ---------------------------------------------------------------------------
# PubMed search vectors
#
# Tiers reflect what is actually live in Bennett's care as of Sept 2026, not generic
# phenotype relevance. Tier A tracks the four open decisions; Tier B is standing
# context; the phenotype spine (EMAS/LGS) is always searched.
#
# Deliberately NOT searched: resective surgery, lesional/focal targets, RNS, and
# gene-specific precision therapy. The Sept 2 MRI and FDG-PET were both negative, so
# the focal pathway has no target; and there is no molecular diagnosis to anchor a
# gene vector. Re-add if either changes.
# ---------------------------------------------------------------------------
PUBMED_QUERIES = [
    # --- Phenotype spine -----------------------------------------------------
    {"id": "emas", "tier": "spine", "label": "EMAS core",
     "q": '"Doose syndrome" OR "myoclonic-atonic epilepsy" OR "epilepsy with myoclonic-atonic seizures"'},
    {"id": "lgs", "tier": "spine", "label": "LGS treatment",
     "q": '"Lennox-Gastaut syndrome" AND (treatment OR outcomes) AND (child* OR pediatric)'},

    # --- Tier A: the four live decisions -------------------------------------
    {"id": "perampanel", "tier": "A", "label": "Perampanel in LGS/EMAS",
     "q": 'perampanel[tiab] AND ("Lennox-Gastaut" OR "myoclonic-atonic" OR "Doose" OR '
          '"developmental and epileptic encephalopathy" OR "generalized epilepsy") '
          'AND (child*[tiab] OR pediatric[tiab] OR adolescen*[tiab])'},
    {"id": "lev_worsening", "tier": "A", "label": "LEV worsening / deprescribing",
     "q": '(levetiracetam[tiab] AND (worsen*[tiab] OR aggravat*[tiab] OR paradoxic*[tiab] OR '
          'exacerbat*[tiab] OR withdraw*[tiab] OR deprescrib*[tiab] OR discontinu*[tiab])) '
          'AND (epilep*[tiab] OR seizure*[tiab])'},
    {"id": "palliative_surgery", "tier": "A", "label": "Callosotomy / VNS in MRI-negative DEE",
     "q": '("corpus callosotomy" OR "vagus nerve stimulation" OR "palliative epilepsy surgery") '
          'AND ("drop attack*" OR "atonic" OR "Lennox-Gastaut" OR "generalized epilepsy" OR '
          '"MRI-negative" OR nonlesional OR "non-lesional") AND (child*[tiab] OR pediatric[tiab])'},
    {"id": "cfd", "tier": "A", "label": "Cerebral folate deficiency / folinic acid",
     "q": '("cerebral folate deficiency" OR "folinic acid" OR "5-methyltetrahydrofolate" OR '
          'FOLR1[tiab] OR "folate receptor autoantibod*" OR "folate receptor alpha") '
          'AND (epilep*[tiab] OR seizure*[tiab] OR encephalopath*[tiab])'},

    # --- Tier B: standing context --------------------------------------------
    {"id": "fenfluramine", "tier": "B", "label": "Fenfluramine in LGS/EMAS",
     "q": 'fenfluramine[tiab] AND (epilep*[tiab] OR seizure*[tiab])'},
    {"id": "dee_trials", "tier": "B", "label": "Relutrigine / DEE pipeline",
     "q": '(relutrigine[tiab] OR soticlestat[tiab] OR ganaxolone[tiab] OR carisbamate[tiab] OR '
          'clemizole[tiab] OR "LP352" OR bexicaserin[tiab]) AND (epilep*[tiab] OR seizure*[tiab])'},
    {"id": "undx_dee", "tier": "B", "label": "Undiagnosed DEE: reanalysis / mosaicism",
     "q": '("exome reanalysis" OR "genome reanalysis" OR "diagnostic yield" OR mosaic*[tiab] OR '
          '"non-coding variant*" OR "structural variant*") AND '
          '("developmental and epileptic encephalopathy" OR "epileptic encephalopathy" OR '
          '"unexplained epilepsy" OR "undiagnosed epilepsy")'},
]

PREPRINT_KEYWORDS = [
    "epilepsy", "seizure", "doose", "myoclonic", "lennox-gastaut", "dravet",
    "fenfluramine", "cannabidiol", "vns", "vagus nerve", "callosotomy",
    "folate deficiency", "encephalopathy", "relutrigine", "clobazam", "atonic",
    "glut1", "cerebral folate", "folinic", "folr1", "perampanel", "levetiracetam",
    "drop attack", "neuromodulation", "nonlesional", "exome reanalysis",
]

# Clinical state as of 2026-09-04 (post 8/18 neurology visit, post 9/2 MRI + FDG-PET).
#
# De-identified on purpose. CLAUDE.md hard constraint #1 says patient data never leaves
# the machine, and this string is the one thing in the repo that goes to an external API.
# Name, DOB, height, treating physician and institution have been removed — none of them
# change how a paper scores. Age, weight, phenotype and regimen stay because they do.
PATIENT_CONTEXT = """
Male, age 3, 17.7 kg.

DIAGNOSIS: EMAS (epilepsy with myoclonic-atonic seizures / Doose), generalized, EVOLVING
TO Lennox-Gastaut syndrome. The 8/18/2026 note dropped the earlier hedging ("consideration
for" -> "evolving to"); this is a documented progression in the treating physician's own
wording, consistent with the 7/17-18 EEG. It is not a new ICD-coded diagnosis.
Seizure types: myoclonic-atonic head drops, 3-15/day in clusters under 10 min; atypical
absence; nocturnal events. Comorbidities: migraine, cognitive impairment. Receiving PT, OT,
speech therapy; IEP through pre-K.

ETIOLOGY: likely genetic, UNKNOWN. Invitae 302-gene epilepsy panel and GeneDx trio WES both
non-diagnostic. Paternally inherited TSC2 c.3181C>G p.(Leu1061Val) VUS, not reclassified.
No whole-genome sequencing has been ordered. There is NO molecular diagnosis, so
gene-specific and precision-therapy findings are NOT actionable for this patient right now.

IMAGING (both 9/2/2026, both negative):
- MRI 3T with and without contrast: hippocampi symmetric and normal, no cortical dysplasia,
  no heterotopia, no migrational anomaly. No epileptogenic lesion identified.
- FDG-PET: normal uptake throughout cortex and deep gray nuclei. The 2024 finding of
  diffusely diminished bilateral temporal pole metabolism did NOT replicate.
- IMPORTANT CAVEAT: the PET is confounded. Tracer injected 10:28:02; three myoclonic-atonic
  seizures at 11:11-11:12 plus a 2.5-min atypical absence, i.e. 43 min post-injection inside
  a 68-min uptake window. Ictal/postictal activity during uptake can mask interictal
  hypometabolism. This is a weaker negative than a seizure-free normal PET.
NET: MRI-negative + PET-negative + generalized-onset electroclinical seizures. The focal
resective pathway has no target. The workup has come back pointing generalized and palliative.

CSF (LP 6/7/2026) — PARTIALLY RESULTED, confirmatory assay still outstanding:
- ARUP quantitative amino acid panel (finalized 6/11): one abnormal value — methionine BELOW
  the 2.0 umol/L detection floor against a 2.0-7.0 reference. CSF homocysteine <2.0.
  Everything else normal.
- NOT RESULTED / UNACCOUNTED FOR: 5-MTHF, neurotransmitter metabolites, pterins. Cerebral
  folate deficiency is therefore NEITHER CONFIRMED NOR EXCLUDED, ~12 weeks stale.
- Also missing: paired fasting CSF/serum glucose ratio and CSF lactate (GLUT1 arm cannot be
  confirmed to have been run), and paired plasma amino acids from the LP date.
CFD is the ONLY live precision hypothesis this patient has. Weight it accordingly.

CURRENT ASMs (as of 8/18/2026):
- Perampanel (Fycompa) 0.5 mg/mL susp: STARTED 2 mg nightly, up to 4 mg after one week,
  planned titration to 8 mg nightly. THIS IS THE ACTIVE TITRATION.
- Rufinamide (Banzel): WEANING OFF, -1 mL/week (1.5 mL BID -> 1 mL BID -> 0.5 mL BID -> off).
  Will enter the record as a failed trial despite never reaching therapeutic dose.
- Levetiracetam: unchanged, 34 mg/kg/day. Chart explicitly notes "either we will increase in
  the future or cancel altogether" — discontinuation is under active consideration.
- Cannabidiol (Epidiolex): unchanged; night dose may increase by 0.1 mL.
- Clobazam (Onfi): unchanged.
Rescue: Valtoco (diazepam nasal), Klonopin ODT (clonazepam).

FAILED: prednisolone (initial benefit only), topiramate (19-day seizure freedom then escape),
felbamate (appetite suppression, dehydration), valproate/Depakote (FORMAL ALLERGY, hives,
NEVER suggest again), ketogenic diet (MAD, 3:1 CKD, 1.5:1 CKD — all insufficient despite
strong ketosis; fully discontinued), rufinamide (weaning, subtherapeutic).

SAFETY FLAGS — surface these prominently whenever a paper touches them:
1. VALPROATE IS A DOCUMENTED ALLERGY. Never surface valproate as an option.
2. Sodium channel blockers (carbamazepine, oxcarbazepine, lamotrigine, phenytoin) are
   documented to WORSEN drop attacks in EMAS/LGS.
3. LEVETIRACETAM WORSENING: Pellacani et al., Brain Communications 2026, fcaf507 — ~5%
   paradoxical worsening in EMAS, comparable to carbamazepine; the same paper reports older
   ASMs outperforming newer ones in EMAS. The patient is currently ON levetiracetam and
   discontinuation is on the table. Any confirmatory or contradictory data is HIGH priority.
4. PK: cannabidiol inhibits CYP2C19, elevating N-desmethylclobazam (active metabolite).
   Watch for sedation data as perampanel titrates upward against clobazam + CBD.

OPEN DECISIONS (what this scan exists to inform):
A. Epilepsy surgery conference — case to be presented now that MRI and PET are in hand and
   both negative. Expect a pivot from resective/sEEG toward palliative options (VNS, corpus
   callosotomy). A "Robotics referral" note in the chart is the sEEG pathway; with
   non-localizing imaging its case is now weak.
B. Levetiracetam: increase vs. discontinue. Best argued during perampanel titration, before
   any perampanel benefit muddies attribution.
C. Perampanel titration to 8 mg nightly — efficacy and tolerability, especially sedation and
   behavioral effects under polytherapy in young children.
D. Chase the outstanding 5-MTHF / neurotransmitter panel. Folinic acid is low-risk against
   the current regimen; this is the only pending test that could convert "unknown etiology"
   into a treatable diagnosis.
Next neurology appointment: approximately 10/18/2026.
""".strip()

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            print(f"[WARN] Could not load state file: {e}. Starting fresh.")
    return {"seen_pubmed": [], "seen_preprints": [], "trial_snapshots": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"[STATE] Saved to {STATE_FILE}")


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------
def pubmed_search(query: str) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "datetype": "edat",
        "reldate": str(SEARCH_LOOKBACK_DAYS),
        "retmode": "json",
        "retmax": "100",
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    # NCBI allows 3 req/s without an API key, 10 with one. A 429 here silently zeroes out
    # a whole search vector, so back off and retry rather than returning [].
    for attempt in range(4):
        try:
            r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                             params=params, timeout=15)
            if r.status_code == 429:
                raise requests.HTTPError("429 rate limited")
            r.raise_for_status()
            return r.json().get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            if attempt == 3:
                print(f"[WARN] PubMed search failed for query '{query[:50]}...': {e}")
                return []
            time.sleep(1.5 * (attempt + 1))
    return []


def pubmed_fetch(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    params = {"db": "pubmed", "id": ",".join(pmids), "rettype": "xml", "retmode": "xml"}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    try:
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                         params=params, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[WARN] PubMed fetch failed: {e}")
        return []

    root = ET.fromstring(r.text)
    papers = []
    for article in root.findall(".//PubmedArticle"):
        pmid    = _text(article, ".//PMID")
        title   = _text(article, ".//ArticleTitle")
        journal = _text(article, ".//ISOAbbreviation") or _text(article, ".//Title")
        year    = _text(article, ".//PubDate/Year") or _text(article, ".//PubDate/MedlineDate")
        month   = _text(article, ".//PubDate/Month") or ""
        abstract_parts = [e.text or "" for e in article.findall(".//AbstractText")]
        abstract = " ".join(abstract_parts)
        papers.append({
            "id":       pmid,
            "source":   "PubMed",
            "title":    title or "(No title)",
            "abstract": abstract,
            "journal":  journal or "",
            "date":     f"{month} {year}".strip(),
            "url":      f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return papers


def _text(el, path: str) -> str:
    found = el.find(path)
    return (found.text or "").strip() if found is not None else ""


# ---------------------------------------------------------------------------
# Preprints (medRxiv / bioRxiv)
# ---------------------------------------------------------------------------
def preprint_search(server: str) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=SEARCH_LOOKBACK_DAYS)
    url = f"https://api.{server}.org/details/{server}/{start}/{today}/0/json"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        collection = r.json().get("collection", [])
    except Exception as e:
        print(f"[WARN] {server} fetch failed: {e}")
        return []

    label = "medRxiv" if server == "medrxiv" else "bioRxiv"
    results = []
    for p in collection:
        text = f"{p.get('title', '')} {p.get('abstract', '')}".lower()
        if any(kw in text for kw in PREPRINT_KEYWORDS):
            results.append({
                "id":       p.get("doi", ""),
                "source":   label,
                "title":    p.get("title", "(No title)"),
                "abstract": p.get("abstract", ""),
                "journal":  f"{label} preprint",
                "date":     p.get("date", ""),
                "url":      f"https://doi.org/{p.get('doi', '')}",
            })
    return results


# ---------------------------------------------------------------------------
# ClinicalTrials.gov
# ---------------------------------------------------------------------------
AGE_UNITS = {"year": 365.25, "month": 30.44, "week": 7.0, "day": 1.0}


def _age_to_days(text: str) -> float | None:
    """'2 Years' -> 730.5. Returns None for 'N/A' or anything unparseable."""
    if not text:
        return None
    parts = text.strip().lower().split()
    if len(parts) < 2:
        return None
    try:
        qty = float(parts[0])
    except ValueError:
        return None
    unit = parts[1].rstrip("s")
    factor = AGE_UNITS.get(unit)
    return qty * factor if factor else None


def eligibility_for_patient(min_age: str, max_age: str, today: date) -> dict:
    """Age-eligibility, computed in code so the model cannot get it wrong.

    Returns {"verdict", "detail"} where verdict is one of:
      eligible_now | eligible_later | aged_out | unknown
    """
    age_days = (today - PATIENT_DOB).days
    lo = _age_to_days(min_age)
    hi = _age_to_days(max_age)

    if lo is None and hi is None:
        return {"verdict": "unknown", "detail": "No age range published"}
    if hi is not None and age_days > hi:
        return {"verdict": "aged_out", "detail": f"Upper age limit {max_age}"}
    if lo is not None and age_days < lo:
        eligible_on = PATIENT_DOB + timedelta(days=lo)
        months = round((eligible_on - today).days / 30.44)
        return {
            "verdict": "eligible_later",
            "detail": f"Minimum age {min_age} — not eligible until about "
                      f"{eligible_on.strftime('%B %Y')} (~{months} months)",
        }
    span = f"{min_age or 'no minimum'} to {max_age or 'no maximum'}"
    return {"verdict": "eligible_now", "detail": f"Meets the age range ({span})"}


def _trial_sites(proto: dict, limit: int = 8) -> list[str]:
    seen, out = set(), []
    for loc in proto.get("contactsLocationsModule", {}).get("locations", []):
        label = ", ".join(x for x in (loc.get("facility"), loc.get("city"),
                                      loc.get("country")) if x)
        if label and label not in seen:
            seen.add(label)
            out.append(label)
        if len(out) >= limit:
            break
    return out


def _fetch_trial(nct: str) -> dict | None:
    try:
        r = requests.get(f"https://clinicaltrials.gov/api/v2/studies/{nct}?format=json",
                         timeout=15)
        r.raise_for_status()
        proto = r.json().get("protocolSection", {})
        elig  = proto.get("eligibilityModule", {})
        min_age, max_age = elig.get("minimumAge", ""), elig.get("maximumAge", "")
        return {
            "status":   proto.get("statusModule", {}).get("overallStatus", "UNKNOWN"),
            "title":    proto.get("identificationModule", {}).get("briefTitle", nct),
            "phase":    ", ".join(proto.get("designModule", {}).get("phases", [])),
            "min_age":  min_age,
            "max_age":  max_age,
            "eligibility": eligibility_for_patient(min_age, max_age,
                                                   datetime.now(timezone.utc).date()),
            "criteria": (elig.get("eligibilityCriteria", "") or "")[:2500],
            "sites":    _trial_sites(proto),
            "summary":  (proto.get("descriptionModule", {})
                              .get("briefSummary", "") or "")[:1500],
        }
    except Exception as e:
        print(f"[WARN] Trial fetch failed for {nct}: {e}")
        return None


# A trial that stops recruiting simply drops out of a RECRUITING-filtered search. That is
# exactly the transition worth knowing about, so every trial ever seen is re-checked by ID
# on each run rather than being rediscovered by the search.
CLOSING_STATUSES = {
    "ACTIVE_NOT_RECRUITING", "COMPLETED", "TERMINATED", "SUSPENDED", "WITHDRAWN",
    "ENROLLING_BY_INVITATION",
}


ELIGIBILITY_LABEL = {
    "eligible_now":   "ELIGIBLE NOW",
    "eligible_later": "NOT YET ELIGIBLE",
    "aged_out":       "AGE-INELIGIBLE",
    "unknown":        "AGE RANGE UNPUBLISHED",
}


def _trial_item(nct: str, rec: dict, headline: str, reason: str, score: str) -> dict:
    elig = rec.get("eligibility", {"verdict": "unknown", "detail": ""})
    return {
        "id":          nct,
        "source":      "ClinicalTrials.gov",
        "title":       f"{rec['title']} ({nct})",
        "abstract":    rec.get("summary", ""),
        "journal":     headline,
        "date":        datetime.now(timezone.utc).date().isoformat(),
        "url":         f"https://clinicaltrials.gov/study/{nct}",
        "score":       score,
        "reason":      reason,
        "status":      rec.get("status", ""),
        "phase":       rec.get("phase", ""),
        "eligibility": elig,
        "elig_label":  ELIGIBILITY_LABEL.get(elig["verdict"], "UNKNOWN"),
        "criteria":    rec.get("criteria", ""),
        "sites":       rec.get("sites", []),
    }


def scan_trials(snapshots: dict) -> tuple[list[dict], dict]:
    updates: list[dict] = []
    updated = dict(snapshots)

    # 1. Re-check every trial already under watch (EMERALD included) for status changes.
    watched = set(updated) | {EMERALD_NCT}
    for nct in sorted(watched):
        rec = _fetch_trial(nct)
        time.sleep(0.2)
        if rec is None:
            continue
        prior = updated.get(nct)
        snapshot = {"status": rec["status"], "title": rec["title"]}
        if prior and prior.get("status") == rec["status"]:
            updated[nct] = snapshot
            continue

        prior_status = prior["status"] if prior else "first observation"
        is_emerald = nct == EMERALD_NCT
        closing = rec["status"] in CLOSING_STATUSES
        if prior is None:
            reason = "Now under watch"
        elif closing:
            reason = (f"Enrollment door closing: {prior_status} -> {rec['status']}. "
                      f"If this was a candidate, it may no longer be open.")
        else:
            reason = f"Status changed: {prior_status} -> {rec['status']}"

        updates.append(_trial_item(
            nct, rec,
            headline="ClinicalTrials.gov — EMERALD watch" if is_emerald
                     else "ClinicalTrials.gov — status change",
            reason=reason,
            score="HIGH" if (is_emerald or closing) else "MEDIUM",
        ))
        updated[nct] = snapshot

    # 2. Discover trials not yet under watch.
    try:
        r = requests.get(
            "https://clinicaltrials.gov/api/v2/studies",
            params={
                # Kept narrow deliberately. Broadening this to "developmental and
                # epileptic encephalopathy" pulls in ~14 gene-specific natural-history
                # and ASO trials per run (SLC6A1, SCN2A, ...) which the rubric down-weights
                # anyway — there is no molecular diagnosis to match them against. The DEE
                # pipeline is already covered by the dee_trials PubMed vector.
                "query.cond": "myoclonic atonic epilepsy OR Doose syndrome OR Lennox-Gastaut",
                "filter.overallStatus": "RECRUITING",
                "pageSize": "30",
                "format": "json",
            },
            timeout=15)
        r.raise_for_status()
        for study in r.json().get("studies", []):
            nct = (study.get("protocolSection", {})
                        .get("identificationModule", {}).get("nctId"))
            if not nct or nct in updated:
                continue
            rec = _fetch_trial(nct)
            time.sleep(0.2)
            if rec is None:
                continue
            updates.append(_trial_item(
                nct, rec,
                headline="ClinicalTrials.gov — new trial",
                reason="Newly recruiting and matching the phenotype",
                # An age-ineligible trial is context, not an opportunity.
                score="HIGH" if rec["eligibility"]["verdict"] == "eligible_now" else "MEDIUM",
            ))
            updated[nct] = {"status": rec["status"], "title": rec["title"]}
    except Exception as e:
        print(f"[WARN] ClinicalTrials scan failed: {e}")

    return updates, updated


# ---------------------------------------------------------------------------
# PubMed Central full text (open-access subset only)
# ---------------------------------------------------------------------------
def fetch_pmc_fulltext(pmids: list[str], limit: int = 12) -> dict[str, str]:
    """Methods/results/limitations text for the OA subset.

    Appraisal detail — "46 of the 48 studies were retrospective", "only 57 of 152 were
    evaluable at 12 months" — usually lives in the body, not the abstract. Paywalled
    papers simply return nothing and the briefing falls back to the abstract.
    """
    if not pmids:
        return {}
    out: dict[str, str] = {}
    try:
        params = {"dbfrom": "pubmed", "db": "pmc", "id": ",".join(pmids[:limit]),
                  "retmode": "json"}
        if NCBI_API_KEY:
            params["api_key"] = NCBI_API_KEY
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
                         params=params, timeout=20)
        r.raise_for_status()
        pairs: list[tuple[str, str]] = []
        for linkset in r.json().get("linksets", []):
            src = str(linkset.get("ids", [""])[0])
            for db in linkset.get("linksetdbs", []):
                if db.get("linkname") == "pubmed_pmc":
                    for pmcid in db.get("links", []):
                        pairs.append((src, str(pmcid)))
                        break
    except Exception as e:
        print(f"[WARN] PMC link lookup failed: {e}")
        return {}

    for pmid, pmcid in pairs:
        try:
            time.sleep(0.35)
            params = {"db": "pmc", "id": pmcid, "retmode": "xml"}
            if NCBI_API_KEY:
                params["api_key"] = NCBI_API_KEY
            r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                             params=params, timeout=25)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            body = root.find(".//body")
            if body is None:
                continue
            chunks: list[str] = []
            for sec in body.findall(".//sec"):
                title = (sec.findtext("title") or "").lower()
                if any(k in title for k in ("method", "result", "limitation",
                                            "discussion", "analysis")):
                    text = " ".join("".join(pp.itertext()) for pp in sec.findall(".//p"))
                    if text.strip():
                        chunks.append(f"{title.upper()}: {text.strip()}")
            if chunks:
                out[pmid] = " \n".join(chunks)[:9000]
        except Exception as e:
            print(f"[WARN] PMC fetch failed for PMC{pmcid}: {e}")
    print(f"  full text retrieved for {len(out)}/{len(pmids[:limit])} (OA subset)")
    return out


# ---------------------------------------------------------------------------
# Stage 1 — triage
# ---------------------------------------------------------------------------
THEMES = ["surgery_neuromodulation", "medication", "diagnostics_etiology",
          "trials_regulatory", "other"]

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "keep":  {"type": "boolean"},
                    "theme": {"type": "string", "enum": THEMES},
                    "note":  {"type": "string"},
                },
                "required": ["index", "keep", "theme", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

TRIAGE_INSTRUCTIONS = """
Decide which items belong in a two-week briefing for the parent of the patient above, who
brings it to the treating pediatric neurologist. This is a filter, not a summary — a later
pass writes the briefing from whatever you keep.

KEEP an item if it bears on any of the open decisions (A-D), reports EMAS/Doose/LGS-specific
outcomes, touches a drug in or adjacent to the current regimen, concerns palliative
neuromodulation or callosotomy, concerns cerebral folate deficiency or its workup, or
touches one of the four safety flags. When genuinely unsure, KEEP — the synthesis pass can
still decide an item does not warrant space.

DROP: adult-only cohorts with no pediatric read-across, unrelated mechanisms, gene-specific
or precision-therapy work tied to a molecular diagnosis this patient does not have,
resective-surgery and focal-localization work (both the MRI and the FDG-PET were negative),
and single case reports without a management implication.

`note` is one clause on why it was kept or dropped, for the audit trail.
""".strip()


def triage_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (kept, failed). Failed items are never marked seen."""
    if not items:
        return [], []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    kept: list[dict] = []
    failed: list[dict] = []

    for i in range(0, len(items), 8):
        batch = items[i : i + 8]
        listing = "\n\n---\n\n".join(
            f"[{idx}] TITLE: {it['title']}\nSOURCE: {it.get('journal','')}\n"
            f"ABSTRACT: {it.get('abstract') or '(none)'}"
            for idx, it in enumerate(batch)
        )
        result = None
        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=4000,
                    messages=[{"role": "user", "content":
                               f"PATIENT:\n{PATIENT_CONTEXT}\n\n{TRIAGE_INSTRUCTIONS}\n\n"
                               f"ITEMS:\n{listing}"}],
                    output_config={"format": {"type": "json_schema",
                                              "schema": TRIAGE_SCHEMA}},
                )
                text = next(b.text for b in resp.content if b.type == "text")
                result = json.loads(text)["items"]
                break
            except Exception as e:
                print(f"[WARN] Triage batch {i // 8} attempt {attempt + 1}/3: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)

        if result is None:
            failed.extend(batch)
            continue
        by_index = {r["index"]: r for r in result}
        for idx, item in enumerate(batch):
            verdict = by_index.get(idx)
            if verdict is None:
                failed.append(item)
            elif verdict["keep"]:
                kept.append({**item, "theme": verdict["theme"], "note": verdict["note"]})
        time.sleep(0.3)

    return kept, failed


# ---------------------------------------------------------------------------
# Stage 2 — synthesis
# ---------------------------------------------------------------------------
BRIEFING_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "lede":     {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading":    {"type": "string"},
                    "paragraphs": {"type": "array", "items": {"type": "string"}},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "paragraphs", "source_ids"],
                "additionalProperties": False,
            },
        },
        "bottom_line": {"type": "array", "items": {"type": "string"}},
        "negatives":   {"type": "array", "items": {"type": "string"}},
        "numeric_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "figure":    {"type": "string"},
                    "claim":     {"type": "string"},
                    "source_id": {"type": "string"},
                },
                "required": ["figure", "claim", "source_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "lede", "sections", "bottom_line", "negatives",
                 "numeric_claims"],
    "additionalProperties": False,
}

SYNTHESIS_INSTRUCTIONS = """
Write a two-week literature briefing for the parent of the patient above. They read it, then
raise what matters with the treating pediatric neurologist. Write for an intelligent
non-clinician: plain, direct prose in full paragraphs. No bullet lists inside sections, no
per-paper cards, no priority badges.

SHAPE
- `headline`: e.g. "LGS treatment update: <period>".
- `lede`: one paragraph naming how many developments actually matter and what they are.
  If it was a quiet period, say so plainly — a thin period is a real finding, not a failure.
- `sections`: one per theme that earned space, most consequential first. Lead with the
  finding and what it means, then a separate paragraph on how much weight it can carry.
  Group related items; do not walk the list in order.
- `bottom_line`: 2-4 sentences on what matters most and why.
- `negatives`: what was checked and NOT found, one line each.

EVIDENCE RULES — these are the point of the exercise
1. Every number you write must appear in the source text provided for that item. If a
   figure is not in the text, write "not reported in the abstract" rather than supplying it.
   Do not compute, round, or recall figures from memory.
2. State the design and its limits in the same breath as the result: retrospective vs.
   randomized, sample size, how many were evaluable at the endpoint, single-centre vs.
   multi-centre, and any certainty rating the authors gave. If a result looks strong but the
   design is weak, say so in the same paragraph — never in a later caveat the reader might
   miss.
3. Watch for attrition and selection effects and name them concretely when the numbers
   support it (e.g. an open-label cohort where only a fraction reached the endpoint).
4. Do not confuse designation, orphan status, or trial registration with approval.
5. For every clinical trial, use the COMPUTED age-eligibility verdict exactly as given. It
   was calculated in code from the patient's date of birth. Never infer, restate, or
   recompute an age range yourself.
6. Report what studies found. Do not recommend a treatment, do not tell the reader what to
   do, and do not predict what the neurologist should decide. Interpreting the strength of
   evidence is expected; prescribing is not.
7. `numeric_claims` must list every figure you used, with the source id you took it from.
   This is checked afterwards, so be complete and honest.

SOURCE IDS: cite by the bracketed id (PMID:..., NCT..., DOI:...) in `source_ids`. Do not
invent ids.
""".strip()


def _source_bundle(items: list[dict], trials: list[dict], fulltext: dict[str, str]) -> str:
    blocks: list[str] = []
    for it in items:
        sid = it["source_id"]
        body = [f"[{sid}] {it['title']}",
                f"SOURCE: {it.get('journal','')} | {it.get('date','')}",
                f"ABSTRACT: {it.get('abstract') or '(none available)'}"]
        ft = fulltext.get(it["id"])
        if ft:
            body.append(f"FULL TEXT (open access): {ft}")
        blocks.append("\n".join(body))
    for t in trials:
        elig = t.get("eligibility", {})
        body = [f"[{t['id']}] {t['title']}",
                f"STATUS: {t.get('status','')} | PHASE: {t.get('phase') or 'n/a'}",
                f"AGE ELIGIBILITY (computed in code, use verbatim): "
                f"{t.get('elig_label','')} — {elig.get('detail','')}",
                f"WHY LISTED: {t.get('reason','')}"]
        if t.get("sites"):
            body.append(f"SITES: {'; '.join(t['sites'])}")
        if t.get("abstract"):
            body.append(f"SUMMARY: {t['abstract']}")
        if t.get("criteria"):
            body.append(f"ELIGIBILITY CRITERIA: {t['criteria']}")
        blocks.append("\n".join(body))
    return "\n\n=====\n\n".join(blocks)


def synthesize_briefing(items: list[dict], trials: list[dict], fulltext: dict[str, str],
                        period_start: date, period_end: date,
                        sources_checked: list[str]) -> dict | None:
    if not items and not trials:
        return None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    period = (f"{period_start.strftime('%B %-d')}–{period_end.strftime('%-d, %Y')}"
              if period_start.month == period_end.month
              else f"{period_start.strftime('%B %-d')}–{period_end.strftime('%B %-d, %Y')}")
    prompt = (
        f"PATIENT:\n{PATIENT_CONTEXT}\n\n"
        f"{SYNTHESIS_INSTRUCTIONS}\n\n"
        f"PERIOD: {period}\n"
        f"SOURCES SEARCHED THIS PERIOD: {', '.join(sources_checked)}.\n"
        f"Nothing else was searched. Do not assert an absence for a source not on that "
        f"list — in particular, no FDA approval or regulatory database was queried, so "
        f"make no claim either way about approvals.\n\n"
        f"ITEMS ({len(items)} papers, {len(trials)} trial records):\n\n"
        f"{_source_bundle(items, trials, fulltext)}"
    )
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": BRIEFING_SCHEMA}},
            )
            text = next(b.text for b in resp.content if b.type == "text")
            briefing = json.loads(text)
            briefing["period"] = period
            return briefing
        except Exception as e:
            print(f"[WARN] Synthesis attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Stage 3 — numeric verification
#
# The briefing format buys narrative at the cost of a much larger surface for
# plausible-but-wrong figures, and these numbers land in a conversation about a
# three-year-old's surgery. Every figure is checked back against the source text it
# claims to come from, and anything unsupported is flagged in the email rather than
# quietly shipped.
# ---------------------------------------------------------------------------
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "figure":    {"type": "string"},
                    "supported": {"type": "boolean"},
                    "comment":   {"type": "string"},
                },
                "required": ["figure", "supported", "comment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["checks"],
    "additionalProperties": False,
}


def verify_numeric_claims(briefing: dict, bundle: str) -> list[dict]:
    claims = briefing.get("numeric_claims", [])
    if not claims:
        return []
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    listing = "\n".join(
        f"- figure: {c['figure']} | claim: {c['claim']} | cited source: {c['source_id']}"
        for c in claims
    )
    prompt = (
        "Below is source material, then a list of numeric figures a briefing drew from it.\n"
        "For each figure, decide whether that exact figure is actually present in the source "
        "material for the cited id, and whether it is being used to say what the source says. "
        "Mark supported=false for anything absent, altered, or attributed to the wrong "
        "source. Be strict: a number that is close but not identical is NOT supported.\n\n"
        f"SOURCE MATERIAL:\n{bundle}\n\nFIGURES TO CHECK:\n{listing}"
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": VERIFY_SCHEMA}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return [c for c in json.loads(text)["checks"] if not c["supported"]]
    except Exception as e:
        print(f"[WARN] Claim verification failed: {e}")
        return [{"figure": "(verification pass failed)", "supported": False,
                 "comment": f"Could not verify figures this run: {e}"}]


# ---------------------------------------------------------------------------
# Email formatting — narrative briefing
# ---------------------------------------------------------------------------
def _esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


SOURCE_URL = {
    "PMID": "https://pubmed.ncbi.nlm.nih.gov/{}/",
    "NCT":  "https://clinicaltrials.gov/study/{}",
    "DOI":  "https://doi.org/{}",
}


def _source_link(sid: str) -> str:
    if sid.startswith("NCT"):
        url = SOURCE_URL["NCT"].format(sid)
    elif ":" in sid:
        kind, value = sid.split(":", 1)
        url = SOURCE_URL.get(kind, "{}").format(value)
    else:
        url = ""
    label = _esc(sid)
    return (f'<a href="{url}" style="color:#1d4ed8;text-decoration:none">{label}</a>'
            if url else label)


def build_email_html(briefing: dict, scan_date: str, counts: dict,
                     unsupported: list[dict], trials: list[dict]) -> str:
    sections_html = ""
    for sec in briefing.get("sections", []):
        paras = "".join(
            f'<p style="margin:0 0 12px;font-size:15px;line-height:1.65;color:#1f2937">'
            f'{_esc(p)}</p>'
            for p in sec.get("paragraphs", [])
        )
        srcs = sec.get("source_ids", [])
        src_line = (
            f'<p style="margin:0 0 4px;font-size:11px;color:#9ca3af;font-family:monospace">'
            f'{" · ".join(_source_link(s) for s in srcs)}</p>'
        ) if srcs else ""
        sections_html += (
            f'<h2 style="margin:28px 0 10px;font-size:16px;font-weight:600;color:#111827">'
            f'{_esc(sec.get("heading",""))}</h2>{paras}{src_line}'
        )

    bottom = "".join(
        f'<p style="margin:0 0 10px;font-size:15px;line-height:1.65;color:#1f2937">{_esc(b)}</p>'
        for b in briefing.get("bottom_line", [])
    )
    bottom_html = (
        f'<div style="margin-top:28px;padding:16px 18px;background:#f9fafb;'
        f'border-left:3px solid #111827;border-radius:4px">'
        f'<h2 style="margin:0 0 10px;font-size:14px;font-weight:600;color:#111827;'
        f'text-transform:uppercase;letter-spacing:0.06em">What matters most</h2>{bottom}</div>'
    ) if bottom else ""

    negatives = briefing.get("negatives", [])
    neg_html = (
        '<h2 style="margin:28px 0 8px;font-size:13px;font-weight:600;color:#6b7280;'
        'text-transform:uppercase;letter-spacing:0.06em">Checked and not found</h2>'
        + "".join(
            f'<p style="margin:0 0 6px;font-size:13px;color:#6b7280;line-height:1.55">'
            f'— {_esc(n)}</p>' for n in negatives)
    ) if negatives else ""

    # Age-eligibility is computed in code, so it is rendered as its own table rather than
    # left in prose where a model could soften or garble it.
    elig_rows = ""
    for t in sorted(trials, key=lambda x: x.get("eligibility", {}).get("verdict", "z")):
        v = t.get("eligibility", {}).get("verdict", "unknown")
        color = {"eligible_now": "#059669", "eligible_later": "#d97706",
                 "aged_out": "#9ca3af", "unknown": "#6b7280"}.get(v, "#6b7280")
        elig_rows += (
            f'<tr>'
            f'<td style="padding:7px 10px 7px 0;font-size:12px;vertical-align:top;'
            f'border-bottom:1px solid #f3f4f6">{_source_link(t["id"])}</td>'
            f'<td style="padding:7px 10px 7px 0;font-size:12px;vertical-align:top;'
            f'border-bottom:1px solid #f3f4f6;color:#374151">{_esc(t["title"][:70])}</td>'
            f'<td style="padding:7px 0;font-size:11px;vertical-align:top;'
            f'border-bottom:1px solid #f3f4f6;color:{color};font-weight:600;'
            f'font-family:monospace;white-space:nowrap">{_esc(t.get("elig_label",""))}</td>'
            f'</tr>'
        )
    elig_html = (
        '<h2 style="margin:28px 0 8px;font-size:13px;font-weight:600;color:#6b7280;'
        'text-transform:uppercase;letter-spacing:0.06em">Trial age-eligibility '
        '<span style="font-weight:400;text-transform:none;letter-spacing:0">'
        '(computed from date of birth, not model-generated)</span></h2>'
        f'<table style="width:100%;border-collapse:collapse">{elig_rows}</table>'
    ) if elig_rows else ""

    warn_html = (
        '<div style="margin:20px 0;padding:14px 16px;background:#fef2f2;'
        'border:1px solid #dc2626;border-radius:6px">'
        '<p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#991b1b">'
        f'{len(unsupported)} figure(s) could not be verified against the source text — '
        'treat these as unconfirmed:</p>'
        + "".join(
            f'<p style="margin:0 0 4px;font-size:12px;color:#7f1d1d;line-height:1.5">'
            f'<strong>{_esc(u.get("figure",""))}</strong> — {_esc(u.get("comment",""))}</p>'
            for u in unsupported)
        + '</div>'
    ) if unsupported else ""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:680px;margin:0 auto;padding:24px 16px">

  <div style="background:#111827;border-radius:8px;padding:20px 24px;margin-bottom:20px">
    <h1 style="margin:0 0 4px;font-size:18px;font-weight:600;color:#f9fafb">
      {_esc(briefing.get("headline", "LGS treatment update"))}
    </h1>
    <p style="margin:0;font-size:13px;color:#9ca3af;font-family:monospace">
      {_esc(briefing.get("period", scan_date))}
    </p>
  </div>

  <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:22px 26px">
    {warn_html}
    <p style="margin:0 0 4px;font-size:15px;line-height:1.65;color:#111827">
      {_esc(briefing.get("lede", ""))}
    </p>
    {sections_html}
    {bottom_html}
    {elig_html}
    {neg_html}
  </div>

  <div style="padding:16px 0;text-align:center">
    <p style="font-size:11px;color:#9ca3af;margin:0;font-family:monospace">
      bennett-care · {counts.get('screened', 0)} screened → {counts.get('kept', 0)} in briefing
      · {counts.get('trials', 0)} trial updates · {counts.get('fulltext', 0)} full texts
      · figures checked against source
    </p>
  </div>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Gmail send
# ---------------------------------------------------------------------------
def send_email(subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_FROM
    msg["To"] = ", ".join(GMAIL_TO)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_FROM, GMAIL_TO, msg.as_string())
    print(f"[EMAIL] Sent to {GMAIL_TO}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _due(state: dict) -> bool:
    if FORCE_RUN:
        print("[GATE] FORCE_RUN set — running regardless of cadence")
        return True
    last = state.get("last_run")
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
    except ValueError:
        return True
    if elapsed < MIN_DAYS_BETWEEN_RUNS:
        print(f"[GATE] Last run was {elapsed}d ago; briefing runs every "
              f"~{MIN_DAYS_BETWEEN_RUNS}d. Nothing to do.")
        return False
    return True


def main() -> None:
    print(f"[START] Bennett literature surveillance — {datetime.now(timezone.utc).isoformat()}")
    state = load_state()
    if not _due(state):
        return

    seen_pubmed     = set(state.get("seen_pubmed", []))
    seen_preprints  = set(state.get("seen_preprints", []))
    trial_snapshots = state.get("trial_snapshots", {})
    period_end   = datetime.now(timezone.utc).date()
    period_start = period_end - timedelta(days=SEARCH_LOOKBACK_DAYS)

    # 1. PubMed
    print(f"[STEP 1] Querying PubMed across {len(PUBMED_QUERIES)} vectors "
          f"(edat, {SEARCH_LOOKBACK_DAYS}d)...")
    all_pmids: set[str] = set()
    for q in PUBMED_QUERIES:
        ids = pubmed_search(q["q"])
        all_pmids.update(ids)
        print(f"  [{q['tier']}] {q['label']}: {len(ids)} results")
        time.sleep(0.15 if NCBI_API_KEY else 0.45)
    new_pmids = [p for p in all_pmids if p not in seen_pubmed]
    fetch_now = new_pmids[:PUBMED_FETCH_CAP]
    if len(new_pmids) > len(fetch_now):
        print(f"  {len(new_pmids) - len(fetch_now)} PMIDs over the fetch cap — deferred")
    pubmed_papers = pubmed_fetch(fetch_now)
    print(f"  {len(pubmed_papers)} fetched (of {len(all_pmids)} matching)")

    # 2. Preprints
    print("[STEP 2] Scanning medRxiv and bioRxiv...")
    medrxiv = preprint_search("medrxiv")
    time.sleep(0.5)
    biorxiv = preprint_search("biorxiv")
    new_preprints = [p for p in medrxiv + biorxiv if p["id"] not in seen_preprints]
    print(f"  {len(new_preprints)} new preprints")

    # 3. Trials
    print("[STEP 3] Checking ClinicalTrials.gov...")
    trial_updates, trial_snapshots = scan_trials(trial_snapshots)
    for t in trial_updates:
        print(f"  {t['id']} {t.get('elig_label',''):22s} {t['title'][:52]}")

    # 4. Triage
    candidates = pubmed_papers + new_preprints
    for c in candidates:
        c["source_id"] = (f"PMID:{c['id']}" if c["source"] == "PubMed"
                          else f"DOI:{c['id']}")
    print(f"[STEP 4] Triaging {len(candidates)} items...")
    kept, failed = triage_items(candidates)
    print(f"  {len(kept)} kept, {len(candidates) - len(kept) - len(failed)} dropped, "
          f"{len(failed)} deferred")

    # 5. Full text for the kept set
    print("[STEP 5] Fetching open-access full text...")
    fulltext = fetch_pmc_fulltext([k["id"] for k in kept if k["source"] == "PubMed"])

    # 6. Synthesis
    sources = ["PubMed", "medRxiv", "bioRxiv", "ClinicalTrials.gov"]
    print(f"[STEP 6] Synthesizing briefing from {len(kept)} papers + "
          f"{len(trial_updates)} trials...")
    briefing = synthesize_briefing(kept, trial_updates, fulltext,
                                   period_start, period_end, sources)
    if briefing is None:
        print("[ABORT] Synthesis failed — state not advanced, will retry next run.")
        sys.exit(1)

    # 7. Verify every figure against its cited source
    print(f"[STEP 7] Verifying {len(briefing.get('numeric_claims', []))} numeric claims...")
    bundle = _source_bundle(kept, trial_updates, fulltext)
    unsupported = verify_numeric_claims(briefing, bundle)
    if unsupported:
        print(f"  {len(unsupported)} UNSUPPORTED figure(s) — flagged in the email")
        for u in unsupported:
            print(f"    {u.get('figure','')}: {u.get('comment','')}")
    else:
        print("  all figures traced to source")

    # 8. State. Only items that completed triage are marked seen.
    failed_ids = {f["id"] for f in failed}
    triaged_ok = {c["id"] for c in candidates} - failed_ids
    seen_pubmed.update({p["id"] for p in pubmed_papers} & triaged_ok)
    seen_preprints.update({p["id"] for p in new_preprints} & triaged_ok)

    state["seen_pubmed"]     = sorted(seen_pubmed)
    state["seen_preprints"]  = sorted(seen_preprints)
    state["trial_snapshots"] = trial_snapshots
    state["last_run"]        = datetime.now(timezone.utc).isoformat()
    state["last_run_summary"] = {
        "period":      briefing.get("period"),
        "screened":    len(candidates),
        "kept":        len(kept),
        "deferred":    len(failed),
        "trials":      len(trial_updates),
        "fulltext":    len(fulltext),
        "unsupported": len(unsupported),
    }
    save_state(state)

    # 9. Send
    counts = {"screened": len(candidates), "kept": len(kept),
              "trials": len(trial_updates), "fulltext": len(fulltext)}
    html = build_email_html(briefing, period_end.strftime("%B %d, %Y"), counts,
                            unsupported, trial_updates)
    subject = f"{briefing.get('headline', 'Bennett Lit Briefing')} — {briefing.get('period','')}"
    if unsupported:
        subject += f" [{len(unsupported)} unverified figure(s)]"
    print(f"[STEP 8] Sending: {subject}")
    send_email(subject, html)
    print("[DONE]")


if __name__ == "__main__":
    main()
