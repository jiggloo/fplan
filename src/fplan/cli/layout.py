"""L3 — placement / layout."""

from __future__ import annotations

import typer

from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented, not_migrated

group = typer.Typer(help="L3 — placement / layout.", no_args_is_help=True)


@group.command()
def place(dry_run: DryRun = False) -> None:
    """Run a placement method over the production plan and map."""
    not_migrated("layout place")


@group.command()
def post(dry_run: DryRun = False) -> None:
    """Post-process the placement into the input for the execution stage (L4)."""
    not_implemented("layout post")


@group.command()
def viz() -> None:
    """Render the placement / flow partition (the render includes the map)."""
    not_migrated("layout viz")
