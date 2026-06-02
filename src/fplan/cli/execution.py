"""L4 — execution (action-step generation)."""

from __future__ import annotations

import typer

from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented

group = typer.Typer(
    help="L4 — execution (action-step generation).", no_args_is_help=True
)


@group.command()
def generate(dry_run: DryRun = False) -> None:
    """Generate the action steps for the Factorio-TAS-Generator.

    ``generate`` is a placeholder name; the verb is settled when L4 is designed.
    """
    not_implemented("execution generate")


@group.command()
def viz() -> None:
    """Render the execution steps / player trajectory."""
    not_implemented("execution viz")
