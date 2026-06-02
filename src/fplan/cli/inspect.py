"""Browse the game model — techs, items, recipes (text query only)."""

from __future__ import annotations

import typer

from fplan.cli._stub import not_migrated

group = typer.Typer(
    help="Browse the game model (techs, items, recipes).", no_args_is_help=True
)


@group.command()
def tech() -> None:
    """Browse technologies."""
    not_migrated("inspect tech")


@group.command()
def item() -> None:
    """Browse items."""
    not_migrated("inspect item")


@group.command()
def recipe() -> None:
    """Browse recipes."""
    not_migrated("inspect recipe")
