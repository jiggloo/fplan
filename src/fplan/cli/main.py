"""Root fplan CLI: wires the stage groups together and the top-level commands.

Bare ``fplan`` (no subcommand) prints the working directory it will operate
from — listing files is what the shell is for. Each pipeline stage is a command
group (L1 ``tech-order`` → L4 ``execution``); cross-cutting commands (``init``,
``full-run``) live at the top level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from fplan import __version__
from fplan.cli import execution, layout, rates, tech_order
from fplan.cli import inspect as inspect_group
from fplan.cli import map as map_group
from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented

app = typer.Typer(
    help="fplan — Factorio production and placement planner.",
    add_completion=True,
    # Keep the no-traceback / no-leak posture intentional and independent of
    # Typer's evolving defaults: never render local variables (file paths,
    # resolved data dirs, env-derived strings) into a displayed traceback once
    # later stages add real subprocess / file work.
    pretty_exceptions_show_locals=False,
)

app.add_typer(tech_order.group, name="tech-order")
app.add_typer(rates.group, name="rates")
app.add_typer(map_group.group, name="map")
app.add_typer(layout.group, name="layout")
app.add_typer(inspect_group.group, name="inspect")
app.add_typer(execution.group, name="execution")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fplan {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """fplan — Factorio production and placement planner."""
    if ctx.invoked_subcommand is None:
        typer.echo(f"fplan working directory: {Path.cwd()}")


@app.command("full-run")
def full_run(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Run the whole L1 → L4 chain (gated on cross-stage discovery)."""
    not_implemented(ctx)


@app.command()
def init(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Detect and create an initial config file."""
    not_implemented(ctx)
