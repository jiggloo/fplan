"""Root fplan CLI: wires the stage groups together and the top-level commands.

Bare ``fplan`` (no subcommand) prints the working directory it will operate from
and the resolved config status — listing files is what the shell is for. Each
pipeline stage is a command group (L1 ``tech-order`` → L4 ``execution``);
cross-cutting commands (``init``, ``full-run``) live at the top level.

The ``--config-file`` global option is stashed on ``ctx.obj`` so subcommands can
read it; CLI arguments override config-file values, and there is no
environment-variable support.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from fplan import __version__, factorio
from fplan import config as cfg
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


@dataclass
class CLIState:
    """Shared invocation state, attached to ``ctx.obj``."""

    config_file: Path | None = None


def _stdin_is_interactive() -> bool:
    """Whether we can prompt the user. Indirected so tests can override it."""
    return sys.stdin.isatty()


def factorio_data_dir_or_exit(config_file: Path | None) -> Path:
    """Resolve the Factorio data dir for a command that *requires* it.

    Fatal-to-stderr on any problem. Built and tested ahead of need; no stage
    calls it yet (all stages are stubs).
    """
    try:
        return cfg.require_data_dir(cfg.load_config(config_file))
    except cfg.ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def factorio_binary_or_exit(config_file: Path | None) -> Path:
    """Resolve the Factorio executable for a command that *runs* it.

    Fatal-to-stderr on any problem, mirroring :func:`factorio_data_dir_or_exit`.
    First used by ``map from-save``.
    """
    try:
        return cfg.require_binary(cfg.load_config(config_file))
    except cfg.ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fplan {__version__}")
        raise typer.Exit()


def _report_config(config_file: Path | None) -> None:
    """Bare-`fplan` consumer: load the config and report its status to stdout.

    Bare `fplan` does not *require* config, so problems are warnings, not fatal.
    """
    try:
        conf = cfg.load_config(config_file)
    except cfg.ConfigError as exc:
        typer.echo(f"config: warning — {exc}")
        return
    if not conf.present:
        typer.echo(
            f"config: none found (run `fplan init` to create {cfg.DEFAULT_CONFIG_NAME})"
        )
        return
    typer.echo(f"config: {conf.source}")
    for label, value in (("data_dir", conf.data_dir), ("binary", conf.binary)):
        if value is None:
            typer.echo(f"  factorio {label}: (unset)")
        else:
            status = "ok" if value.exists() else "MISSING"
            typer.echo(f"  factorio {label}: {value} [{status}]")


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
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            help="Path to a config file (default: ./.fplan-config.yaml).",
        ),
    ] = None,
) -> None:
    """fplan — Factorio production and placement planner."""
    ctx.obj = CLIState(config_file=config_file)
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(f"fplan working directory: {Path.cwd()}")
    _report_config(config_file)
    typer.echo("\nRun `fplan --help` to list the available commands.")


def _detect_factorio_interactively() -> factorio.FactorioInstall | None:
    """Run `init`'s detection: approval prompt, OS scan, manual fallback.

    Returns the resolved install, or None when a template should be written
    (unrecognized platform, declined, non-interactive, or no path given).
    """
    platform = factorio.current_platform()
    if platform is None:
        typer.echo(
            f"warning: unrecognized platform ({sys.platform}); cannot auto-detect "
            "Factorio. Writing a template."
        )
        return None
    if factorio.is_untested(platform):
        typer.echo(
            "note: Factorio auto-detection is untested on "
            f"{factorio.platform_label(platform)} — verify the paths it writes."
        )
    if not _stdin_is_interactive():
        typer.echo(
            "non-interactive session: skipping the system scan. Writing a template."
        )
        return None
    if not typer.confirm(
        "May fplan scan the known Factorio install locations on this system?",
        default=True,
    ):
        typer.echo("skipping the system scan. Writing a template.")
        return None

    install = factorio.detect(platform)
    if install is not None:
        return install

    example = factorio.default_root(platform)
    typer.echo(
        "No Factorio installation found in the known locations for "
        f"{factorio.platform_label(platform)}."
    )
    root_str = typer.prompt(
        f"Enter your Factorio install root (example: {example}), or leave blank "
        "to skip",
        default="",
        show_default=False,
    )
    if not root_str.strip():
        return None
    return factorio.derive_from_root(Path(root_str.strip()).expanduser(), platform)


@app.command("full-run")
def full_run(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Run the whole L1 → L4 chain (gated on cross-stage discovery)."""
    not_implemented(ctx)


@app.command()
def init(ctx: typer.Context, dry_run: DryRun = False) -> None:
    """Detect Factorio and write the default config file (.fplan-config.yaml)."""
    state: CLIState = ctx.obj
    target = state.config_file or cfg.default_config_path()

    if target.exists():
        typer.echo(
            f"{target} already exists; delete it to regenerate. Nothing written."
        )
        return
    if dry_run:
        typer.echo(f"Would create {target} (dry run; nothing written).")
        return

    install = _detect_factorio_interactively()
    try:
        target.write_text(
            cfg.render_config(
                str(install.data_dir) if install else None,
                str(install.binary) if install else None,
            )
        )
    except OSError as exc:
        typer.echo(f"error: could not write {target}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if install is None:
        typer.echo(f"Created {target} as a template — fill in the Factorio paths.")
    elif install.data_dir.exists():
        typer.echo(f"Created {target} with detected Factorio paths.")
    else:
        typer.echo(
            f"Created {target} with candidate Factorio paths — verify them; the "
            "data directory was not found on disk."
        )
