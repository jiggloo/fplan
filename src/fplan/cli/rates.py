"""L2 — production rates (run-aware SCIP solve)."""

from __future__ import annotations

import random
import shutil
from pathlib import Path
from typing import Annotated

import typer
import yaml

from fplan.cli._options import DryRun
from fplan.cli._stub import not_migrated

group = typer.Typer(help="L2 — production rates.", no_args_is_help=True)

# Search candidates live in their own subdir of the run so a search never
# collides with the promoted rates.yaml until the user explicitly promotes.
SEARCH_DIRNAME = "rates-search"
SEARCH_SUMMARY = "summary.yaml"
RATES_NAME = "rates.yaml"

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
SeedsOpt = Annotated[
    str | None,
    typer.Option(
        "--seeds",
        help="Multi-seed search: N (run N random seeds) or \\[a,b,c] (those "
        "seeds). Mutually exclusive with --seed/--out.",
    ),
]
OutOpt = Annotated[
    Path | None,
    typer.Option(
        "--out",
        help="Single-solve export path instead of runs/<run>/rates.yaml "
        "(manifest is NOT updated). Mutually exclusive with --seeds.",
    ),
]
JobsOpt = Annotated[
    int | None,
    typer.Option(
        "--jobs",
        "-j",
        help="Parallel worker processes for --seeds (default: up to CPU count). "
        "1 = serial. Each seed solve is heavy — cap this if memory-bound.",
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
QuietSolverOpt = Annotated[
    bool,
    typer.Option(
        "--quiet-solver",
        help="Slim the per-seed logs (parallel search only): omit SCIP's live "
        "progress table. By default each seed's log captures full progress.",
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


def _seed_spec(spec: str) -> int | list[int]:
    """Parse a ``--seeds`` value into either a count (bare int) or an explicit
    list (bracketed). Raises ``ValueError`` with a clean message on bad syntax.

    ``"5"`` → ``5`` (run 5 random seeds); ``"[1,2,3]"`` → ``[1, 2, 3]`` (exactly
    those, de-duplicated preserving order).
    """
    s = spec.strip()
    if s.startswith("["):
        if not s.endswith("]"):
            raise ValueError(f"malformed seed list {spec!r}: missing closing ']'")
        body = s[1:-1].strip()
        if not body:
            raise ValueError(f"empty seed list {spec!r}")
        seen: dict[int, None] = {}
        for tok in body.split(","):
            tok = tok.strip()
            try:
                seen[int(tok)] = None
            except ValueError:
                raise ValueError(
                    f"bad seed {tok!r} in {spec!r}: seeds must be integers"
                ) from None
        return list(seen)
    try:
        n = int(s)
    except ValueError:
        raise ValueError(
            f"bad --seeds {spec!r}: use N (a count) or [a,b,c] (explicit seeds)"
        ) from None
    if n < 1:
        raise ValueError(f"--seeds count must be ≥ 1, got {n}")
    return n


def _random_seeds(n: int) -> list[int]:
    """``n`` distinct random SCIP seeds in ``[1, 2**31)``."""
    return random.sample(range(1, 2**31 - 1), n)


@group.command()
def solve(
    ctx: typer.Context,
    run: RunArg,
    mode: ModeOpt = "experimental",
    seed: SeedOpt = None,
    seeds: SeedsOpt = None,
    l2_config: L2ConfigOpt = None,
    time_limit_s: TimeLimitOpt = None,
    gap_limit: GapLimitOpt = None,
    stall_nodes: StallNodesOpt = None,
    node_limit: NodeLimitOpt = None,
    max_area_fraction: MaxAreaOpt = None,
    no_deployment: NoDeploymentOpt = False,
    no_player_time: NoPlayerTimeOpt = False,
    out: OutOpt = None,
    jobs: JobsOpt = None,
    quiet_solver: QuietSolverOpt = False,
    force: ForceOpt = False,
    dry_run: DryRun = False,
) -> None:
    """Solve a run's production-rate plan (MINLP) with SCIP, writing rates.yaml.

    Reads the run manifest's scenario / tech-order / map (resolved relative to
    the current directory), builds the L2 instance, solves the nonconvex MINLP
    with SCIP, and records the L2 settings + outcome back into the manifest.

    With ``--seeds`` it runs a multi-seed search instead: every seed is solved,
    each candidate is written under ``runs/<run>/rates-search/`` (never touching
    the promoted ``rates.yaml``), the seeds are ranked by t_FINAL, and the best
    is promoted to ``rates.yaml`` after a prompt (``--force`` to skip).
    """
    from fplan import refs
    from fplan import run as run_mod
    from fplan.cli import main as cli_main
    from fplan.l2 import config as l2_config_mod
    from fplan.l2 import instance as l2_instance

    state: cli_main.CLIState = ctx.obj

    # Cheap argument validation first: surface usage errors (exit 2) before any
    # model load or solve.
    if seeds is not None and seed is not None:
        typer.echo("error: --seed and --seeds are mutually exclusive.", err=True)
        raise typer.Exit(code=2)
    if seeds is not None and out is not None:
        typer.echo("error: --out cannot be combined with --seeds.", err=True)
        raise typer.Exit(code=2)
    chosen_seeds: list[int] | None = None
    if seeds is not None:
        try:
            spec = _seed_spec(seeds)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        chosen_seeds = _random_seeds(spec) if isinstance(spec, int) else spec

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

    if dry_run:
        l2_instance._print_summary(inst, model)
        if chosen_seeds is not None:
            search_dir = run_dir / SEARCH_DIRNAME
            typer.echo(
                f"\n(dry run; would solve {len(chosen_seeds)} seed(s) and write "
                f"candidates under {search_dir})"
            )
        else:
            dest = out if out is not None else run_dir / RATES_NAME
            typer.echo(f"\n(dry run; would solve and write {dest})")
        return

    config_ref = refs.file_ref(l2_config) if l2_config is not None else "default"
    solver_kwargs = {
        "time_limit_s": time_limit_s,
        "gap_limit": gap_limit,
        "stall_nodes": stall_nodes,
        "node_limit": node_limit,
    }

    if chosen_seeds is not None:
        _run_search(
            run_dir=run_dir,
            manifest=manifest,
            inst=inst,
            model=model,
            mode=mode,
            seeds=chosen_seeds,
            solver_kwargs=solver_kwargs,
            config_ref=config_ref,
            jobs=jobs,
            quiet_solver=quiet_solver,
            force=force,
            run_mod=run_mod,
            cli_main=cli_main,
        )
        return

    from fplan.l2 import solve as l2_solve

    _run_single(
        run_dir=run_dir,
        manifest=manifest,
        inst=inst,
        model=model,
        mode=mode,
        seed=seed,
        out=out,
        solver_kwargs=solver_kwargs,
        config_ref=config_ref,
        force=force,
        run_mod=run_mod,
        l2_solve=l2_solve,
        cli_main=cli_main,
    )


def _run_single(
    *,
    run_dir,
    manifest,
    inst,
    model,
    mode,
    seed,
    out,
    solver_kwargs,
    config_ref,
    force,
    run_mod,
    l2_solve,
    cli_main,
) -> None:
    """Single-seed solve. Writes the canonical rates.yaml (and grows the
    manifest), or — when ``out`` is given — a pure export to that path that
    leaves the manifest untouched."""
    out_path = out if out is not None else run_dir / RATES_NAME
    if not force:
        cli_main.confirm_overwrite_or_exit(out_path)

    if seed is None:
        seed = random.randint(1, 2**31 - 1)
    typer.echo(f"SCIP seed: {seed}  (re-pass --seed {seed} to reproduce)")

    try:
        sol, _m, _handles = l2_solve.solve(inst, model, **solver_kwargs, seed=seed)
    except Exception as exc:  # SCIP-side failure → clean error, not a traceback
        typer.echo(f"error: solve failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    sol.seed = seed

    if sol.objective is None:
        typer.echo(f"✗ no feasible incumbent (status: {sol.status})")
        raise typer.Exit(code=1)

    try:
        l2_solve.write_solution(inst, sol, model, out_path)
    except OSError as exc:
        typer.echo(f"error: could not write {out_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if out is None:
        # Grow the manifest with this stage's settings + outcome. An --out
        # export is deliberately not recorded — the l2: block only ever points
        # at the promoted rates.yaml inside the run.
        manifest.extra["l2"] = {
            "mode": mode,
            "seed": seed,
            "objective_s": float(sol.objective),
            "status": sol.status,
            "solve_time_s": float(sol.solve_time_s),
            "config": config_ref,
        }
        run_mod.save(run_dir, manifest)
    else:
        typer.echo("(--out export; manifest not updated)")

    typer.echo(f"✓ t_FINAL = {sol.objective:.1f}s (status: {sol.status})\n→ {out_path}")


def _run_search(
    *,
    run_dir,
    manifest,
    inst,
    model,
    mode,
    seeds,
    solver_kwargs,
    config_ref,
    jobs,
    quiet_solver,
    force,
    run_mod,
    cli_main,
) -> None:
    """Solve every seed (serial or parallel), store candidates, rank by t_FINAL,
    and (after a prompt) promote the best to rates.yaml.

    The instance is solver-neutral and reused across seeds — only SCIP's
    randomseedshift varies — so the build cost is paid once and shipped to each
    worker. The fan-out itself lives in :mod:`fplan.l2.search`; this stays the
    CLI-facing ranking / summary / promotion shell.
    """
    from fplan.l2 import search as l2_search

    search_dir = run_dir / SEARCH_DIRNAME
    search_dir.mkdir(parents=True, exist_ok=True)

    n_jobs = l2_search.resolve_jobs(jobs, len(seeds))
    parallel = n_jobs > 1
    # Parallel seeds each get full SCIP progress in their own log (so they're
    # monitorable with tail -f) — interleaving the console is the whole problem.
    # --quiet-solver slims those logs. Serial stays quiet on the console as before.
    solver_kwargs = {**solver_kwargs, "verbose": parallel and not quiet_solver}

    if parallel:
        typer.echo(
            f"Searching {len(seeds)} seed(s), up to {n_jobs} in parallel — "
            f"per-seed logs (tail -f to monitor):"
        )
        for sd in seeds:
            typer.echo(f"  seed {sd} → {search_dir / f'seed-{sd}.log'}")
    else:
        typer.echo(f"Searching {len(seeds)} seed(s) serially …")

    candidates: list[dict] = []
    total = len(seeds)
    for res in l2_search.run_search(
        inst,
        model,
        seeds,
        solver_kwargs=solver_kwargs,
        search_dir=search_dir,
        jobs=n_jobs,
    ):
        entry: dict = {
            "seed": res.seed,
            "status": res.status,
            "objective_s": res.objective_s,
            "solve_time_s": res.solve_time_s,
            "file": res.file,
        }
        if parallel:
            entry["log"] = f"seed-{res.seed}.log"
        if res.error is not None:
            entry["error"] = res.error
        candidates.append(entry)

        done = len(candidates)
        if res.error is not None:
            typer.echo(f"  [{done}/{total}] ✗ seed {res.seed} failed: {res.error}")
        elif res.objective_s is None:
            typer.echo(
                f"  [{done}/{total}] ✗ seed {res.seed}: no feasible incumbent "
                f"({res.status})"
            )
        else:
            typer.echo(
                f"  [{done}/{total}] ✓ seed {res.seed}: "
                f"t_FINAL = {res.objective_s:.1f}s ({res.status})"
            )

    # Rank: feasible seeds by t_FINAL then seed (deterministic tie-break);
    # infeasible / errored seeds are never promotable.
    feasible = sorted(
        (c for c in candidates if c["objective_s"] is not None),
        key=lambda c: (c["objective_s"], c["seed"]),
    )
    best = feasible[0] if feasible else None

    summary = {
        "mode": mode,
        "config": config_ref,
        "jobs": n_jobs,
        "solver": dict(solver_kwargs),
        "seeds": list(seeds),
        "best_seed": best["seed"] if best else None,
        # Stored seed-sorted for a stable diff; live output is completion order.
        "candidates": sorted(candidates, key=lambda c: c["seed"]),
    }
    summary_path = search_dir / SEARCH_SUMMARY
    try:
        summary_path.write_text(yaml.safe_dump(summary, sort_keys=False))
    except OSError as exc:
        typer.echo(f"error: could not write {summary_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _print_ranked(candidates, best)

    if best is None:
        typer.echo(f"\n✗ no feasible incumbent across {len(seeds)} seed(s).")
        typer.echo(f"  candidates + summary left in {search_dir}")
        raise typer.Exit(code=1)

    _promote(
        run_dir=run_dir,
        manifest=manifest,
        search_dir=search_dir,
        best=best,
        mode=mode,
        seeds=seeds,
        config_ref=config_ref,
        force=force,
        run_mod=run_mod,
        cli_main=cli_main,
    )


def _print_ranked(candidates: list[dict], best: dict | None) -> None:
    """Print the search candidates, feasible (by t_FINAL) first."""
    ordered = sorted(
        candidates,
        key=lambda c: (
            c["objective_s"] is None,
            c["objective_s"] if c["objective_s"] is not None else 0.0,
            c["seed"],
        ),
    )
    typer.echo("\nSearch results (ranked):")
    for c in ordered:
        obj = f"{c['objective_s']:.1f}s" if c["objective_s"] is not None else "—"
        mark = "  ★ best" if best is not None and c["seed"] == best["seed"] else ""
        typer.echo(f"  seed {c['seed']:>10}   t_FINAL = {obj:>9}   {c['status']}{mark}")


def _promote(
    *,
    run_dir,
    manifest,
    search_dir,
    best,
    mode,
    seeds,
    config_ref,
    force,
    run_mod,
    cli_main,
) -> None:
    """Promote the best candidate to rates.yaml after confirmation.

    ``--force`` skips both prompts. A non-interactive session never silently
    clobbers rates.yaml — it leaves the candidates and explains how to promote.
    """
    out_path = run_dir / RATES_NAME
    best_obj = best["objective_s"]

    if not force:
        if not cli_main._stdin_is_interactive():
            typer.echo(
                f"\nnon-interactive: not promoting. Best is seed {best['seed']} "
                f"(t_FINAL = {best_obj:.1f}s) at {search_dir / best['file']}.\n"
                "Re-run with --force (or in an interactive shell) to promote."
            )
            return
        if not typer.confirm(
            f"\nPromote seed {best['seed']} (t_FINAL = {best_obj:.1f}s) → {out_path}?",
            default=True,
        ):
            typer.echo(f"Not promoted; candidates left in {search_dir}.")
            return
        if out_path.exists() and not typer.confirm(
            f"{out_path} already exists. Overwrite?", default=False
        ):
            typer.echo(f"Not promoted; existing {out_path} kept.")
            return

    cand_path = search_dir / best["file"]
    try:
        shutil.copyfile(cand_path, out_path)
    except OSError as exc:
        typer.echo(f"error: could not write {out_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    manifest.extra["l2"] = {
        "mode": mode,
        "seed": best["seed"],
        "objective_s": best_obj,
        "status": best["status"],
        "solve_time_s": best["solve_time_s"],
        "config": config_ref,
        "search": {
            "seeds": list(seeds),
            "promoted_from": f"{SEARCH_DIRNAME}/{best['file']}",
        },
    }
    run_mod.save(run_dir, manifest)
    typer.echo(
        f"✓ promoted seed {best['seed']} (t_FINAL = {best_obj:.1f}s) → {out_path}"
    )


@group.command()
def post(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Post-process the solved rates into the input for the layout stage."""
    not_migrated(ctx)


@group.command()
def viz(ctx: typer.Context) -> None:
    """Render the capacity-saturation heatmap."""
    not_migrated(ctx)
