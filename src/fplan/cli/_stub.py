"""Stub helpers for commands whose logic has not landed yet.

The CLI exposes the full command tree from day one; most leaves don't have
real behavior yet. Each un-built leaf calls one of these helpers so the
surface is navigable and self-documenting without ever raising a traceback.

Two distinct, reserved exit codes let scripts and tests tell the two states
apart:

- ``EXIT_NOT_MIGRATED`` — the capability exists in the source project
  (factorio_explore) but hasn't been ported to fplan yet.
- ``EXIT_NOT_IMPLEMENTED`` — genuinely new functionality with no upstream
  equivalent.

Per the stream convention these notices are informational (a graceful
"not available yet" state, not a crash), so they go to stdout; stderr is
reserved for fatal errors.
"""

from __future__ import annotations

from typing import NoReturn

import typer

EXIT_NOT_MIGRATED = 70
EXIT_NOT_IMPLEMENTED = 71


def not_migrated(ctx: typer.Context) -> NoReturn:
    """Report that the command exists upstream but isn't ported yet, then exit.

    The command path is read from ``ctx`` (Typer already owns it) so the notice
    can never drift out of sync with a renamed command or group.
    """
    typer.echo(
        f"'{ctx.command_path}' is not available yet: it exists in the source "
        f"project (factorio_explore) but has not been ported to fplan."
    )
    raise typer.Exit(code=EXIT_NOT_MIGRATED)


def not_implemented(ctx: typer.Context) -> NoReturn:
    """Report that the command is planned but has no implementation, then exit."""
    typer.echo(
        f"'{ctx.command_path}' is not implemented yet: it is planned but has "
        f"no implementation."
    )
    raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)
