"""L1 — technology research ordering."""

from __future__ import annotations

import typer

from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented, not_migrated

group = typer.Typer(help="L1 — technology research ordering.", no_args_is_help=True)


@group.command()
def build(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Generate a tech research order from a scenario."""
    not_migrated(ctx)


@group.command()
def verify(ctx: typer.Context) -> None:
    """Verify a tech order satisfies the game-model requirements."""
    not_migrated(ctx)


@group.command()
def viz(ctx: typer.Context) -> None:
    """Render the tech order (layers / DAG)."""
    not_implemented(ctx)
