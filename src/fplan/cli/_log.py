"""Shared CLI logging helpers — the *effective-settings* transparency contract.

A user who omits an optional flag should still see what the command ran with.
Each command, before doing its work, prints one compact ``settings:`` line via
``echo_settings`` listing the effective value of its implicit parameters, with
``(default)`` marking the ones the user didn't override.
"""

from __future__ import annotations

import typer

# (name, rendered-value, is_default) — value already stringified by the caller.
SettingItem = tuple[str, str, bool]


def echo_settings(items: list[SettingItem]) -> None:
    """Print one ``settings:`` line. Items flagged ``is_default`` get a trailing
    ``(default)`` so an omitted-but-active value reads clearly. No-op if empty."""
    if not items:
        return
    parts = [
        f"{name}={value}" + (" (default)" if is_default else "")
        for name, value, is_default in items
    ]
    typer.echo("settings: " + "  ·  ".join(parts))
