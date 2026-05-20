"""Assemble the Phase 1 pre-visit summary Word document.

Section layout (per CLAUDE.md):
  Brand band: "Bennett — Pre-Visit Summary | Visit: <date>"
  1. Header (patient, visit, neurologist, current regimen)
  2. Daily totals chart
  3. 14-day rolling average chart
  4. Time-of-day heatmap
  5. Pre/post statistics table for the two most recent med changes
  6. Seizure type distribution
  7. Flags summary (rescue, ended-in-tonic, school)
  8. Notable days (>2 SD above trailing 28-day mean)
  9. Open clinical questions (empty template)
  10. Appendix: raw daily counts for last 30 days

All output is factual; no causal language. The user composes narrative
from this document during the clinic visit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import _Cell

from .ingest import SeizureLog, count_flag
from .stats import (
    PrePostAnalysis,
    format_regimen,
    notable_days,
    rescue_event_dates,
)
from .visualize import ChartPaths

# --- Patient constants (Phase 1; later we may externalize to a config file) ---
PATIENT_NAME: str = "Bennett"
PATIENT_WEIGHT_LB: float = 35.0
PATIENT_WEIGHT_KG: float = 15.9
NEUROLOGIST: str = "Dr. Anna Lecticia Ribeiro-Pinto"
INSTITUTION: str = "Children's Healthcare of Atlanta"

# --- Styling ---
BRAND_COLOR_HEX: str = "1F4E79"  # dark blue
BODY_FONT: str = "Calibri"
BODY_PT: int = 11
HEADING_PT: int = 13
APPENDIX_DAYS: int = 30


@dataclass(frozen=True)
class ReportInputs:
    """Everything build_report needs. Bundling keeps the call site readable."""

    log: SeizureLog
    chart_paths: ChartPaths
    analyses: list[PrePostAnalysis]
    visit_date: date
    lookback_days: int = 90


def build_report(inputs: ReportInputs, *, output_path: str | Path) -> Path:
    """Build the pre-visit summary .docx and write it to ``output_path``."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    _configure_page(doc)
    _configure_default_style(doc)

    _add_brand_band(doc, inputs.visit_date)
    _add_section_1_header(doc, inputs)
    _add_chart_section(doc, "2. Daily seizure counts", inputs.chart_paths.daily_totals,
                       caption=f"Last {inputs.lookback_days} days of monitored data. "
                               "Dashed lines mark regimen changes.")
    _add_chart_section(doc, "3. 14-day rolling average", inputs.chart_paths.rolling_avg,
                       caption="14-day trailing mean of daily seizure counts.")
    _add_chart_section(doc, "4. Time-of-day heatmap", inputs.chart_paths.tod_heatmap,
                       caption=f"Hour × day-of-week intensity of seizures in clusters with a "
                               f"recorded time, last {inputs.lookback_days} days.")
    _add_section_5_prepost(doc, inputs)
    _add_chart_section(doc, "6. Seizure type distribution", inputs.chart_paths.type_distribution,
                       caption="Distribution among clusters that have a Seizure Type recorded "
                               "(coverage shown in chart).")
    _add_section_7_flags(doc, inputs)
    _add_section_8_notable(doc, inputs)
    _add_section_9_questions(doc)
    _add_section_10_appendix(doc, inputs)

    doc.save(output_path)
    return output_path


# --------------------------------------------------------------------------- #
# Page / style                                                                #
# --------------------------------------------------------------------------- #


def _configure_page(doc: DocxDocument) -> None:
    for section in doc.sections:
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)
        section.top_margin = Inches(0.6)
        section.bottom_margin = Inches(0.8)


def _configure_default_style(doc: DocxDocument) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(BODY_PT)


# --------------------------------------------------------------------------- #
# Brand band                                                                  #
# --------------------------------------------------------------------------- #


