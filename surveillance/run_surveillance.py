#!/usr/bin/env python3
"""
Bennett Literature Surveillance — Automated weekly scan
Queries PubMed, medRxiv, bioRxiv, and ClinicalTrials.gov.
Scores results against Bennett's phenotype using Claude.
Sends an HTML digest to Gmail every Friday via GitHub Actions.

Place this file at: bennett-care/surveillance/run_surveillance.py
State file lives at: bennett-care/surveillance/state.json (committed to repo)
"""

import json
import os
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration — all secrets come from environment (GitHub Secrets)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_FROM         = os.environ["GMAIL_FROM"]   # e.g. youremail@gmail.com
GMAIL_TO = [a.strip() for a in os.environ["GMAIL_TO"].split(",")]     # can be same address
NCBI_API_KEY       = os.environ.get("NCBI_API_KEY", "")  # optional but increases rate limit

STATE_FILE    = Path(__file__).parent / "state.json"
DAYS_LOOKBACK = 7        # weekly run looks back 7 days
EMERALD_NCT   = "NCT07010471"   # relutrigine EMERALD trial (corrected)

# ---------------------------------------------------------------------------
# PubMed search vectors — ported exactly from bennett_literature_surveillance.jsx
# ---------------------------------------------------------------------------
PUBMED_QUERIES = [
    {"id": "emas",        "label": "EMAS core",
     "q": '"Doose syndrome" OR "myoclonic-atonic epilepsy" OR "epilepsy with myoclonic-atonic seizures"'},
    {"id": "rufinamide",  "label": "Rufinamide",
     "q": "rufinamide[tiab] AND (pediatric[tiab] OR children[tiab] OR child[tiab]) AND epilepsy[tiab]"},
    {"id": "fenfluramine","label": "Fenfluramine",
     "q": "fenfluramine[tiab] AND epilepsy[tiab]"},
    {"id": "vns",         "label": "VNS pediatric",
     "q": '"vagus nerve stimulation" AND ("Lennox-Gastaut" OR "generalized epilepsy") AND (child[tiab] OR pediatric[tiab])'},
    {"id": "cfd",         "label": "Cerebral folate deficiency",
     "q": '"cerebral folate deficiency" AND epilepsy'},
    {"id": "csf",         "label": "CSF neurotransmitters",
     "q": '("5-methyltetrahydrofolate" OR "CSF neurotransmitters") AND ("epileptic encephalopathy" OR "generalized epilepsy")'},
    {"id": "lgs",         "label": "LGS treatment",
     "q": '"Lennox-Gastaut syndrome" AND (treatment OR outcomes) AND (child* OR pediatric)'},
    {"id": "dee",         "label": "Dravet / DEE",
     "q": '("Dravet syndrome" OR "developmental epileptic encephalopathy") AND (treatment OR therapy)'},
]

PREPRINT_KEYWORDS = [
    "epilepsy", "seizure", "doose", "myoclonic", "lennox-gastaut", "dravet",
    "rufinamide", "fenfluramine", "cannabidiol", "vns", "vagus nerve",
    "folate deficiency", "encephalopathy", "relutrigine", "clobazam", "atonic",
    "glut1", "cerebral folate", "perampanel",
]

