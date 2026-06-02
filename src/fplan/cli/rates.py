"""L2 — production rates (run-aware SCIP solve)."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Annotated

import typer
import yaml

from fplan.cli._options import DryRun
from fplan.cli._stub import not_migrated

group = typer.Typer(help="L2 — production rates.", no_args_is_help=True)

RunArg = Annotated[str, typer.Argument(help="Run name (under runs/) to solve.")]
ModeOpt = Annotated[
    str,
    typer.Option(
        "--mode", help="Planning mode: lower-bound | experimental | trapezoidal."
    ),
]
SeedOpt = Annotated[
    int | None,
    typer.Option(
        "--seed", help="SCIP randomization seed (random + printed if omitted)."
    ),
]
L2ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--l2-config", help="L2 tuning config to deep-merge over the defaults."
    ),
]
TimeLimitOpt = Annotated[
    float | None, typer.Option("--time-limit-s", help="Wall-clock seconds for SCIP.")
]
GapLimitOpt = Annotated[
    float | None,
    typer.Option("--gap-limit", help="Relative gap to accept (e.g. 0.05)."),
]
StallNodesOpt = Annotated[
    int | None,
    typer.Option("--stall-nodes", help="Stop after N nodes without improvement."),
]
NodeLimitOpt = Annotated[
    int | None, typer.Option("--node-limit", help="Hard cap on total B&B nodes.")
]
MaxAreaOpt = Annotated[
    float | None,
    typer.Option(
        "--max-area-fraction", help="Override the config's max building area fraction."
    ),
]
NoDeploymentOpt = Annotated[
    bool,
    typer.Option(
        "--no-deployment", help="Disable only the infrastructure-item reservation."
    ),
]
NoPlayerTimeOpt = Annotated[
    bool,
    typer.Option(
        "--no-player-time", help="Disable the per-step player-time constraint."
    ),
]
ForceOpt = Annotated[
    bool,
    typer.Option("--force", help="Overwrite an existing rates.yaml without prompting."),
]


def _input_path(manifest, label: str) -> Path | None:
    ref = manifest.inputs.get(label)
    if not isinstance(ref, dict) or not ref.get("path"):
        return None
    return Path(ref["path"])


@group.command()
def solve(
    ctx: typer.Context,
    run: RunArg,
    mode: ModeOpt = "experimental",
    seed: SeedOpt = None,
    l2_config: L2ConfigOpt = None,
    time_limit_s: TimeLimitOpt = None,
    gap_limit: GapLimitOpt = None,
    stall_nodes: StallNodesOpt = None,
    node_limit: NodeLimitOpt = None,
    max_area_fraction: MaxAreaOpt = None,
    no_deployment: NoDeploymentOpt = False,
    no_player_time: NoPlayerTimeOpt = False,
    force: ForceOpt = False,
    dry_run: DryRun = False,
) -> None:
    """Solve a run's production-rate LP, writing runs/<run>/rates.yaml.

    Reads the run manifest's scenario / tech-order / map (resolved relative to
    the current directory), builds the L2 instance, solves with SCIP, and
    records the L2 settings + outcome back into the manifest.
    """
    from fplan import refs
    from fplan import run as run_mod
    from fplan.cli import main as cli_main
    from fplan.l2 import config as l2_config_mod
    from fplan.l2 import instance as l2_instance

    state: cli_main.CLIState = ctx.obj

    run_dir = run_mod.run_dir(run)
    if not run_mod.manifest_path(run_dir).exists():
        typer.echo(f"error: run {run!r} not found at {run_dir}", err=True)
        raise typer.Exit(code=1)
    try:
        manifest = run_mod.load(run_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    scenario_path = _input_path(manifest, "scenario")
    l1_path = _input_path(manifest, "tech-order")
    map_path = _input_path(manifest, "map")
    if scenario_path is None or l1_path is None:
        typer.echo(
            f"error: run {run!r} manifest lacks a scenario or tech-order binding",
            err=True,
        )
        raise typer.Exit(code=1)
    for label, p in (("scenario", scenario_path), ("tech-order", l1_path)):
        if not p.exists():
            typer.echo(f"error: {label} not found: {p}", err=True)
            raise typer.Exit(code=1)
    if map_path is not None and not map_path.exists():
        typer.echo(f"error: map not found: {map_path}", err=True)
        raise typer.Exit(code=1)

    if mode not in l2_instance.MODES:
        choices = ", ".join(l2_instance.MODES)
        typer.echo(f"error: unknown mode {mode!r}; choose from {choices}.", err=True)
        raise typer.Exit(code=2)
    if l2_config is not None and not l2_config.exists():
        typer.echo(f"error: L2 config not found: {l2_config}", err=True)
        raise typer.Exit(code=1)

    # Load the model + tuning config + scenario, then build the instance.
    from fplan import scenario as scenario_mod

    model = cli_main.load_model_or_exit(state.config_file)
    try:
        cfg = l2_config_mod.load_config(l2_config)
        scenario_obj = scenario_mod.load(scenario_path)
        inst = l2_instance.build_instance(
            scenario_obj,
            l1_path,
            model,
            mode=mode,
            map_probe_path=map_path,
            deployment_enabled=not no_deployment,
            player_time_enabled=not no_player_time,
            max_area_fraction=max_area_fraction,
            l2_config=cfg,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    for w in inst.warnings:
        typer.echo(f"  ⚠ {w}")

    out_path = run_dir / "rates.yaml"
    if dry_run:
        l2_instance._print_summary(inst, model)
        typer.echo(f"\n(dry run; would solve and write {out_path})")
        return

    if not force:
        cli_main.confirm_overwrite_or_exit(out_path)

    if seed is None:
        seed = random.randint(1, 2**31 - 1)
    typer.echo(f"SCIP seed: {seed}  (re-pass --seed {seed} to reproduce)")

    from fplan.l2 import solve as l2_solve

    sol, _m, _handles = l2_solve.solve(
        inst,
        model,
        time_limit_s=time_limit_s,
        gap_limit=gap_limit,
        stall_nodes=stall_nodes,
        node_limit=node_limit,
        seed=seed,
    )
    sol.seed = seed

    if sol.objective is None:
        typer.echo(f"✗ no feasible incumbent (status: {sol.status})")
        raise typer.Exit(code=1)

    try:
        l2_solve.write_solution(inst, sol, model, out_path)
    except OSError as exc:
        typer.echo(f"error: could not write {out_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Grow the manifest with this stage's settings + outcome.
    config_ref = refs.file_ref(l2_config) if l2_config is not None else "default"
    manifest.extra["l2"] = {
        "mode": mode,
        "seed": seed,
        "objective_s": float(sol.objective),
        "status": sol.status,
        "solve_time_s": float(sol.solve_time_s),
        "config": config_ref,
    }
    run_mod.save(run_dir, manifest)

    typer.echo(f"✓ t_FINAL = {sol.objective:.1f}s (status: {sol.status})\n→ {out_path}")


@group.command()
def post(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Post-process the solved rates into the input for the layout stage."""
    not_migrated(ctx)


@group.command()
def viz(ctx: typer.Context) -> None:
    """Render the capacity-saturation heatmap."""
    not_migrated(ctx)
