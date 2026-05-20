"""Click entry point. To be implemented once downstream modules exist."""

import click


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
