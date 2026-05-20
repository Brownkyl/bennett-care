# bennett-care

A local, private toolkit for analyzing seizure data for Bennett, supporting clinic visits with his neurologist. **Phase 1 = a CLI tool that generates a pre-visit summary Word document from an Excel seizure log.**

## Patient context

Every design decision in this repo serves Bennett's care. Hold this context in mind whenever you're making judgment calls about what to surface, how to phrase output, or how conservative to be with claims.

- **Patient**: Bennett, ~35 lb (15.9 kg) child
- **Diagnosis**: EMAS (epilepsy with myoclonic-atonic seizures), possibly evolving toward Lennox-Gastaut
- **Neurologist**: Dr. Anna Lecticia Ribeiro-Pinto, Children's Healthcare of Atlanta
- **Current regimen (as of 2026-05-04)**:
  - Epidiolex 0.9 mL AM / 1.5 mL PM
  - Clobazam 1 mL AM / 4 mL PM
  - Levetiracetam 2 mL at breakfast, lunch, dinner
- **Rescue meds**: Valtoco, Klonopin ODT
- **Allergy**: Valproate (skin reaction, April 2026) — formally documented
- **Diet**: Classic ketogenic diet has been weaned
- **Failed prior ASMs**: prednisolone, topiramate, felbamate, valproate
- **Active treatment plan**:
  - Plan A: continue Epidiolex optimization
  - Plan B: add rufinamide next
  - Plan C: VNS planning
- **Upcoming**: Summer 2026 EMU with LP for CSF neurotransmitters

## Hard constraints (non-negotiable)

1. **All patient data stays local.** Bennett's data must never be sent to any external API, cloud service, telemetry endpoint, or remote logging. No exceptions. If a dependency tries to phone home, remove it.
2. **No causal claims in output.** The decision-support boundary is rigorous statistics with effect sizes and confidence intervals — and that's it. Never write "the drug worked," "X reduced seizures," "responded well," or similar. Output is *facts and numbers*; the user composes narrative.
3. **`data/`, `output/`, and `reference/` are gitignored.** Never commit anything from these directories. Never paste their contents into commit messages, PR descriptions, or anywhere shared.

## Engineering conventions

- **Python**: 3.11+ (3.12 in use, managed by uv)
- **Env/deps**: `uv` (uv-managed venv + lockfile)
- **Core libs**: `pandas`, `openpyxl`, `matplotlib`, `python-docx`, `click`, `pytest`
- **Style**: type hints on public functions; docstrings only where the *why* isn't obvious from names. No comments restating what code does.
- **Tests**: `pytest`. Each module has a sibling test file. Tests must not require the real seizure log — use fixtures with synthetic data shaped like the real schema.
- **CLI shape**: subcommands under `bennett-care`. Phase 1 subcommand is `visit-prep`.
- **Commit policy**: commit after each working module passes its tests. Conventional-style messages (`feat:`, `fix:`, `test:`, `docs:`). Never amend a pushed commit. Never `--no-verify`.

## Data model

The Excel log lives at `data/seizure_log.xlsx` (gitignored). Two sheets:

### Sheet: `All Data`
- `Date` — daily, Feb 2024 to present (~819 rows)
- `Cluster 1` … `Cluster 17` — per-cluster seizure counts for that date (mostly blank)
- `Daily Total` — total seizures that day
- `7 Day Avg`, `14 Day Avg` — precomputed rolling averages
- `Meds (first day of updated meds noted)` — free-text, semicolon-separated regimen entry on the day a change takes effect; blank rows between changes mean "no change." Format example:
  > `Clobazam 1mL am; 4mL pm; Keppra 2mL 3x; Epidiolex 0.9mL am; 1.5mL pm`
  - Drug name carries forward across `;`-separated fragments until a new drug name appears
  - Common timings: `am`, `pm`, `3x` (three times daily), etc.
- `Diet` — free text
- `Notes` — free text

### Sheet: `Cluster Detail`
- `Date`, `Cluster #`, `Start Time`, `Duration (min)`, `Seizure Count`, `Seizure Type`, `Day Status`, `Verified`, `Flags`, `Notes`
- ~2,257 rows
- `Seizure Type`: free-text descriptors. Observed vocabulary: `atonic`, `myoclonic-atonic`, `myoclonic-to-tonic`, `tonic`, `tonic-clonic`. Normalize via lowercase + strip.
- `Flags`: comma-separated lowercase snake_case tokens. Use **exact-token match** after splitting on `,` and stripping. Observed vocabulary (21 tokens):
  - **Clinical**: `rescue_meds_given`, `ended_in_tonic`, `school_data`, `school_data_pending`
  - **Data quality** (informational): `ambiguous_am_pm`, `approx_count`, `approx_dur`, `at_least`, `bare_count`, `count_range_*`, `date_corrected_from_*`, `dur_range_*`, `no_time`, `orphan_time_line`, `partial_data_loss`, `partial_unmonitored_note`, `suspected_event`, `unknown_count`
  - Section 7 of the report counts: `rescue_meds_given`, `ended_in_tonic`, and `school_data` + `school_data_pending` (combined as "school events").
