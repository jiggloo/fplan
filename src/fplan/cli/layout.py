"""L3 — placement / layout."""

from __future__ import annotations

import typer

from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented, not_migrated

group = typer.Typer(help="L3 — placement / layout.", no_args_is_help=True)


@group.command()
def place(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Run a placement method over the production plan and map. (pending migration)"""
    not_migrated(ctx)


@group.command()
def post(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Post-process the placement into the L4 (execution) input. (not implemented)"""
    not_implemented(ctx)


@group.command()
def viz(ctx: typer.Context) -> None:
    """Render the placement / flow partition (includes the map). (pending migration)"""
    not_migrated(ctx)
