"""Root fplan CLI: wires the stage groups together and the top-level commands.

Bare ``fplan`` (no subcommand) prints the working directory it will operate
from — listing files is what the shell is for. Each pipeline stage is a command
group (L1 ``tech-order`` → L4 ``execution``); cross-cutting commands (``init``,
``full-run``) live at the top level.
"""

from __future__ import annotations

from pathlib import Path

import typer

from fplan.cli import execution, layout, rates, tech_order
from fplan.cli import inspect as inspect_group
from fplan.cli import map as map_group
from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented

app = typer.Typer(
    help="fplan — Factorio production and placement planner.",
    add_completion=True,
)

app.add_typer(tech_order.group, name="tech-order")
app.add_typer(rates.group, name="rates")
app.add_typer(map_group.group, name="map")
app.add_typer(layout.group, name="layout")
app.add_typer(inspect_group.group, name="inspect")
app.add_typer(execution.group, name="execution")


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """fplan — Factorio production and placement planner."""
    if ctx.invoked_subcommand is None:
        typer.echo(f"fplan working directory: {Path.cwd()}")


@app.command("full-run")
def full_run(dry_run: DryRun = False) -> None:
    """Run the whole L1 → L4 chain (gated on cross-stage discovery)."""
    not_implemented("full-run")


@app.command()
def init(dry_run: DryRun = False) -> None:
    """Detect and create an initial config file."""
    not_implemented("init")
