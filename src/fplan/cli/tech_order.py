"""L1 — technology research ordering (build / verify)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from fplan import goals, refs
from fplan import tech_order as ordering
from fplan.cli._log import echo_settings
from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented

group = typer.Typer(
    help=(
        "L1 — technology research ordering. Reads your Factorio installation's "
        "Lua data files to build the game model (does not launch the game)."
    ),
    no_args_is_help=True,
)

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
        "--scenario",
        help="Goal source override (default: the order's referenced scenario).",
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
    echo_settings([("method", method, method == "forward")])
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
    # Record where this order came from as a reference (name + path + hash),
    # not the goal content — the order is L1's output, the scenario its input.
    scenario_ref = refs.file_ref(scenario, name=goal.name or None)
    payload = ordering.build_payload(result, method, scenario_ref)
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

    # Resolve the goal. A tech-order carries no goal content — only a
    # `scenario:` reference recording the input it was built from — so the goal
    # comes from that referenced scenario, or from an explicit --scenario
    # override. A malformed/unreadable scenario must surface as a clean error,
    # not a traceback (matching build's goals.load handling).
    drift_warning: str | None = None
    try:
        if scenario is not None:
            if not scenario.exists():
                typer.echo(f"error: scenario file not found: {scenario}", err=True)
                raise typer.Exit(code=1)
            goal = goals.load(scenario)
            goal_src = f"--scenario {scenario}"
        elif isinstance(doc.get("scenario"), dict) and doc["scenario"].get("path"):
            ref = doc["scenario"]
            ref_path = Path(ref["path"])
            if not ref_path.exists():
                typer.echo(
                    f"error: referenced scenario not found: {ref_path}; "
                    "pass --scenario",
                    err=True,
                )
                raise typer.Exit(code=1)
            goal = goals.load(ref_path)
            goal_src = f"referenced scenario {ref_path}"
            if refs.is_current(ref) is False:
                drift_warning = (
                    f"referenced scenario {ref_path} has changed since this order "
                    "was built (hash mismatch) — verifying against current content"
                )
        else:
            typer.echo(
                f"error: {order} has no scenario reference; pass --scenario", err=True
            )
            raise typer.Exit(code=1)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    model = cli_main.load_model_or_exit(state.config_file)
    res = ordering.verify_order(model.technologies, model, layers, goal)

    typer.echo(f"Verifying {order} against {goal_src}")
    if drift_warning:
        typer.echo(f"  ⚠ WARNING: {drift_warning}")
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
    """Render the tech order (layers / DAG). (not implemented)"""
    not_implemented(ctx)