# Updated June 2026 clinical state
BENNETT_CONTEXT = """
Bennett is male, age 3 (DOB Feb 7 2023), 17.7 kg (per 5/4/2026 CHOA visit; up from 16.7 kg on 3/30/26), height 98.5 cm.
Diagnosis: EMAS (epilepsy with myoclonic-atonic seizures / Doose), generalized, with consideration for evolution to Lennox-Gastaut syndrome. Seizure types: myoclonic-atonic head drops, 3-15 per day in clusters under 10 minutes, plus nocturnal events (mostly EEG signature, tracking for possible ictal arousals).
Comorbidities: migraine, cognitive impairment.
Etiology: likely genetic, currently unknown (Invitae epilepsy panel + WES non-diagnostic; TSC2 VUS, paternally inherited, likely benign; trio WGS under evaluation).
EEG (Feb 16 2026): numerous atonic seizures with head drops; frequent multifocal spike/spike-and-wave; sleep-activated diffuse spike/polyspike and slow-wave bursts with overriding fast activity in runs of 3 to 9 sec (pattern consistent with LGS-evolution concern).
Currently receiving PT, OT, and speech therapy; IEP in place through pre-K.

FAILED therapies: prednisolone (initial benefit only), topiramate (19-day seizure freedom then escape), felbamate (appetite suppression and dehydration), valproate/Depakote (FORMAL ALLERGY, hives, NEVER suggest again), ketogenic diet (MAD, 3:1 CKD, 1.5:1 CKD all insufficient despite strong ketosis; now fully DISCONTINUED, no longer on any ketogenic diet).

CURRENT ASMs:
- Levetiracetam (Keppra) 100 mg/mL: 2 mL (200 mg) TID
- Cannabidiol (Epidiolex) 100 mg/mL: 0.9 mL (90 mg) AM, 1.5 mL (150 mg) PM (nocturnal dose titrating up)
- Clobazam (Onfi) 2.5 mg/mL: 1 mL AM, 4 mL PM
Rescue: Valtoco (diazepam nasal), Klonopin ODT (clonazepam).

NEXT STEPS (Dr. Ribeiro-Pinto, CHOA, May 2026):
- Plan A: Optimize Epidiolex (nocturnal titration). No clobazam/LEV changes yet.
- Plan B: Add rufinamide (LGS-approved, targets drop attacks). Not yet started.
- Plan C: VNS education / near-future neuromodulation planning.
- Also on table: perampanel, fenfluramine.
- Consider ongoing trial enrollment if seizures persist once stable.
- Summer 2026: 48-hr EMU video-EEG admission + LP under sedation for CSF neurotransmitters (5-MTHF, cerebral folate deficiency, biogenic amines, pterins, GLUT1 deficiency screen via fasting glucose ratio).
- August 4 2026: Next neurology follow-up.

KEY PK NOTE: Epidiolex inhibits CYP2C19, elevating clobazam N-desmethyl (active metabolite). Rufinamide also weakly inhibits CYP2C19. Flag any drug interaction data on this pathway.

SAFETY FLAG: Sodium channel blockers (carbamazepine, oxcarbazepine, lamotrigine, phenytoin) are documented to WORSEN drop attacks in EMAS/LGS. Flag prominently if any study involves these in this phenotype.

WORSENING SIGNAL: Levetiracetam has a published seizure-worsening signal in EMAS (Pellacani et al., Brain Communications 2026, fcaf507; about 5% worsening, comparable to carbamazepine; the same paper reports older ASMs outperform newer ones in EMAS). Bennett is currently on levetiracetam, so flag any confirmatory or contradictory data at HIGH priority.
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
        "datetype": "pdat",
        "reldate": str(DAYS_LOOKBACK),
        "retmode": "json",
        "retmax": "50",
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    try:
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                         params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"[WARN] PubMed search failed for query '{query[:50]}...': {e}")
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
    start = today - timedelta(days=DAYS_LOOKBACK)
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
def scan_trials(snapshots: dict) -> tuple[list[dict], dict]:
    updates = []
    updated = dict(snapshots)

    # Watch EMERALD specifically
    try:
        r = requests.get(
            f"https://clinicaltrials.gov/api/v2/studies/{EMERALD_NCT}?format=json",
            timeout=15)
        r.raise_for_status()
        data = r.json()
        status = (data.get("protocolSection", {})
                      .get("statusModule", {})
                      .get("overallStatus", "UNKNOWN"))
        title  = (data.get("protocolSection", {})
                      .get("identificationModule", {})
                      .get("briefTitle", "Relutrigine (EMERALD) trial"))
        prior  = updated.get(EMERALD_NCT)
        if not prior or prior["status"] != status:
            prior_status = prior["status"] if prior else "first observation"
            updates.append({
                "id":       EMERALD_NCT,
                "source":   "ClinicalTrials.gov",
                "title":    f"{title} ({EMERALD_NCT})",
                "abstract": f"Trial status: {status}. Previously: {prior_status}.",
                "journal":  "ClinicalTrials.gov — EMERALD watch",
                "date":     datetime.now(timezone.utc).date().isoformat(),
                "url":      f"https://clinicaltrials.gov/study/{EMERALD_NCT}",
                "score":    "HIGH",
                "reason":   f"EMERALD relutrigine trial status changed to {status} — check enrollment eligibility",
            })
        updated[EMERALD_NCT] = {"status": status, "title": title}
    except Exception as e:
        print(f"[WARN] EMERALD trial check failed: {e}")

    # Scan for new recruiting EMAS/LGS trials
    try:
        r = requests.get(
            "https://clinicaltrials.gov/api/v2/studies",
            params={
                "query.cond": "myoclonic atonic epilepsy OR Doose syndrome OR Lennox-Gastaut",
                "filter.overallStatus": "RECRUITING",
                "pageSize": "20",
                "format": "json",
            },
            timeout=15)
        r.raise_for_status()
        for s in r.json().get("studies", []):
            nct    = (s.get("protocolSection", {})
                       .get("identificationModule", {})
                       .get("nctId"))
            title  = (s.get("protocolSection", {})
                       .get("identificationModule", {})
                       .get("briefTitle", ""))
            status = (s.get("protocolSection", {})
                       .get("statusModule", {})
                       .get("overallStatus", ""))
            conditions = ", ".join(
                s.get("protocolSection", {})
                 .get("conditionsModule", {})
                 .get("conditions", []))
            if nct and nct != EMERALD_NCT and nct not in updated:
                updates.append({
                    "id":       nct,
                    "source":   "ClinicalTrials.gov",
                    "title":    f"New recruiting trial: {title} ({nct})",
                    "abstract": f"Recruiting for: {conditions or 'epilepsy'}. Status: {status}.",
                    "journal":  "ClinicalTrials.gov — new trial",
                    "date":     datetime.now(timezone.utc).date().isoformat(),
                    "url":      f"https://clinicaltrials.gov/study/{nct}",
                    "score":    "MEDIUM",
                    "reason":   "New recruiting trial matching EMAS/LGS phenotype — verify age/eligibility",
                })
                updated[nct] = {"status": status, "title": title}
    except Exception as e:
        print(f"[WARN] ClinicalTrials scan failed: {e}")

    return updates, updated


# ---------------------------------------------------------------------------
# Claude scoring
# ---------------------------------------------------------------------------
def score_with_claude(papers: list[dict]) -> list[dict]:
    if not papers:
        return []

    results = []
    batch_size = 8

    for i in range(0, len(papers), batch_size):
        batch = papers[i : i + batch_size]
        listing = "\n\n---\n\n".join(
            f"[{idx}] TITLE: {p['title']}\nABSTRACT: {(p.get('abstract') or '')[:500]}"
            for idx, p in enumerate(batch)
        )
        prompt = (
            f"Evaluate research papers for a specific pediatric epilepsy patient.\n\n"
            f"PATIENT:\n{BENNETT_CONTEXT}\n\n"
            f"NON-PHARMACOLOGIC / SUPPORTIVE THERAPIES (evidence-backed only):\n"
            f"Evaluate data on neuromodulation (VNS, and RNS/DBS where age-relevant) and on "
            f"rehabilitative or developmental therapies in pediatric DEE: occupational therapy, "
            f"physical therapy, speech-language therapy, feeding/swallowing therapy, and "
            f"developmental or behavioral intervention. NOTE: ABA is autism-specific; include only "
            f"if a study addresses behavioral therapy in DEE without requiring an autism diagnosis. "
            f"For experimental cell-based therapies (stem cell, MSC, neural progenitor or interneuron "
            f"grafts, e.g. NRTX-1001), score as research-only unless a controlled trial reports "
            f"efficacy in generalized pediatric epilepsy; flag evidence level explicitly.\n\n"
            f"SCORING CRITERIA:\n"
            f"HIGH = directly actionable: EMAS/Doose-specific data, rufinamide/fenfluramine/perampanel/VNS "
            f"efficacy or safety, active titration drugs, CFD/CSF/GLUT1 workup, CYP2C19 interactions, "
            f"LEV worsening data, trial enrollment opportunity Bennett could access\n"
            f"MEDIUM = relevant context: LGS, Dravet, other DEE, related drug class, "
            f"pediatric generalized epilepsy outcomes, evidence-backed OT/PT/speech/feeding/developmental "
            f"therapy outcomes in DEE\n"
            f"LOW = tangential: general epilepsy, adult-only studies, unrelated mechanism, "
            f"low-quality or uncontrolled experimental therapy\n"
            f"SKIP = not applicable\n\n"
            f"PAPERS:\n{listing}\n\n"
            f"Return ONLY a JSON array with no markdown fences:\n"
            f'[{{"index": 0, "score": "HIGH", "reason": "one concise sentence"}}]'
        )

        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            r.raise_for_status()
            raw    = r.json()["content"][0]["text"]
            scores = json.loads(raw.replace("```json", "").replace("```", "").strip())
            for p_idx, paper in enumerate(batch):
                match = next((s for s in scores if s["index"] == p_idx), None)
                results.append({
                    **paper,
                    "score":  match["score"]  if match else "LOW",
                    "reason": match["reason"] if match else "",
                })
        except Exception as e:
            print(f"[WARN] Claude scoring failed for batch {i//batch_size}: {e}")
            for paper in batch:
                results.append({**paper, "score": "LOW", "reason": "Scoring error"})

        time.sleep(0.3)

    return results


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------
SCORE_COLORS = {
    "HIGH":   {"border": "#dc2626", "bg": "#fef2f2", "badge_bg": "#dc2626", "badge_fg": "#ffffff"},
    "MEDIUM": {"border": "#d97706", "bg": "#fffbeb", "badge_bg": "#d97706", "badge_fg": "#ffffff"},
    "LOW":    {"border": "#9ca3af", "bg": "#f9fafb", "badge_bg": "#6b7280", "badge_fg": "#ffffff"},
}
SOURCE_BADGE = {
    "PubMed":            {"bg": "#1d4ed8", "fg": "#ffffff"},
    "medRxiv":           {"bg": "#059669", "fg": "#ffffff"},
    "bioRxiv":           {"bg": "#059669", "fg": "#ffffff"},
    "ClinicalTrials.gov":{"bg": "#7c3aed", "fg": "#ffffff"},
}


def _badge(text: str, bg: str, fg: str) -> str:
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'font-size:11px;font-family:monospace;padding:2px 8px;border-radius:4px;'
        f'font-weight:600;margin-right:4px">{text}</span>'
    )


def _paper_row(p: dict) -> str:
    sc = SCORE_COLORS.get(p["score"], SCORE_COLORS["LOW"])
    sb = SOURCE_BADGE.get(p["source"], SOURCE_BADGE["PubMed"])
    score_badge  = _badge(p["score"],  sc["badge_bg"], sc["badge_fg"])
    source_badge = _badge(p["source"], sb["bg"],       sb["fg"])
    date_str = f'<span style="font-family:monospace;font-size:11px;color:#6b7280">{p.get("date","")}</span>' if p.get("date") else ""
    reason_str = (
        f'<p style="margin:6px 0 0;font-size:12px;color:#374151;line-height:1.5">'
        f'&#8594; {p["reason"]}</p>'
    ) if p.get("reason") else ""
    abstract_preview = (p.get("abstract") or "")[:300]
    if len(p.get("abstract","")) > 300:
        abstract_preview += "…"
    return f"""
