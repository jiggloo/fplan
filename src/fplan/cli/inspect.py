"""Browse the game model — techs, items, recipes (text query only)."""

from __future__ import annotations

import typer

from fplan.cli._stub import not_migrated

group = typer.Typer(
    help="Browse the game model (techs, items, recipes).", no_args_is_help=True
)


@group.command()
def tech(ctx: typer.Context) -> None:
    """Browse technologies."""
    not_migrated(ctx)


@group.command()
def item(ctx: typer.Context) -> None:
    """Browse items."""
    not_migrated(ctx)


@group.command()
def recipe(ctx: typer.Context) -> None:
    """Browse recipes."""
    not_migrated(ctx)