- `Day Status`: one of `logged` (normal), `unmonitored` (Bennett not being watched), `uncertain` (ambiguous). **Dates with `unmonitored` or `uncertain` status are excluded from the daily-count series, all charts, all stats.** Per-date status is unambiguous (no mixed-status dates observed).
- `Verified`: `Y` or blank — currently informational only.

### Source of truth
**Daily seizure counts are recomputed from `Cluster Detail`** (sum of `Seizure Count` per date). The `Daily Total` column in `All Data` is treated as informational; mismatches between the two are surfaced as a data-quality warning but do not block the report.

## Phase 1 deliverable

CLI:

```bash
bennett-care visit-prep --log data/seizure_log.xlsx --visit-date 2026-08-04 --lookback 90 --output output/
```

Output: a `.docx` in `output/` with sections:

1. Header (name, weight, visit date, current regimen parsed from most recent med change)
2. Daily totals chart, last 90 days, vertical lines at med changes with labels
3. 14-day rolling average chart, last 180 days
4. Time-of-day heatmap from Cluster Detail, last 90 days (hour × day-of-week)
5. Pre/post statistics table for the two most recent med changes — mean daily count with 95% CI, Hedges' g, sample sizes for 14, 28, 56 day windows
6. Seizure type distribution, last 90 days
7. Flags summary (rescue meds given, ended-in-tonic, school events)
8. Notable days (any day > 2 SD above trailing 28-day mean)
9. Open clinical questions — empty template section
10. Appendix: raw daily counts for last 30 days

### Phase 1 design decisions (locked)

- **Source of truth**: recompute daily counts from `Cluster Detail`; warn on `Daily Total` mismatch.
- **Lookback anchor**: all "last N days" windows end at the **latest date present in the log**, not at `--visit-date`. `--visit-date` is used only for the header and filename.
- **95% CI for mean daily count**: bootstrap BCa, 10,000 resamples, seeded for reproducibility.
- **Pre/post windows around med changes**: full requested window used even if it crosses another change date; any contaminated window is flagged with a footnote.
- **Notable-days threshold**: > 2 SD above a *strictly trailing* 28-day mean (the day itself is excluded). Skipped (not errored) when <28 days of history exist.
- **Effect size**: Hedges' g (small-sample-corrected Cohen's d).
- **Excluded dates**: clusters and dates with Day Status ∈ {`unmonitored`, `uncertain`} are dropped before any analysis. Excluded date count is surfaced in the report's data-quality footnote.
- **Section 6 (Seizure Type distribution)**: plot only clusters with a non-blank `Seizure Type` (≈5% of clusters in the current log). The chart caption must state the typed-cluster count and total, e.g. *"Based on 114 of 2,247 clusters (5%) with a Seizure Type recorded; remaining 95% untyped."*
- **Med changes vs. rescue notes in the Meds column**: a `MedChange` is "real" iff its parsed `regimen` is non-empty. Entries that parse to an empty regimen (e.g. *"Gave rescue meds at 9:28pm"*) are rescue-event notes — those dates feed the rescue-event count in Section 7 alongside the `rescue_meds_given` flag tokens, and are NOT drawn as vertical lines on the daily-totals chart.

## Project structure

```
bennett-care/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .gitignore                # MUST exclude data/, output/, reference/
├── data/                     # local seizure log, gitignored
├── output/                   # generated docs, gitignored
├── reference/                # clinical PDFs/DOCXs (PHI), gitignored
├── src/bennett_care/
│   ├── __init__.py
│   ├── ingest.py             # parse Excel log
│   ├── stats.py              # pre/post analysis, effect sizes, bootstrap CIs
│   ├── visualize.py          # matplotlib charts
│   ├── report.py             # python-docx assembly
│   └── cli.py                # click entry point
└── tests/
    ├── test_ingest.py
    ├── test_stats.py
    ├── test_visualize.py
    └── test_report.py
```

## How to collaborate with the user

- Ask clarifying questions before making non-obvious design decisions.
- Propose plans before large changes; wait for approval.
- After each working module passes its tests, commit and pause for review before moving on.
- Default to terse responses. The user reads diffs; don't summarize what the code does.
- When showing data parsed from the log in chat, **redact or summarize** — never paste large slices of Bennett's raw seizure data into responses. Counts, schemas, and aggregate previews are fine.
