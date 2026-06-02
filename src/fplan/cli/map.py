"""Map artifact generation and inspection.

A map artifact is a single self-describing bundle (seed + map-gen settings +
extracted data) so a map can always be reproduced from the file alone. No
``viz`` here: the L3 render already includes the map, and Factorio itself is a
better map viewer than fplan could build.
"""

from __future__ import annotations

import typer

from fplan.cli._options import DryRun
from fplan.cli._stub import not_implemented, not_migrated

group = typer.Typer(
    help="Map artifact generation and inspection.", no_args_is_help=True
)


@group.command("from-string")
def from_string(dry_run: DryRun = False) -> None:
    """Build a map artifact from a Factorio map-exchange string."""
    not_implemented("map from-string")


@group.command("from-save")
def from_save(dry_run: DryRun = False) -> None:
    """Build a map artifact (the freshly-generated map) from a save file."""
    not_migrated("map from-save")


@group.command()
def show() -> None:
    """Print a text summary of a map artifact."""
    not_implemented("map show")
