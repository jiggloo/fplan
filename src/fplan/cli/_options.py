"""Shared CLI option definitions, reused across commands for consistency."""

from __future__ import annotations

from typing import Annotated

import typer

DryRun = Annotated[
    bool,
    typer.Option("--dry-run", help="Show what would happen without doing it."),
]