<div style="border:1px solid {sc['border']};border-left:4px solid {sc['border']};
     background:{sc['bg']};border-radius:6px;padding:14px 16px;margin-bottom:10px">
  <div style="margin-bottom:8px">{score_badge}{source_badge}{date_str}</div>
  <p style="margin:0 0 4px;font-size:14px;font-weight:600;color:#111827;line-height:1.4">
    <a href="{p['url']}" style="color:#1d4ed8;text-decoration:none">{p['title']}</a>
  </p>
  {f'<p style="margin:0 0 4px;font-size:12px;color:#6b7280;font-style:italic">{p["journal"]}</p>' if p.get("journal") else ""}
  {reason_str}
  {f'<p style="margin:8px 0 0;font-size:12px;color:#4b5563;line-height:1.6">{abstract_preview}</p>' if abstract_preview else ""}
</div>"""


def _section_header(label: str, count: int, color: str) -> str:
    return f"""
<div style="display:flex;align-items:center;margin:28px 0 12px">
  <div style="width:4px;height:20px;background:{color};border-radius:2px;margin-right:10px;flex-shrink:0"></div>
  <span style="font-size:13px;font-weight:600;color:#374151;text-transform:uppercase;
               letter-spacing:0.06em">{label}</span>
  <span style="font-family:monospace;font-size:12px;color:#9ca3af;margin-left:8px">{count}</span>
