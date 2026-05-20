# Known data issues

Aggregate notes on data-quality patterns in `data/seizure_log.xlsx`. This file
lives in the repo so future readers (and future Claude sessions) interpret
report output correctly. **It deliberately contains no specific dates or daily
seizure counts** — only aggregate descriptions of the patterns.

Snapshot date: 2026-05-20. Re-run `bennett-care inspect-mismatches` after
substantial back-fill to refresh these numbers.

## Cluster Detail vs. Daily Total mismatches

The Phase 1 pipeline recomputes daily counts from the `Cluster Detail` sheet
(per CLAUDE.md, that sheet is the source of truth). The `Daily Total` column in
`All Data` is informational and surfaced as a warning when the two disagree.

At the snapshot date there are **67 mismatched days out of 827 monitored days
(~8.1%)**. They split into two distinct patterns:

### Pattern A — Cluster Detail more granular than Daily Total

- ~80% of mismatches (after excluding Pattern B below).
- `cluster_sum > daily_total`; median diff ≈ +8, mean ≈ +11.
- All occur on `Day Status = logged` days.
- **Likely cause**: `Daily Total` is a quick end-of-day number. Cluster Detail
  rows are added/refined later, but `Daily Total` is not recomputed. The
  cluster-derived count is the right one to use.

**Action**: none in code. The report already uses Cluster Detail as source of
truth and flags the mismatch in the Appendix's "Mismatch?" column. The +6ish
mean of `(cluster_sum − daily_total)` across mismatched days is **expected
under this pattern** and should not be read as a bug.

### Pattern B — Missing cluster-level data

- 7 days at the snapshot date.
- `cluster_sum = 0` but `daily_total > 0` (some values >50).
- **Cause**: a `Daily Total` was entered (so the user knew it was a seizure day)
  but the per-cluster rows were never filled in.
- **Impact**: these days currently appear as **zero-seizure days** in every
  cluster-based analysis — daily totals chart, Section 6 type distribution,
  Section 7 flag counts, Section 4 hour-of-day. Under-reports the totals on
  those days by the full Daily-Total amount.

**Action (manual)**:
1. Run `bennett-care inspect-mismatches --log data/seizure_log.xlsx --top 20 --output output`
2. Open the generated CSV in Excel. The Pattern B days have
   `cluster_detail_sum == 0` and `all_data_daily_total > 0` — they sort to the
   top by `|diff|`.
3. Back-fill each day's clusters in `Cluster Detail` from notes/memory.
4. Re-run `inspect-mismatches` to confirm; update this file's snapshot date and
   counts.

A subset of negative-diff days have `cluster_sum > 0 but < daily_total` — these
are partial back-fills of the same kind. Audit them at the same time.

## Seizure Type coverage (Section 6)

`Seizure Type` is recorded on ~5% of clusters in the current log; ~95% are
blank. Section 6 of the report plots only the typed clusters and states the
coverage in the caption. Back-filling type information from historical notes
would give Section 6 substantially more clinical signal — this is a known gap,
not a defect.
