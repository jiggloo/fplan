"""L2 — production rates."""

from __future__ import annotations

import typer

from fplan.cli._options import DryRun
from fplan.cli._stub import not_migrated

group = typer.Typer(help="L2 — production rates.", no_args_is_help=True)


@group.command()
def solve(dry_run: DryRun = False) -> None:
    """Solve the production-phase LP. --dry-run shows the instance without solving."""
    not_migrated("rates solve")


@group.command()
def post(dry_run: DryRun = False) -> None:
    """Post-process the solved rates into the input for the layout stage."""
    not_migrated("rates post")


@group.command()
def viz() -> None:
    """Render the capacity-saturation heatmap."""
    not_migrated("rates viz")
