"""Browse the game model — techs, items, recipes (text query only)."""

from __future__ import annotations

from typing import Annotated

import typer

from fplan.cli._stub import not_migrated
from fplan.model import Technology, format_research_trigger

group = typer.Typer(
    help="Browse the game model (techs, items, recipes).", no_args_is_help=True
)

NameArg = Annotated[
    str | None, typer.Argument(help="Technology name to show in detail.")
]
FilterOpt = Annotated[
    str | None,
    typer.Option("--filter", help="List technologies whose name contains this."),
]


def _tech_detail(t: Technology, technologies: dict[str, Technology]) -> str:
    trigger = format_research_trigger(t.research_trigger)
    if trigger:
        cost = f"trigger: {trigger}"
    elif t.ingredients:
        packs = ", ".join(f"{n}x{c}" for n, c in t.ingredients)
        count = t.count if t.count is not None else "?"
        secs = f", {t.time:g}s each" if t.time else ""
        cost = f"{count} × ({packs}){secs}"
    else:
        cost = "—"
    dependents = sorted(n for n, o in technologies.items() if t.name in o.prerequisites)
    return "\n".join(
        [
            f"{t.name}{'  *(essential)*' if t.essential else ''}",
            f"  cost:          {cost}",
            f"  prerequisites: {', '.join(t.prerequisites) or '(none)'}",
            f"  unlocks:       {', '.join(t.unlocks_recipes) or '(no recipes)'}",
            f"  required by:   {', '.join(dependents) or '(nothing)'}",
        ]
    )


@group.command()
def tech(ctx: typer.Context, name: NameArg = None, pattern: FilterOpt = None) -> None:
    """Show a technology's detail, or list technologies (with --filter)."""
    from fplan.cli import main as cli_main

    state: cli_main.CLIState = ctx.obj
    model = cli_main.load_model_or_exit(state.config_file)
    technologies = model.technologies

    if name is not None:
        t = technologies.get(name)
        if t is None:
            typer.echo(f"error: unknown technology: {name}", err=True)
            raise typer.Exit(code=1)
        typer.echo(_tech_detail(t, technologies))
        return

    names = sorted(technologies)
    if pattern:
        names = [n for n in names if pattern in n]
    if not names:
        typer.echo(f"no technologies match {pattern!r}")
        return
    for n in names:
        typer.echo(n)


@group.command()
def item(ctx: typer.Context) -> None:
    """Browse items. (pending migration)"""
    not_migrated(ctx)


@group.command()
def recipe(ctx: typer.Context) -> None:
    """Browse recipes. (pending migration)"""
    not_migrated(ctx)