def _add_brand_band(doc: DocxDocument, visit_date: date) -> None:
    table = doc.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    _set_cell_background(cell, BRAND_COLOR_HEX)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    para = cell.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(f"{PATIENT_NAME} — Pre-Visit Summary     |     Visit: {visit_date.isoformat()}")
    run.font.name = BODY_FONT
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    # Vertical padding on the band
    _set_cell_padding(cell, top=120, bottom=120)
    # Spacer paragraph below the band
    doc.add_paragraph()


def _set_cell_background(cell: _Cell, hex_color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), hex_color)
    tc_pr.append(shading)


def _set_cell_padding(cell: _Cell, *, top: int = 80, bottom: int = 80) -> None:
    """Padding values are in twentieths of a point (dxa). 120 ≈ 6pt."""
    tc_pr = cell._tc.get_or_add_tcPr()
    margins = OxmlElement("w:tcMar")
    for side, value in (("top", top), ("bottom", bottom)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(value))
        el.set(qn("w:type"), "dxa")
        margins.append(el)
    tc_pr.append(margins)


# --------------------------------------------------------------------------- #
# Section helpers                                                             #
# --------------------------------------------------------------------------- #


def _add_section_heading(doc: DocxDocument, text: str) -> None:
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.font.name = BODY_FONT
    run.font.size = Pt(HEADING_PT)
    run.font.bold = True
    # Bottom border on the paragraph
    p_pr = para._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "808080")
    p_bdr.append(bottom)
    p_pr.append(p_bdr)


def _add_caption(doc: DocxDocument, text: str) -> None:
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.italic = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x60, 0x60, 0x60)


def _add_chart_section(doc: DocxDocument, heading: str, image_path: Path, *, caption: str) -> None:
    _add_section_heading(doc, heading)
    doc.add_picture(str(image_path), width=Inches(6.5))
    _add_caption(doc, caption)


# --------------------------------------------------------------------------- #
# Section 1: Header                                                           #
# --------------------------------------------------------------------------- #


def _add_section_1_header(doc: DocxDocument, inputs: ReportInputs) -> None:
    _add_section_heading(doc, "1. Patient & current regimen")
    log = inputs.log

    facts = [
        ("Patient", f"{PATIENT_NAME} (~{PATIENT_WEIGHT_LB:.0f} lb / {PATIENT_WEIGHT_KG:.1f} kg)"),
        ("Visit date", inputs.visit_date.isoformat()),
        ("Data through", log.latest_date.date().isoformat()),
        ("Monitored days in log", f"{len(log.daily)}"),
        ("Excluded dates (unmonitored / uncertain)", f"{len(log.excluded_dates)}"),
        ("Neurologist", f"{NEUROLOGIST}, {INSTITUTION}"),
    ]
    table = doc.add_table(rows=len(facts), cols=2)
    table.autofit = False
    for i, (label, value) in enumerate(facts):
        row = table.rows[i]
        row.cells[0].width = Inches(2.2)
        row.cells[1].width = Inches(4.3)
        _set_cell_text(row.cells[0], label, bold=True)
        _set_cell_text(row.cells[1], value)

    # Current regimen
    doc.add_paragraph()
    real_changes = [c for c in log.med_changes if c.regimen]
    if real_changes:
        latest = real_changes[-1]
        sub = doc.add_paragraph()
        run = sub.add_run(f"Current regimen (as of {latest.date.date().isoformat()}):")
        run.bold = True
        for drug, doses in latest.regimen.items():
            dose_str = ", ".join(f"{d.amount_ml} mL {d.timing}" for d in doses)
            doc.add_paragraph(f"  • {drug}: {dose_str}")
    else:
        doc.add_paragraph("No parsed regimen found in the log.")


def _set_cell_text(cell: _Cell, text: str, *, bold: bool = False) -> None:
    cell.text = ""
    run = cell.paragraphs[0].add_run(text)
    run.font.name = BODY_FONT
    run.font.size = Pt(BODY_PT)
    if bold:
        run.bold = True


# --------------------------------------------------------------------------- #
# Section 5: Pre/post statistics                                              #
# --------------------------------------------------------------------------- #


