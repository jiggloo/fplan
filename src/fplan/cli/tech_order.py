"""L1 — technology research ordering (build / verify)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from fplan import goals
from fplan import tech_order as ordering
from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented

group = typer.Typer(help="L1 — technology research ordering.", no_args_is_help=True)

ScenarioArg = Annotated[
    Path, typer.Argument(help="Scenario YAML describing the goal to plan for.")
]
OutOpt = Annotated[
    Path, typer.Option("--out", help="Tech-order YAML to write. Required.")
]
MethodOpt = Annotated[
    str,
    typer.Option("--method", help="Ordering method: forward | from-goal | balanced."),
]
OrderArg = Annotated[Path, typer.Argument(help="Tech-order YAML to verify.")]
ScenarioOpt = Annotated[
    Path | None,
    typer.Option(
        "--scenario", help="Goal source override (default: the order's embedded goal)."
    ),
]


@group.command()
def build(
    ctx: typer.Context,
    scenario: ScenarioArg,
    out: OutOpt,
    method: MethodOpt = "forward",
    dry_run: DryRun = False,
) -> None:
    """Generate a tech research order from a scenario."""
    from fplan.cli import main as cli_main

    state: cli_main.CLIState = ctx.obj
    if method not in ordering.METHODS:
        choices = ", ".join(sorted(ordering.METHODS))
        typer.echo(
            f"error: unknown method {method!r}; choose from {choices}.", err=True
        )
        raise typer.Exit(code=2)
    if not scenario.exists():
        typer.echo(f"error: scenario file not found: {scenario}", err=True)
        raise typer.Exit(code=1)
    try:
        goal = goals.load(scenario)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    model = cli_main.load_model_or_exit(state.config_file)
    techs = model.technologies
    try:
        required = ordering.required_set(techs, goal, model)
    except KeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    result = ordering.METHODS[method](techs, required, goal)
    typer.echo(ordering.format_layers(result, techs, goal, method))

    if dry_run:
        typer.echo(f"\n(dry run; would write {out})")
        return

    cli_main.confirm_overwrite_or_exit(out)
    payload = ordering.build_payload(result, goal, method)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(payload, sort_keys=False))
    except OSError as exc:
        typer.echo(f"error: could not write {out}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"\n→ {out}")


@group.command()
def verify(ctx: typer.Context, order: OrderArg, scenario: ScenarioOpt = None) -> None:
    """Verify a tech order is a valid research plan for its goal."""
    from fplan.cli import main as cli_main

    state: cli_main.CLIState = ctx.obj
    try:
        doc = yaml.safe_load(order.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        typer.echo(f"error: cannot read {order}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(doc, dict) or not doc.get("layers"):
        typer.echo(f"error: {order} has no 'layers'", err=True)
        raise typer.Exit(code=1)
    layers = [list(layer) for layer in doc["layers"]]

    if scenario is not None:
        if not scenario.exists():
            typer.echo(f"error: scenario file not found: {scenario}", err=True)
            raise typer.Exit(code=1)
        goal = goals.load(scenario)
        goal_src = f"--scenario {scenario}"
    elif doc.get("goal"):
        goal = goals.from_dict(doc["goal"])
        goal_src = f"embedded goal in {order}"
    else:
        typer.echo(f"error: {order} has no embedded 'goal'; pass --scenario", err=True)
        raise typer.Exit(code=1)

    model = cli_main.load_model_or_exit(state.config_file)
    res = ordering.verify_order(model.technologies, model, layers, goal)

    typer.echo(f"Verifying {order} against {goal_src}")
    for line in res.info:
        typer.echo(f"  · {line}")
    for w in res.warnings:
        typer.echo(f"  ⚠ WARNING: {w}")
    for e in res.errors:
        typer.echo(f"  ✗ ERROR: {e}")
    if res.ok:
        typer.echo("✓ VALID" + (" (with warnings)" if res.warnings else ""))
        return
    typer.echo("✗ INVALID")
    raise typer.Exit(code=1)


@group.command()
def viz(ctx: typer.Context) -> None:
    """Render the tech order (layers / DAG)."""
    not_implemented(ctx)
