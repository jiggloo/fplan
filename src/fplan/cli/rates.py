"""L2 — production rates (run-aware SCIP solve)."""

from __future__ import annotations

import os
import random
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml

from fplan.cli._log import echo_settings
from fplan.cli._options import DryRun

group = typer.Typer(
    help=(
        "L2 — production rates. Reads your Factorio installation's Lua data "
        "files to build the game model (does not launch the game)."
    ),
    no_args_is_help=True,
)

# Search candidates live in their own subdir of the run so a search never
# collides with the promoted rates.yaml until the user explicitly promotes.
SEARCH_DIRNAME = "rates-search"
SEARCH_SUMMARY = "summary.yaml"
RATES_NAME = "rates.yaml"
# The post-processed L2 output — the provisional L3 input (current operation:
# rate-flattening). Same rates schema as RATES_NAME plus a `post:` diagnostics
# block (which `rates viz` auto-detects to pick the matching view).
POST_NAME = "rates-post.yaml"
# Inclusive upper bound for a random SCIP seed; shared by single-solve and
# search so both draw from the same [1, SEED_MAX] range.
SEED_MAX = 2**31 - 1

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
LpAlgorithmOpt = Annotated[
    str | None,
    typer.Option(
        "--lp-algorithm",
        help="LP method: barrier | simplex. Overrides the config's detected "
        "preference (barrier needs a HiGHS-linked SCIP). Omit to use the config, "
        "or SCIP's default if unset.",
    ),
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
        help="Silence SCIP in the per-seed logs (parallel search only); the logs "
        "then stay near-empty. By default each seed's log captures full progress.",
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
                val = int(tok)
            except ValueError:
                raise ValueError(
                    f"bad seed {tok!r} in {spec!r}: seeds must be integers"
                ) from None
            if not 1 <= val <= SEED_MAX:
                raise ValueError(
                    f"seed {val} out of range in {spec!r}: must be 1..{SEED_MAX}"
                )
            seen[val] = None
        return list(seen)
    try:
        n = int(s)
    except ValueError:
        raise ValueError(
            f"bad --seeds {spec!r}: use N (a count) or [a,b,c] (explicit seeds)"
        ) from None
    if n < 1:
        raise ValueError(f"--seeds count must be ≥ 1, got {n}")
    # Random seeds are drawn distinctly from [1, SEED_MAX]; a count beyond that
    # population would otherwise blow up in random.sample with a raw traceback.
    if n > SEED_MAX:
        raise ValueError(
            f"--seeds count {n} exceeds the {SEED_MAX} distinct seeds available"
        )
    return n


def _random_seeds(n: int) -> list[int]:
    """``n`` distinct random SCIP seeds in ``[1, SEED_MAX]``."""
    return random.sample(range(1, SEED_MAX + 1), n)


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
    lp_algorithm: LpAlgorithmOpt = None,
    max_area_fraction: MaxAreaOpt = None,
    no_deployment: NoDeploymentOpt = False,
    no_player_time: NoPlayerTimeOpt = False,
    out: OutOpt = None,
    jobs: JobsOpt = None,
    quiet_solver: QuietSolverOpt = False,
    force: ForceOpt = False,
    dry_run: DryRun = False,
) -> None:
    """Solve a run's production-rate plan with SCIP, writing rates.yaml.

    Reads the run manifest's scenario / tech-order / map (resolved relative to
    the current directory), builds the L2 instance, solves the nonconvex NLP
    with SCIP, and records the L2 settings + outcome back into the manifest.

    With --seeds it runs a multi-seed search instead: every seed is solved, each
    candidate is written under runs/<run>/rates-search/ (never touching the
    promoted rates.yaml), the seeds are ranked by t_FINAL, and the best is
    promoted to rates.yaml after a prompt (--force to skip).
    """
    from fplan import config as app_config
    from fplan import refs
    from fplan import run as run_mod
    from fplan.cli import main as cli_main
    from fplan.l2 import backend as l2_backend
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
    if seeds is None and (jobs is not None or quiet_solver):
        typer.echo(
            "error: --jobs / --quiet-solver only apply to a --seeds search.",
            err=True,
        )
        raise typer.Exit(code=2)
    if lp_algorithm is not None and lp_algorithm not in l2_backend.VALID_LP_ALGORITHMS:
        choices = ", ".join(l2_backend.VALID_LP_ALGORITHMS)
        typer.echo(
            f"error: unknown --lp-algorithm {lp_algorithm!r}; choose from {choices}.",
            err=True,
        )
        raise typer.Exit(code=2)
    chosen_seeds: list[int] | None = None
    if seeds is not None:
        try:
            spec = _seed_spec(seeds)
            chosen_seeds = _random_seeds(spec) if isinstance(spec, int) else spec
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc

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
    # LP method: an explicit --lp-algorithm wins; otherwise the config's detected
    # preference; otherwise None (SCIP's own default). A broken config here is
    # non-fatal — the solve proceeds on SCIP's default rather than aborting.
    config_lp = None
    try:
        config_lp = app_config.load_config(state.config_file).lp_algorithm
    except app_config.ConfigError:
        config_lp = None
    lp_from_cli = lp_algorithm is not None
    effective_lp = lp_algorithm if lp_from_cli else config_lp
    solver_kwargs = {
        "time_limit_s": time_limit_s,
        "gap_limit": gap_limit,
        "stall_nodes": stall_nodes,
        "node_limit": node_limit,
        "lp_algorithm": effective_lp,
    }

    # Surface the effective settings (so omitting an optional flag is transparent).
    settings: list[tuple[str, str, bool]] = [
        ("mode", mode, mode == "experimental"),
        (
            "time-limit",
            f"{time_limit_s:g}s" if time_limit_s is not None else "none",
            time_limit_s is None,
        ),
        (
            "gap",
            f"{gap_limit:g}" if gap_limit is not None else "none",
            gap_limit is None,
        ),
        ("deployment", "off" if no_deployment else "on", not no_deployment),
        ("player-time", "off" if no_player_time else "on", not no_player_time),
        (
            "config",
            "default" if config_ref == "default" else str(l2_config),
            config_ref == "default",
        ),
        (
            "lp-algorithm",
            effective_lp if effective_lp is not None else "scip-default",
            not lp_from_cli,
        ),
    ]
    # Advanced knobs: list only when set (their default is "unset").
    for name, val in (
        ("stall-nodes", stall_nodes),
        ("node-limit", node_limit),
        ("max-area-fraction", max_area_fraction),
    ):
        if val is not None:
            settings.append((name, f"{val:g}", False))
    echo_settings(settings)

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

    provided = seed is not None
    if seed is None:
        seed = random.randint(1, SEED_MAX)
    typer.echo(
        f"SCIP seed: {seed} "
        + (
            "(from --seed)"
            if provided
            else f"(random — pass --seed {seed} to reproduce)"
        )
    )

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
    # Clear a prior search's residue so rates-search/ reflects only this search
    # (summary.yaml is rewritten below; orphaned seed-*.yaml/.log would otherwise
    # mislead a casual `ls` into mixing two searches).
    for stale in (
        *search_dir.glob("seed-*.yaml"),
        *search_dir.glob("seed-*.log"),
        search_dir / SEARCH_SUMMARY,
    ):
        stale.unlink(missing_ok=True)

    n_jobs = l2_search.resolve_jobs(jobs, len(seeds))
    parallel = n_jobs > 1
    # Parallel seeds each get full SCIP progress in their own log (so they're
    # monitorable with tail -f) — interleaving the console is the whole problem.
    # --quiet-solver silences SCIP, leaving the logs near-empty. Serial runs SCIP
    # quietly (verbose stays False), as a single solve does.
    solver_kwargs = {**solver_kwargs, "verbose": parallel and not quiet_solver}

    if parallel and quiet_solver:
        typer.echo(
            f"Searching {len(seeds)} seed(s), up to {n_jobs} in parallel "
            "(SCIP silenced via --quiet-solver; per-seed logs stay near-empty)."
        )
    elif parallel:
        typer.echo(
            f"Searching {len(seeds)} seed(s), up to {n_jobs} in parallel "
            "(each worker holds a full model copy) — per-seed logs "
            "(tail -f to monitor):"
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
        jobs=n_jobs,
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
    jobs,
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
    # Atomic replace: copy to a temp sibling, then os.replace onto rates.yaml so
    # an interruption mid-copy can't truncate a previously-promoted good plan.
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        shutil.copyfile(cand_path, tmp_path)
        os.replace(tmp_path, out_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
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
            "jobs": jobs,
            "promoted_from": f"{SEARCH_DIRNAME}/{best['file']}",
            "summary": f"{SEARCH_DIRNAME}/{SEARCH_SUMMARY}",
        },
    }
    run_mod.save(run_dir, manifest)
    typer.echo(
        f"✓ promoted seed {best['seed']} (t_FINAL = {best_obj:.1f}s) → {out_path}"
    )