def _add_section_5_prepost(doc: DocxDocument, inputs: ReportInputs) -> None:
    _add_section_heading(doc, "5. Pre / post statistics — two most recent regimen changes")
    if not inputs.analyses:
        doc.add_paragraph("No real regimen changes in the log; nothing to analyze.")
        return

    intro = (
        "Each row reports the mean daily seizure count in a window before and after "
        "the change, with a bootstrap-BCa 95% CI (10,000 resamples, seeded). "
        "Hedges' g is the standardized effect (post − pre); negative means lower post-mean. "
        "Mann-Whitney p is a two-sided non-parametric test, no significance threshold applied."
    )
    _add_caption(doc, intro)

    for analysis in inputs.analyses:
        para = doc.add_paragraph()
        run = para.add_run(f"Change on {analysis.change_date.date().isoformat()} — {analysis.delta_str}")
        run.bold = True
        para2 = doc.add_paragraph()
        r2 = para2.add_run(f"Regimen after change: {analysis.regimen_str}")
        r2.italic = True
        r2.font.size = Pt(9)
        r2.font.color.rgb = RGBColor(0x60, 0x60, 0x60)

        headers = ["Window", "Pre mean (95% CI)", "n", "Post mean (95% CI)", "n",
                   "Hedges' g", "MW p", "Window crosses other change?"]
        table = doc.add_table(rows=1 + len(analysis.windows), cols=len(headers))
        for j, h in enumerate(headers):
            _set_cell_text(table.rows[0].cells[j], h, bold=True)
        for i, days in enumerate(sorted(analysis.windows)):
            w = analysis.windows[days]
            row = table.rows[i + 1].cells
            _set_cell_text(row[0], f"{days} d")
            _set_cell_text(row[1], _fmt_mean_ci(w.pre))
            _set_cell_text(row[2], str(w.pre.n))
            _set_cell_text(row[3], _fmt_mean_ci(w.post))
            _set_cell_text(row[4], str(w.post.n))
            _set_cell_text(row[5], f"{w.hedges_g:+.3f}" if w.hedges_g is not None else "n/a")
            _set_cell_text(row[6], f"{w.mann_whitney_p:.4f}" if w.mann_whitney_p is not None else "n/a")
            overlap_parts = []
            if w.pre_contains_other_change:
                overlap_parts.append("pre")
            if w.post_contains_other_change:
                overlap_parts.append("post")
            _set_cell_text(row[7], ", ".join(overlap_parts) if overlap_parts else "no")
        doc.add_paragraph()


def _fmt_mean_ci(ci) -> str:
    if ci.n == 0:
        return "—"
    if ci.low == ci.high == ci.mean:
        return f"{ci.mean:.2f}"
    return f"{ci.mean:.2f} [{ci.low:.2f}, {ci.high:.2f}]"


# --------------------------------------------------------------------------- #
# Section 7: Flags                                                            #
# --------------------------------------------------------------------------- #


def _add_section_7_flags(doc: DocxDocument, inputs: ReportInputs) -> None:
    _add_section_heading(doc, "7. Flag summary")
    log = inputs.log
    end = log.latest_date
    start = end - __import__("pandas").Timedelta(days=inputs.lookback_days - 1)
    in_window = log.clusters[(log.clusters["Date"] >= start) & (log.clusters["Date"] <= end)]

    rescue_clusters = count_flag(in_window, "rescue_meds_given")
    rescue_notes = sum(
        1
        for c in log.med_changes
        if not c.regimen
        and "rescue" in c.raw.lower()
        and start <= c.date <= end
    )
    rescue_dates = rescue_event_dates(log)
    rescue_dates_in_window = sum(1 for d in rescue_dates if start <= d <= end)
    ended_tonic = count_flag(in_window, "ended_in_tonic")
    school = count_flag(in_window, ["school_data", "school_data_pending"])

    headers = ["Event type", "Count in last 90 days", "Notes"]
    rows = [
        ("Rescue meds given (cluster flags)", rescue_clusters, "Clusters tagged `rescue_meds_given`."),
        ("Rescue notes in Meds column", rescue_notes, "Free-text rescue entries co-located with regimen log."),
        ("Distinct dates with rescue evidence", rescue_dates_in_window, "Union of the two rows above."),
        ("Ended in tonic", ended_tonic, "Clusters tagged `ended_in_tonic`."),
        ("School events", school, "Clusters tagged `school_data` or `school_data_pending`."),
    ]
    table = doc.add_table(rows=1 + len(rows), cols=3)
    for j, h in enumerate(headers):
        _set_cell_text(table.rows[0].cells[j], h, bold=True)
    for i, (label, count, note) in enumerate(rows):
        cells = table.rows[i + 1].cells
        _set_cell_text(cells[0], label)
        _set_cell_text(cells[1], str(count))
        _set_cell_text(cells[2], note)


