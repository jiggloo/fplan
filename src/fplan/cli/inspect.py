"""Browse the game model — techs, items, recipes (text query only)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated

import typer

from fplan.model import GameModel, Item, Recipe, Technology, format_research_trigger

group = typer.Typer(
    help=(
        "Browse the game model (techs, items, recipes). Reads your Factorio "
        "installation's Lua data files to build the model (does not launch the "
        "game)."
    ),
    no_args_is_help=True,
)

NameArg = Annotated[str | None, typer.Argument(help="Name to show in detail.")]
FilterOpt = Annotated[
    str | None,
    typer.Option(
        "--filter",
        help="Show full detail for every entry whose name contains this substring.",
    ),
]


def _row(label: str, value: str) -> str:
    """One aligned ``label: value`` detail line (shared by all three views)."""
    return f"  {label + ':':<15}{value}"


def _query(
    *,
    entities: Mapping[str, object],
    detail: Callable[[str], str],
    name: str | None,
    pattern: str | None,
    singular: str,
    plural: str,
) -> None:
    """Run the shared inspect dispatch: detail / list-names / filter-to-detail.

    Three modes, identical across ``tech``/``item``/``recipe``:

    - ``name`` given      → one entry's detail (unknown name is fatal, exit 1).
    - no name, no filter  → every name, one per line (a discovery index).
    - ``--filter`` given  → full detail for every name containing the substring,
      so a search and an inspect are a single call.
    """
    if name is not None:
        if name not in entities:
            typer.echo(f"error: unknown {singular}: {name}", err=True)
            raise typer.Exit(code=1)
        typer.echo(detail(name))
        return

    names = sorted(entities)
    if pattern:
        names = [n for n in names if pattern in n]
    if not names:
        typer.echo(f"no {plural} match {pattern!r}")
        return
    if pattern:
        typer.echo("\n\n".join(detail(n) for n in names))
    else:
        for n in names:
            typer.echo(n)


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
            _row("cost", cost),
            _row("prerequisites", ", ".join(t.prerequisites) or "(none)"),
            _row("unlocks", ", ".join(t.unlocks_recipes) or "(no recipes)"),
            _row("required by", ", ".join(dependents) or "(nothing)"),
        ]
    )


def _item_detail(it: Item, model: GameModel) -> str:
    produced = sorted(r.name for r in model.recipes_producing(it.name))
    consumed = sorted(r.name for r in model.recipes_consuming(it.name))
    techs = sorted(model.unlocking_techs_for(it.name))
    lines = [f"{it.name}{'  *(fluid)*' if it.kind == 'fluid' else ''}"]
    if it.stack_size is not None:
        lines.append(_row("stack size", str(it.stack_size)))
    if it.fuel_value_j:
        lines.append(_row("fuel value", f"{it.fuel_value_j:g} J"))
    lines.append(_row("produced by", ", ".join(produced) or "(nothing)"))
    lines.append(_row("consumed by", ", ".join(consumed) or "(nothing)"))
    lines.append(_row("unlocked by", ", ".join(techs) or "(available at start)"))
    return "\n".join(lines)


def _stacks(stacks: list) -> str:
    return ", ".join(f"{s.name}x{s.amount:g}" for s in stacks) or "(none)"


def _recipe_detail(r: Recipe, model: GameModel) -> str:
    made_in = sorted(b.name for b in model.buildings_for(r))
    if r.enabled_at_start:
        unlock = "(available at start)"
    else:
        unlock = ", ".join(r.unlocking_techs) or "(no tech)"
    return "\n".join(
        [
            f"{r.name}{'' if r.kind == 'crafting' else f'  *({r.kind})*'}",
            _row("category", r.category),
            _row("time", f"{r.time_seconds:g}s"),
            _row("ingredients", _stacks(r.ingredients)),
            _row("outputs", _stacks(r.outputs)),
            _row("made in", ", ".join(made_in) or "(nothing)"),
            _row("unlocked by", unlock),
        ]
    )


def _model(ctx: typer.Context) -> GameModel:
    from fplan.cli import main as cli_main

    state: cli_main.CLIState = ctx.obj
    return cli_main.load_model_or_exit(state.config_file)


@group.command()
def tech(ctx: typer.Context, name: NameArg = None, pattern: FilterOpt = None) -> None:
    """Show a technology's detail, list technologies, or --filter to details."""
    techs = _model(ctx).technologies
    _query(
        entities=techs,
        detail=lambda n: _tech_detail(techs[n], techs),
        name=name,
        pattern=pattern,
        singular="technology",
        plural="technologies",
    )


@group.command()
def item(ctx: typer.Context, name: NameArg = None, pattern: FilterOpt = None) -> None:
    """Show an item's detail, list items, or --filter to details."""
    model = _model(ctx)
    _query(
        entities=model.items,
        detail=lambda n: _item_detail(model.items[n], model),
        name=name,
        pattern=pattern,
        singular="item",
        plural="items",
    )


@group.command()
def recipe(ctx: typer.Context, name: NameArg = None, pattern: FilterOpt = None) -> None:
    """Show a recipe's detail, list recipes, or --filter to details."""
    model = _model(ctx)
    _query(
        entities=model.recipes,
        detail=lambda n: _recipe_detail(model.recipes[n], model),
        name=name,
        pattern=pattern,
        singular="recipe",
        plural="recipes",
    )