</div>"""


def build_email_html(
    items: list[dict],
    scan_date: str,
    pubmed_count: int,
    preprint_count: int,
    trial_count: int,
) -> str:
    high   = [i for i in items if i["score"] == "HIGH"]
    medium = [i for i in items if i["score"] == "MEDIUM"]
    low    = [i for i in items if i["score"] == "LOW"]

    high_html = "".join(_paper_row(p) for p in high) if high else (
        '<p style="color:#6b7280;font-size:13px;font-style:italic">No high-priority findings this week.</p>'
    )
    medium_html = "".join(_paper_row(p) for p in medium) if medium else (
        '<p style="color:#6b7280;font-size:13px;font-style:italic">No medium-priority findings this week.</p>'
    )
    low_summary = "".join(
        f'<li style="font-size:12px;color:#6b7280;margin-bottom:4px">'
        f'<a href="{p["url"]}" style="color:#9ca3af">{p["title"][:90]}{"…" if len(p["title"])>90 else ""}</a>'
        f'</li>'
        for p in low
    )
    low_section = (
        f'{_section_header("Low priority", len(low), "#9ca3af")}'
        f'<ul style="margin:0;padding-left:20px">{low_summary}</ul>'
    ) if low else ""

    no_results_msg = ""
    if not high and not medium and not low:
        no_results_msg = """
