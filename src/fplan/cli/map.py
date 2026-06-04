"""Map artifact generation and inspection.

A map artifact is a single self-describing bundle (seed + map-gen settings +
extracted data) so a map can always be reproduced from the file alone. No
``viz`` here: the L3 render already includes the map, and Factorio itself is a
better map viewer than fplan could build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from fplan import factorio
from fplan.cli._log import echo_settings
from fplan.cli._options import DryRun
from fplan.map import artifact, cluster, exchange, extract

group = typer.Typer(
    help=(
        "Map artifact generation and inspection. Extracting from a save "
        "(`from-save`) launches Factorio headless — it runs the game in the "
        "background, which can take a few minutes."
    ),
    no_args_is_help=True,
)

SaveArg = Annotated[Path, typer.Argument(help="Factorio save file (.zip) to probe.")]
OutOpt = Annotated[
    Path,
    typer.Option("--out", help="Artifact output path (.yaml) to write. Required."),
]
ArtifactArg = Annotated[Path, typer.Argument(help="Map artifact (.yaml) to summarize.")]
FromOpt = Annotated[
    Path | None,
    typer.Option(
        "--from",
        help="Read the map-exchange string from this file, or `-` for stdin. "
        "Omit to paste it interactively.",
    ),
]


def _warn_if_untested() -> None:
    """Mirror `fplan init`: note that off-macOS Factorio interaction is untested."""
    platform = factorio.current_platform()
    if platform is not None and factorio.is_untested(platform):
        typer.echo(
            "note: headless map extraction is untested on "
            f"{factorio.platform_label(platform)} — only macOS is verified."
        )


@group.command("from-string")
def from_string(
    ctx: typer.Context,
    out: OutOpt,
    from_path: FromOpt = None,
    dry_run: DryRun = False,
) -> None:
    """Build a map artifact from a Factorio map-exchange string.

    Reads the string from a file (--from PATH), stdin (--from -), or an
    interactive paste (no --from). A map-exchange string holds only map-gen
    settings, so this generates a world from them with Factorio headless (runs
    the game in the background; can take a few minutes) and probes it — producing
    the same artifact as from-save.
    """
    # Imported here to avoid an import cycle (main imports this module).
    from fplan.cli import main as cli_main

    state: cli_main.CLIState = ctx.obj

    # Resolve + validate the string first: a bad paste must fail instantly, never
    # after a multi-minute Factorio run. A bare invocation on a non-TTY (nothing
    # to prompt) also surfaces here as a clean error, not a hang.
    try:
        text, source = exchange.resolve_source(
            from_path, is_interactive=cli_main._stdin_is_interactive
        )
        parsed = exchange.parse_exchange_string(text)
    except exchange.ExchangeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    echo_settings(
        [
            ("source", source, from_path is None),
            ("factorio-version", parsed.version_label, False),
        ]
    )

    _warn_if_untested()
    if dry_run:
        clobber = " (overwriting the existing file)" if out.exists() else ""
        typer.echo(
            f"Would generate a map from the {source} map-exchange string "
            f"(Factorio {parsed.version_label}) → {out}{clobber} "
            "(dry run; Factorio not run)."
        )
        return

    # Guard before resolving the binary and the multi-minute run, not after.
    cli_main.confirm_overwrite_or_exit(out)

    binary = cli_main.factorio_binary_or_exit(state.config_file)
    typer.echo("Generating map from exchange string (running Factorio headless) ...")
    try:
        data = extract.extract_from_string(exchange_string=parsed.raw, binary=binary)
    except extract.ExtractError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    cluster.postprocess(data)
    artifact.write_yaml(data, out)
    typer.echo(artifact.summarize(data))
    typer.echo(f"→ {out}")


@group.command("from-save")
def from_save(
    ctx: typer.Context,
    save: SaveArg,
    out: OutOpt,
    dry_run: DryRun = False,
) -> None:
    """Build a map artifact (resources, oil, water, trees) from a save file.

    Launches Factorio headless (runs the game in the background; can take a few
    minutes).
    """
    # Imported here to avoid an import cycle (main imports this module).
    from fplan.cli import main as cli_main

    state: cli_main.CLIState = ctx.obj
    if not save.exists():
        typer.echo(f"error: save file not found: {save}", err=True)
        raise typer.Exit(code=1)

    _warn_if_untested()
    if dry_run:
        clobber = " (overwriting the existing file)" if out.exists() else ""
        typer.echo(
            f"Would extract {save} → {out}{clobber} (dry run; Factorio not run)."
        )
        return

    # Guard before the multi-minute Factorio run, not after: don't clobber an
    # existing artifact silently.
    cli_main.confirm_overwrite_or_exit(out)

    binary = cli_main.factorio_binary_or_exit(state.config_file)
    typer.echo(f"Extracting map from {save.name} (running Factorio headless) ...")
    try:
        data = extract.extract(save=save, binary=binary)
    except extract.ExtractError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    cluster.postprocess(data)
    artifact.write_yaml(data, out)
    typer.echo(artifact.summarize(data))
    typer.echo(f"→ {out}")


@group.command()
def show(ctx: typer.Context, path: ArtifactArg) -> None:
    """Print a text summary of a map artifact."""
    try:
        data = artifact.load_artifact(path)
    except artifact.ArtifactError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(artifact.summarize(data))