# --------------------------------------------------------------------------- #
# Section 8: Notable days                                                     #
# --------------------------------------------------------------------------- #


def _add_section_8_notable(doc: DocxDocument, inputs: ReportInputs) -> None:
    _add_section_heading(doc, "8. Notable days — > 2 SD above trailing 28-day mean")
    log = inputs.log
    end = log.latest_date
    pd = __import__("pandas")
    start = end - pd.Timedelta(days=inputs.lookback_days - 1)
    notable = notable_days(log.daily)
    in_window = notable.loc[(notable.index >= start) & (notable.index <= end)]

    if in_window.empty:
        doc.add_paragraph(f"No days in the last {inputs.lookback_days} days exceeded 2 SD above "
                          "the trailing 28-day mean.")
        return

    headers = ["Date", "Count", "Trailing 28d mean", "Trailing 28d SD", "Z-score"]
    table = doc.add_table(rows=1 + len(in_window), cols=len(headers))
    for j, h in enumerate(headers):
        _set_cell_text(table.rows[0].cells[j], h, bold=True)
    for i, (idx, row) in enumerate(in_window.iterrows()):
        cells = table.rows[i + 1].cells
        _set_cell_text(cells[0], idx.date().isoformat())
        _set_cell_text(cells[1], str(int(row["count"])))
        _set_cell_text(cells[2], f"{row['trailing_mean']:.2f}")
        _set_cell_text(cells[3], f"{row['trailing_std']:.2f}")
        _set_cell_text(cells[4], f"{row['z_score']:+.2f}")


# --------------------------------------------------------------------------- #
# Section 9: Open questions                                                   #
# --------------------------------------------------------------------------- #


def _add_section_9_questions(doc: DocxDocument) -> None:
    _add_section_heading(doc, "9. Open clinical questions")
    para = doc.add_paragraph()
    r = para.add_run("(Add questions here before the visit.)")
    r.italic = True
    r.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    for _ in range(5):
        doc.add_paragraph("• ")


# --------------------------------------------------------------------------- #
# Section 10: Appendix                                                        #
# --------------------------------------------------------------------------- #


def _add_section_10_appendix(doc: DocxDocument, inputs: ReportInputs) -> None:
    _add_section_heading(doc, f"10. Appendix — raw daily counts, last {APPENDIX_DAYS} days")
    pd = __import__("pandas")
    daily = inputs.log.daily
    end = inputs.log.latest_date
    start = end - pd.Timedelta(days=APPENDIX_DAYS - 1)
    window = daily.loc[(daily.index >= start) & (daily.index <= end)]

    headers = ["Date", "Day", "Count", "Recorded Daily Total", "Mismatch?"]
    table = doc.add_table(rows=1 + len(window), cols=len(headers))
    for j, h in enumerate(headers):
        _set_cell_text(table.rows[0].cells[j], h, bold=True)
    for i, (idx, row) in enumerate(window.iterrows()):
        cells = table.rows[i + 1].cells
        _set_cell_text(cells[0], idx.date().isoformat())
        _set_cell_text(cells[1], idx.strftime("%a"))
        _set_cell_text(cells[2], str(int(row["count"])))
        _set_cell_text(cells[3], str(int(row["daily_total_recorded"])))
        _set_cell_text(cells[4], "yes" if row["mismatch"] else "")
