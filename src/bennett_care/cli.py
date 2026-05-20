"""CLI for bennett-care.

Subcommands:
    visit-prep          Generate the pre-visit summary .docx for clinic.
    inspect-mismatches  Dump worst Cluster Detail vs. Daily Total mismatches.
    dump-notes          Dump verbatim Notes/Diet cells in a lookback window.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import click
import pandas as pd

from .ingest import load_log
from .notes import extract_notes
from .report import ReportInputs, build_report, convert_to_pdf
from .stats import analyze_recent_changes, current_baseline
from .visualize import render_all_charts


@click.group()
def cli() -> None:
    """bennett-care: local seizure-log analysis."""


@cli.command("visit-prep")
@click.option(
    "--log",
    "log_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the seizure log .xlsx.",
)
@click.option(
    "--visit-date",
    required=True,
    help="Visit date in YYYY-MM-DD format. Used in the report header and filename only — "
         "data windows are anchored to the latest date in the log per CLAUDE.md.",
)
@click.option("--lookback", default=90, type=int, show_default=True,
              help="Lookback days for charts in sections 2, 4, 6, 7.")
@click.option(
    "--rolling-lookback",
    default=90,
    type=int,
    show_default=True,
    help="Lookback days for the 14-day rolling-average chart (section 3).",
)
@click.option(
    "--output",
    "output_dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Directory for the generated .docx and the chart subdir.",
)
@click.option(
    "--pdf",
    is_flag=True,
    default=False,
    help="Also write a PDF next to the .docx via LibreOffice headless "
         "(requires `soffice` on PATH).",
)
def visit_prep(
    log_path: str,
    visit_date: str,
    lookback: int,
    rolling_lookback: int,
    output_dir: str,
    pdf: bool,
) -> None:
    """Generate the pre-visit summary .docx for a clinic visit."""
    try:
        parsed_visit_date = datetime.strptime(visit_date, "%Y-%m-%d").date()
    except ValueError as e:
        raise click.BadParameter(f"--visit-date must be YYYY-MM-DD: {e}") from None

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    charts_dir = out_root / "charts" / stamp
    docx_path = out_root / f"bennett_visit_{parsed_visit_date.isoformat()}_{stamp}.docx"

    click.echo(f"Loading log: {log_path}")
    log = load_log(log_path)
    click.echo(f"  monitored days: {len(log.daily)}")
    click.echo(f"  excluded dates: {len(log.excluded_dates)}")
    click.echo(f"  mismatches:     {len(log.mismatches)}")

    click.echo("Rendering charts...")
    chart_paths = render_all_charts(
        log,
        output_dir=charts_dir,
        lookback_days=lookback,
        rolling_lookback_days=rolling_lookback,
    )

    click.echo("Running pre/post analyses...")
    analyses = analyze_recent_changes(log, k=2)
    baseline = current_baseline(log)

    click.echo("Building document...")
    inputs = ReportInputs(
        log=log,
        chart_paths=chart_paths,
        analyses=analyses,
        baseline=baseline,
        visit_date=parsed_visit_date,
        lookback_days=lookback,
    )
    build_report(inputs, output_path=docx_path)

    pdf_path: Path | None = None
    if pdf:
        click.echo("Converting to PDF...")
        try:
            pdf_path = convert_to_pdf(docx_path)
        except (FileNotFoundError, RuntimeError) as e:
            raise click.ClickException(str(e)) from None

    click.echo("")
    click.echo(f"Document: {docx_path}")
    if pdf_path is not None:
        click.echo(f"PDF:      {pdf_path}")
    click.echo(f"Charts:   {charts_dir}")


@cli.command("inspect-mismatches")
@click.option(
    "--log",
    "log_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the seizure log .xlsx.",
)
@click.option(
    "--top",
    default=20,
    type=int,
    show_default=True,
    help="Number of largest-|diff| mismatch days to dump.",
)
@click.option(
    "--output",
    "output_dir",
    default="output",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory to write the CSV into. Created if it doesn't exist.",
)
def inspect_mismatches(log_path: str, top: int, output_dir: str) -> None:
    """Dump the N worst Cluster Detail / Daily Total mismatches to a local CSV.

    The CSV is written under ``output_dir`` (gitignored) so seizure counts are
    never echoed to the terminal — open the file in Excel to audit.
    """
    log = load_log(log_path)
    mismatches = log.mismatches.copy()
    total_days = len(log.daily)

    click.echo(
        f"Mismatches: {len(mismatches)} of {total_days} monitored days "
        f"({100 * len(mismatches) / total_days:.1f}%)"
    )
    if mismatches.empty:
        click.echo("No mismatches to dump.")
        return

    mismatches["abs_diff"] = mismatches["diff"].abs()
    top_n = mismatches.sort_values("abs_diff", ascending=False).head(top)
    top_n = top_n.rename(
        columns={
            "count": "cluster_detail_sum",
            "daily_total_recorded": "all_data_daily_total",
        }
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"mismatches_top{top}_{stamp}.csv"
    top_n.to_csv(out_path)

    click.echo(
        f"Worst |diff|: {int(top_n['abs_diff'].max())} seizures.\n"
        f"Wrote top {len(top_n)} rows to: {out_path}"
    )


@cli.command("dump-notes")
@click.option(
    "--log",
    "log_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the seizure log .xlsx.",
)
@click.option(
    "--lookback",
    default=90,
    type=int,
    show_default=True,
    help="Lookback days, anchored at the latest date in the log.",
)
@click.option(
    "--output",
    "output_dir",
    default="output",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory to write the CSV into. Created if it doesn't exist.",
)
def dump_notes(log_path: str, lookback: int, output_dir: str) -> None:
    """Dump verbatim Notes/Diet cells in the lookback window to a local CSV.

    Includes every non-blank cell from ``All Data.Notes``, ``All Data.Diet``,
    and ``Cluster Detail.Notes`` on logged days only. No keyword filtering,
    no paraphrase — open the CSV in Excel and skim.
    """
    notes_df = extract_notes(log_path, lookback_days=lookback)
    log = load_log(log_path)
    end = log.latest_date
    start = end - pd.Timedelta(days=lookback - 1)

    click.echo(
        f"Window: {start.date().isoformat()} → {end.date().isoformat()} "
        f"({lookback} days)"
    )
    click.echo(f"Notes extracted: {len(notes_df)} entries")
    if not notes_df.empty:
        for source, count in notes_df["Source"].value_counts().sort_index().items():
            click.echo(f"  {source}: {count}")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"bennett_notes_{end.date().isoformat()}_{stamp}.csv"
    notes_df.to_csv(out_path, index=False)
    click.echo(f"Wrote: {out_path}")
