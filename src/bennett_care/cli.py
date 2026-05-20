"""CLI for bennett-care.

Implemented subcommands:
    inspect-mismatches  Dump worst Cluster Detail vs. Daily Total mismatches.

Stub subcommands (to be filled in as modules land):
    visit-prep          Generate the pre-visit summary document.
"""

from __future__ import annotations

from pathlib import Path

import click
import pandas as pd

from .ingest import load_log


@click.group()
def cli() -> None:
    """bennett-care: local seizure-log analysis."""


@cli.command("visit-prep")
@click.option("--log", "log_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--visit-date", required=True)
@click.option("--lookback", default=90, type=int, show_default=True)
@click.option("--output", "output_dir", required=True, type=click.Path(file_okay=False))
def visit_prep(log_path: str, visit_date: str, lookback: int, output_dir: str) -> None:
    """Generate a pre-visit summary document. (Stub — implementation pending.)"""
    click.echo("visit-prep: not yet implemented")


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