<div style="text-align:center;padding:40px 0">
  <p style="font-size:16px;color:#059669;font-weight:600">&#10003; No new relevant papers this week</p>
  <p style="font-size:13px;color:#6b7280">All sources scanned. Nothing above LOW threshold.</p>
</div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<div style="max-width:680px;margin:0 auto;padding:24px 16px">

  <!-- Header -->
  <div style="background:#111827;border-radius:8px;padding:20px 24px;margin-bottom:20px">
    <h1 style="margin:0 0 4px;font-size:18px;font-weight:600;color:#f9fafb">
      Bennett — Literature Surveillance
    </h1>
    <p style="margin:0;font-size:13px;color:#9ca3af;font-family:monospace">
      Week of {scan_date} &nbsp;·&nbsp; PubMed · medRxiv · bioRxiv · ClinicalTrials.gov
    </p>
  </div>

  <!-- Summary strip -->
  <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;
              padding:12px 16px;margin-bottom:20px;display:flex;gap:20px;
              font-family:monospace;font-size:13px;flex-wrap:wrap">
    <span><strong style="color:#dc2626">{len(high)}</strong>&nbsp;HIGH</span>
    <span style="color:#d1d5db">|</span>
    <span><strong style="color:#d97706">{len(medium)}</strong>&nbsp;MEDIUM</span>
    <span style="color:#d1d5db">|</span>
    <span><strong style="color:#6b7280">{len(low)}</strong>&nbsp;LOW</span>
    <span style="color:#d1d5db">|</span>
    <span style="color:#6b7280">{pubmed_count} PubMed · {preprint_count} preprints · {trial_count} trial updates</span>
  </div>

  <!-- Main content -->
  <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:20px 24px">
    {no_results_msg}
    {_section_header("High priority", len(high), "#dc2626") if not no_results_msg else ""}
    {high_html if not no_results_msg else ""}
    {_section_header("Medium priority", len(medium), "#d97706") if not no_results_msg else ""}
    {medium_html if not no_results_msg else ""}
    {low_section}
  </div>

  <!-- Footer -->
  <div style="padding:16px 0;text-align:center">
    <p style="font-size:11px;color:#9ca3af;margin:0;font-family:monospace">
      bennett-care · automated weekly scan · 8 PubMed vectors · EMAS/LGS/DEE phenotype
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
def main() -> None:
    print(f"[START] Bennett literature surveillance — {datetime.now(timezone.utc).isoformat()}")
    state = load_state()
    seen_pubmed    = set(state.get("seen_pubmed", []))
    seen_preprints = set(state.get("seen_preprints", []))
    trial_snapshots = state.get("trial_snapshots", {})

    # 1. PubMed
    print("[STEP 1] Querying PubMed across 8 vectors...")
    all_pmids: set[str] = set()
    for q in PUBMED_QUERIES:
        ids = pubmed_search(q["q"])
        all_pmids.update(ids)
        print(f"  {q['label']}: {len(ids)} results")
        time.sleep(0.15)
    new_pmids = [p for p in all_pmids if p not in seen_pubmed]
    print(f"  {len(new_pmids)} new PMIDs to fetch (of {len(all_pmids)} total)")

    pubmed_papers = pubmed_fetch(new_pmids[:40]) if new_pmids else []
    seen_pubmed.update(new_pmids)

    # 2. Preprints
    print("[STEP 2] Scanning medRxiv and bioRxiv...")
    medrxiv  = preprint_search("medrxiv")
    time.sleep(0.5)
    biorxiv  = preprint_search("biorxiv")
    new_preprints = [p for p in medrxiv + biorxiv if p["id"] not in seen_preprints]
    print(f"  {len(new_preprints)} new preprints")
    seen_preprints.update(p["id"] for p in new_preprints)

    # 3. ClinicalTrials
    print("[STEP 3] Checking ClinicalTrials.gov...")
    trial_updates, trial_snapshots = scan_trials(trial_snapshots)
    print(f"  {len(trial_updates)} trial updates")

    # 4. Score with Claude
    to_score = pubmed_papers + new_preprints
    print(f"[STEP 4] Scoring {len(to_score)} papers with Claude...")
    scored = score_with_claude(to_score)
    relevant = [p for p in scored if p["score"] != "SKIP"]
    all_items = relevant + trial_updates
    print(f"  {sum(1 for p in relevant if p['score']=='HIGH')} HIGH  "
          f"{sum(1 for p in relevant if p['score']=='MEDIUM')} MEDIUM  "
          f"{sum(1 for p in relevant if p['score']=='LOW')} LOW")

    # 5. Save state (before email so state is preserved even if email fails)
    state["seen_pubmed"]      = list(seen_pubmed)
    state["seen_preprints"]   = list(seen_preprints)
    state["trial_snapshots"]  = trial_snapshots
    state["last_run"]         = datetime.now(timezone.utc).isoformat()
    save_state(state)

    # 6. Build and send email
    scan_date  = datetime.now(timezone.utc).strftime("%B %d, %Y")
    high_count = sum(1 for p in all_items if p["score"] == "HIGH")
    subject    = (
        f"Bennett Lit Scan — {scan_date} — "
        f"{high_count} HIGH priority" if high_count
        else f"Bennett Lit Scan — {scan_date} — No high-priority findings"
    )
    html = build_email_html(
        items          = all_items,
        scan_date      = scan_date,
        pubmed_count   = len(pubmed_papers),
        preprint_count = len(new_preprints),
        trial_count    = len(trial_updates),
    )
    print(f"[STEP 5] Sending email: {subject}")
    send_email(subject, html)
    print("[DONE]")


if __name__ == "__main__":
    main()
