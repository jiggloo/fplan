"""Runs — create and manage L2→L4 pipeline runs.

A run binds a scenario, a tech-order, and a map into a ``runs/<name>/``
directory described by a ``manifest.yaml`` (see :mod:`fplan.run`).
``create``/``clone``/``show`` manage that structure; ``full`` (executing
L2→L4) is stubbed until those stages land.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml

from fplan import refs
from fplan import run as run_mod
from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented

group = typer.Typer(help="Manage L2→L4 pipeline runs.", no_args_is_help=True)

NameArg = Annotated[str, typer.Argument(help="Run name (a directory under runs/).")]
ScenarioOpt = Annotated[
    Path, typer.Option("--scenario", help="Scenario YAML this run targets.")
]
TechOrderOpt = Annotated[
    Path, typer.Option("--tech-order", help="Tech-order YAML (L1 output) to solve.")
]
MapOpt = Annotated[
    Path, typer.Option("--map", help="Map artifact the run is placed against.")
]

_LOAD_ERRORS = (OSError, ValueError, yaml.YAMLError)


def _now() -> str:
    """Created-timestamp for a manifest. Indirected so tests can override it."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@group.command()
def create(
    ctx: typer.Context,
    name: NameArg,
    scenario: ScenarioOpt,
    tech_order: TechOrderOpt,
    map: MapOpt,
    dry_run: DryRun = False,
) -> None:
    """Create a run directory and manifest binding scenario + tech-order + map."""
    inputs: list[tuple[str, Path]] = [
        ("scenario", scenario),
        ("tech-order", tech_order),
        ("map", map),
    ]
    for label, path in inputs:
        if not path.exists():
            typer.echo(f"error: {label} file not found: {path}", err=True)
            raise typer.Exit(code=1)

    directory = run_mod.run_dir(name)
    if directory.exists():
        typer.echo(
            f"error: run {name!r} already exists at {directory}; remove it or use "
            "`fplan run clone`.",
            err=True,
        )
        raise typer.Exit(code=1)

    if dry_run:
        bindings = ", ".join(f"{label}={path}" for label, path in inputs)
        typer.echo(f"Would create {directory}/ ({bindings}) (dry run; nothing).")
        return

    manifest = run_mod.Manifest.new(
        name, scenario=scenario, tech_order=tech_order, map_path=map, created=_now()
    )
    try:
        path = run_mod.save(directory, manifest)
    except OSError as exc:
        typer.echo(f"error: could not create run: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"→ {path}")


@group.command()
def clone(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Existing run to clone from.")],
    name: NameArg,
    dry_run: DryRun = False,
) -> None:
    """Create a new run from an existing run's manifest (inputs only, no artifacts)."""
    src_dir = run_mod.run_dir(source)
    if not run_mod.manifest_path(src_dir).exists():
        typer.echo(f"error: run {source!r} not found at {src_dir}", err=True)
        raise typer.Exit(code=1)
    dst_dir = run_mod.run_dir(name)
    if dst_dir.exists():
        typer.echo(
            f"error: run {name!r} already exists at {dst_dir}; remove it first.",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        src = run_mod.load(src_dir)
    except _LOAD_ERRORS as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if dry_run:
        typer.echo(
            f"Would clone {source} → {dst_dir}/ (manifest only; stage artifacts not "
            "copied) (dry run; nothing written)."
        )
        return

    try:
        path = run_mod.save(dst_dir, src.cloned(name, created=_now()))
    except OSError as exc:
        typer.echo(f"error: could not create run: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"→ {path}  (cloned from {source}; stage artifacts not copied)")


@group.command()
def show(ctx: typer.Context, name: NameArg) -> None:
    """Show a run's bindings, input freshness, and which stage artifacts exist."""
    directory = run_mod.run_dir(name)
    if not run_mod.manifest_path(directory).exists():
        typer.echo(f"error: run {name!r} not found at {directory}", err=True)
        raise typer.Exit(code=1)
    try:
        manifest = run_mod.load(directory)
    except _LOAD_ERRORS as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"run: {manifest.run}")
    if manifest.created:
        typer.echo(f"created: {manifest.created}")
    if manifest.fplan_version:
        typer.echo(f"fplan: {manifest.fplan_version}")
    typer.echo("inputs:")
    status_label = {True: "✓ current", False: "⚠ changed", None: "✗ missing"}
    for label in ("scenario", "tech-order", "map"):
        ref = manifest.inputs.get(label)
        if not ref:
            continue
        status = status_label[refs.is_current(ref)]
        typer.echo(f"  {label}: {ref.get('path')} [{status}]")
    artifacts = run_mod.stage_artifacts(directory)
    typer.echo("artifacts: " + (", ".join(artifacts) if artifacts else "(none yet)"))


@group.command()
def full(ctx: typer.Context, name: NameArg, dry_run: DryRun = False) -> None:
    """Run the full L2→L4 chain against a run's manifest (excludes L1)."""
    not_implemented(ctx)