PostRunArg = Annotated[
    str, typer.Argument(help="Run (under runs/) whose rates.yaml to post-process.")
]
MethodOpt = Annotated[
    str,
    typer.Option(
        "--method",
        help="Rate-flattening method (post's current operation): chord (default) | "
        "tube (taut-string) | mrp (cross-dependency).",
    ),
]
PostFromOpt = Annotated[
    Path | None,
    typer.Option(
        "--from",
        help="Post-process this rates-shaped YAML instead of the run's rates.yaml "
        "(e.g. a search candidate). The output is still the run's rates-post.yaml.",
    ),
]
NoVizOpt = Annotated[
    bool,
    typer.Option("--no-viz", help="Skip auto-generating the visualization."),
]


@group.command()
def post(
    ctx: typer.Context,
    run: PostRunArg,
    method: MethodOpt = "chord",
    from_path: PostFromOpt = None,
    no_viz: NoVizOpt = False,
    open_browser: OpenVizOpt = False,
    force: ForceOpt = False,
    dry_run: DryRun = False,
) -> None:
    """Post-process a solved rates.yaml into the layout-stage (L3) input.

    `rates post` is the L2→L3 post-processing stage; it's still under
    development and will grow more operations. Its *current* operation is
    **rate-flattening**: replacing each item's per-step production rate with the
    smoothest schedule that still meets every deadline — minimizing assembler
    revisits (real TAS player-time) without producing ahead of causality.

    Writes runs/<run>/rates-post.yaml: the same (PROVISIONAL) rates schema with
    the post-processed production characteristics, plus a `post:` block carrying
    the operation's settings, source, and per-item / unmet-input diagnostics. By
    default it also auto-generates a visualization (for the current operation, a
    flattening diff: original vs flattened + the unmet-input table); regenerate
    it later with `rates viz --from`.

    The output is the temporary L2→L3 input and its schema is temporary too —
    it mirrors rates.yaml only because L3's format isn't decided yet. Don't
    build anything downstream that assumes the schema is stable.
    """
    from fplan import config as cfg
    from fplan import run as run_mod
    from fplan.cli import main as cli_main
    from fplan.l2 import flatten as l2_flatten

    state: cli_main.CLIState = ctx.obj

    if method not in l2_flatten.METHODS:
        choices = ", ".join(l2_flatten.METHODS)
        typer.echo(
            f"error: unknown method {method!r}; choose from {choices}.", err=True
        )
        raise typer.Exit(code=2)

    try:
        run_dir = run_mod.run_dir(run)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not run_mod.manifest_path(run_dir).exists():
        typer.echo(f"error: run {run!r} not found at {run_dir}", err=True)
        raise typer.Exit(code=1)

    src = from_path if from_path is not None else run_dir / RATES_NAME
    if not src.exists():
        hint = "" if from_path is not None else " (run `fplan rates solve` first)"
        typer.echo(f"error: rates file not found: {src}{hint}", err=True)
        raise typer.Exit(code=1)

    out_path = run_dir / POST_NAME
    viz_dir = run_dir / "viz"
    viz_path = viz_dir / f"{out_path.stem}-timeline.html"

    if dry_run:
        typer.echo(f"(dry run) would post-process {src} (method={method}) and write:")
        typer.echo(f"  {out_path}")
        if not no_viz:
            typer.echo(f"  {viz_path}")
        return

    try:
        l2 = yaml.safe_load(src.read_text())
    except (OSError, yaml.YAMLError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(l2, dict):
        typer.echo(
            f"error: {src} is not a valid rates YAML (expected a mapping)", err=True
        )
        raise typer.Exit(code=1)

    echo_settings(
        [
            ("method", method, method == "chord"),
            ("source", src.name, from_path is None),
            ("viz", "off" if no_viz else "on", not no_viz),
        ]
    )

    # The model is REQUIRED here (unlike viz): the unmet-input diagnostics and
    # the mrp dependency graph both need the recipe→ingredient map.
    model = cli_main.load_model_or_exit(state.config_file)

    if not force:
        cli_main.confirm_overwrite_or_exit(out_path)

    # source ref recorded in the post block so `rates viz` can find the original
    # series for the faint overlay. Stored relative to the run dir so it resolves
    # (and stays confined) under the post file's directory; a --from outside the
    # run dir falls back to its basename (overlay then degrades gracefully).
    if from_path is None:
        source_ref = RATES_NAME
    else:
        try:
            source_ref = str(from_path.resolve().relative_to(run_dir.resolve()))
        except ValueError:
            source_ref = from_path.name
    try:
        result = l2_flatten.flatten(l2, method=method, model=model)
        post_yaml = l2_flatten.build_post_yaml(l2, result, source_ref=source_ref)
    except (KeyError, ValueError, TypeError, AttributeError, ZeroDivisionError) as exc:
        # `src` is untrusted (--from any file): degenerate shapes (a non-dict
        # step, duplicate/zero-duration timestamps, …) must surface as a clean
        # error, never a raw traceback.
        typer.echo(f"error: malformed rates YAML in {src}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    try:
        out_path.write_text(yaml.safe_dump(post_yaml, sort_keys=False))
    except OSError as exc:
        typer.echo(f"error: could not write {out_path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = result.summary()
    try:
        manifest = run_mod.load(run_dir)
        manifest.extra["post"] = {
            "method": method,
            "source": source_ref,
            "summary": summary,
        }
        run_mod.save(run_dir, manifest)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # The output is already written; a manifest hiccup shouldn't lose it.
        typer.echo(f"warning: could not update manifest: {exc}", err=True)

    typer.echo(f"✓ wrote {out_path}")
    typer.echo(
        f"  method={method}  items={summary['items_scored']}  "
        f"revisits={summary['revisits']} (was {summary['orig_segments']}, "
        f"saved {summary['revisits_saved']})  "
        f"self-stockouts={summary['self_stockouts']}  "
        f"unmet-inputs={summary['deficit_lines']}"
    )

    if no_viz:
        if open_browser:
            typer.echo("note: --open has no effect with --no-viz (nothing to open).")
    else:
        from fplan.l2 import viz as l2_viz

        data_dir = None
        try:
            data_dir = cfg.load_config(state.config_file).data_dir
        except cfg.ConfigError:
            data_dir = None
        try:
            dataset = l2_viz.build_flatten_dataset(post_yaml, l2, data_dir=data_dir)
            html = l2_viz.render_flatten_html(dataset, method=method)
            viz_dir.mkdir(parents=True, exist_ok=True)
            viz_path.write_text(html)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            typer.echo(f"warning: could not render viz: {exc}", err=True)
        except OSError as exc:
            typer.echo(f"warning: could not write viz: {exc}", err=True)
        else:
            typer.echo(f"✓ wrote {viz_path}")
            if open_browser:
                _open_in_browser(viz_path)


VizRunArg = Annotated[
    str, typer.Argument(help="Run (under runs/) whose rates.yaml to visualize.")
]
FromOpt = Annotated[
    Path | None,
    typer.Option(
        "--from",
        help="Visualize this rates-shaped YAML instead of the run's rates.yaml "
        "(e.g. a search candidate rates-search/seed-N.yaml).",
    ),
]
NoHeatmapOpt = Annotated[
    bool,
    typer.Option(
        "--no-heatmap", help="Skip the companion capacity-saturation heatmap."
    ),
]
OpenVizOpt = Annotated[
    bool,
    typer.Option(
        "--open", help="Open the timeline in the default browser after writing."
    ),
]


def _open_in_browser(path: Path) -> None:
    """Open ``path`` in the default browser, following ``fplan init``'s platform
    convention: the OS-specific open is abstracted (``webbrowser`` dispatches to
    macOS ``open`` / Linux ``xdg-open`` / Windows ``start``); an unrecognized
    platform is skipped with a notice, a recognized-but-untested one is flagged
    and still attempted, and any failure falls back to printing the path."""
    import webbrowser

    from fplan import factorio

    platform = factorio.current_platform()
    if platform is None:
        typer.echo(
            f"note: unrecognized platform ({sys.platform}); not opening a browser. "
            f"Open it manually: {path}"
        )
        return
    if factorio.is_untested(platform):
        typer.echo(
            f"note: browser-open is untested on {factorio.platform_label(platform)}; "
            "attempting anyway — open the path manually if nothing happens."
        )
    try:
        opened = webbrowser.open(path.resolve().as_uri())
    except Exception:
        opened = False
    if not opened:
        typer.echo(f"note: could not open a browser; open it manually: {path}")


def _read_overlay_source(post: dict, post_file: Path) -> dict | None:
    """Best-effort load of the original solve referenced by a post block's
    ``source`` — the faint original-rate overlay in the flatten diff view.

    ``post.source`` is attacker-controlled on the ``--from`` path, so resolution
    is **confined to the post file's own directory**: a crafted ``../../etc/...``
    or absolute path resolves outside and is refused (no arbitrary file read).
    The canonical layout (rates.yaml beside rates-post.yaml, or an in-run
    candidate like rates-search/seed-N.yaml) stays within it. Any miss → None,
    so the diff still renders, just without the original line."""
    ref = post.get("source")
    if not isinstance(ref, str) or not ref:
        return None
    base = post_file.parent.resolve()
    candidate = (base / ref).resolve()  # absolute ref discards base → caught below
    try:
        candidate.relative_to(base)
    except ValueError:
        return None  # escapes the post file's directory
    try:
        if candidate.exists():
            data = yaml.safe_load(candidate.read_text())
            return data if isinstance(data, dict) else None
    except (OSError, yaml.YAMLError):
        return None
    return None


@group.command()
def viz(
    ctx: typer.Context,
    run: VizRunArg,
    from_path: FromOpt = None,
    no_heatmap: NoHeatmapOpt = False,
    open_browser: OpenVizOpt = False,
    dry_run: DryRun = False,
) -> None:
    """Render a run's rates.yaml as interactive HTML (timeline + heatmap).

    Writes self-contained HTML under runs/<run>/viz/. The game model is loaded
    best-effort (to enrich the legend with facility counts) — viz works without
    a Factorio install, just without that breakdown.
    """
    from fplan import config as cfg
    from fplan import run as run_mod
    from fplan.cli import main as cli_main
    from fplan.l2 import viz as l2_viz

    state: cli_main.CLIState = ctx.obj

    try:
        run_dir = run_mod.run_dir(run)
    except ValueError as exc:  # bad run name (traversal/empty) → usage error
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not run_mod.manifest_path(run_dir).exists():
        typer.echo(f"error: run {run!r} not found at {run_dir}", err=True)
        raise typer.Exit(code=1)

    src = from_path if from_path is not None else run_dir / RATES_NAME
    if not src.exists():
        hint = "" if from_path is not None else " (run `fplan rates solve` first)"
        typer.echo(f"error: rates file not found: {src}{hint}", err=True)
        raise typer.Exit(code=1)

    # Load the YAML up front: it both validates the shape and selects the view.
    try:
        l2 = yaml.safe_load(src.read_text())
    except (OSError, yaml.YAMLError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(l2, dict):
        typer.echo(
            f"error: {src} is not a valid rates YAML (expected a mapping)", err=True
        )
        raise typer.Exit(code=1)
    # View selection: a `post:` block recording a flattening operation → the
    # flatten diff view; otherwise the timeline. We key off the recorded method
    # (not merely the block's presence) so that future, non-flatten post
    # operations get their own view rather than being mis-rendered as a diff.
    from fplan.l2 import flatten as l2_flatten

    post_block = l2.get("post")
    is_flatten_diff = (
        isinstance(post_block, dict) and post_block.get("method") in l2_flatten.METHODS
    )

    viz_dir = run_dir / "viz"
    stem = src.stem  # rates.yaml → "rates", rates-post.yaml → "rates-post"
    timeline_path = viz_dir / f"{stem}-timeline.html"
    heatmap_path = viz_dir / f"{stem}-heatmap.html"
    # The flatten diff view has no companion heatmap (capacity is unchanged by
    # flattening); --no-heatmap is moot there.
    want_heatmap = not no_heatmap and not is_flatten_diff

    if dry_run:
        view = "flatten diff view" if is_flatten_diff else "timeline"
        typer.echo(f"(dry run) would read {src} ({view}) and write:")
        typer.echo(f"  {timeline_path}")
        if want_heatmap:
            typer.echo(f"  {heatmap_path}")
        return

    settings: list[tuple[str, str, bool]] = [
        ("source", src.name, from_path is None),
        ("view", "flatten-diff" if is_flatten_diff else "timeline", False),
    ]
    if not is_flatten_diff:
        settings.append(("heatmap", "off" if no_heatmap else "on", not no_heatmap))
    echo_settings(settings)

    # Best-effort model load: enrich the legend with facility counts if a valid
    # data_dir is configured; otherwise render from the YAML alone.
    data_dir = None
    try:
        data_dir = cfg.load_config(state.config_file).data_dir
    except cfg.ConfigError:
        data_dir = None

    try:
        viz_dir.mkdir(parents=True, exist_ok=True)
        if is_flatten_diff:
            # Pure render of the post file + its source (for the faint original
            # overlay): no re-flattening; model load stays best-effort (legend
            # facility counts only), per the no-install guarantee.
            source_l2 = _read_overlay_source(l2["post"], src)
            dataset = l2_viz.build_flatten_dataset(l2, source_l2, data_dir=data_dir)
            timeline_path.write_text(l2_viz.render_flatten_html(dataset))
        else:
            dataset = l2_viz.build_dataset(l2, data_dir=data_dir)
            timeline_path.write_text(l2_viz.render_html(dataset))
        outputs = [timeline_path]
        if want_heatmap:
            heatmap_path.write_text(l2_viz.build_heatmap_html(l2))
            outputs.append(heatmap_path)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        # Shape mismatches (e.g. a step that's a str → .get on a non-dict raises
        # AttributeError) map to a clean error, not a raw traceback.
        typer.echo(f"error: malformed rates YAML in {src}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"error: could not write viz: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    for p in outputs:
        typer.echo(f"✓ wrote {p}")
    if is_flatten_diff and not dataset.get("has_orig"):
        typer.echo(
            "note: original-rate overlay omitted — source rates not found "
            f"(post.source = {l2['post'].get('source')!r})"
        )
    if not dataset.get("model_loaded"):
        typer.echo("note: model not loaded — legend omits the facility-count breakdown")
    if open_browser:
        _open_in_browser(timeline_path)
