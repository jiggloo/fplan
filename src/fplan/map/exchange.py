"""Ingest and validate a Factorio map-exchange string.

A map-exchange string is the ``>>>`` … ``<<<`` blob Factorio's map-generation
screen exports: an envelope around base64-encoded, zlib-compressed bytes that
begin with a four-field version header and then the serialized map-gen settings.
It carries the *settings and seed* — not a generated world — so producing a map
artifact from one still needs Factorio (see :mod:`fplan.map.extract`).

This module is the pure, Factorio-free front half: it resolves the raw text
(from a file, stdin, or an interactive paste) and validates the envelope far
enough to fail fast — before a multi-minute headless run — without
deserializing the settings (Factorio's own parser does that in the probe mod).
"""

from __future__ import annotations

import base64
import binascii
import struct
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ENVELOPE_OPEN = ">>>"
ENVELOPE_CLOSE = "<<<"
_HEADER_BYTES = 8  # four little-endian uint16: main, major, minor, developer


class ExchangeError(Exception):
    """The map-exchange input was missing, unreadable, or malformed."""


@dataclass(frozen=True)
class ExchangeString:
    """A validated map-exchange string and the version it declares.

    ``raw`` is the normalized envelope (markers around the whitespace-stripped
    body) handed to Factorio's parser. ``version`` is the 4-field header
    (main, major, minor, developer).
    """

    raw: str
    version: tuple[int, int, int, int]

    @property
    def version_label(self) -> str:
        """The first three header fields as ``"1.1.92"``."""
        return ".".join(str(n) for n in self.version[:3])


def parse_exchange_string(text: str) -> ExchangeString:
    """Validate and decode a map-exchange string, or raise :class:`ExchangeError`.

    Checks the ``>>>`` … ``<<<`` envelope, strips all whitespace from the body
    (terminals soft-wrap long pastes), base64-decodes, zlib-inflates, and reads
    the version header. Does *not* deserialize the map-gen settings.
    """
    text = text.strip()
    if not text:
        raise ExchangeError("empty map-exchange string")
    if not (
        text.startswith(ENVELOPE_OPEN)
        and text.endswith(ENVELOPE_CLOSE)
        and len(text) > len(ENVELOPE_OPEN) + len(ENVELOPE_CLOSE)
    ):
        raise ExchangeError("not a map-exchange string (missing the >>> … <<< markers)")

    body = "".join(text[len(ENVELOPE_OPEN) : -len(ENVELOPE_CLOSE)].split())
    if not body:
        raise ExchangeError("map-exchange string is empty between the markers")

    try:
        packed = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExchangeError("map-exchange string is not valid base64") from exc

    try:
        blob = zlib.decompress(packed)
    except zlib.error as exc:
        raise ExchangeError(
            "map-exchange string did not decompress (corrupt or truncated)"
        ) from exc

    if len(blob) < _HEADER_BYTES:
        raise ExchangeError(
            "map-exchange payload is too short to contain a version header"
        )
    version = struct.unpack_from("<4H", blob, 0)
    return ExchangeString(raw=f"{ENVELOPE_OPEN}{body}{ENVELOPE_CLOSE}", version=version)


def resolve_source(
    from_path: Path | None,
    *,
    is_interactive: Callable[[], bool],
    read_stdin: Callable[[], str] = lambda: sys.stdin.read(),
    prompt: Callable[[str], str] = input,
) -> tuple[str, str]:
    """Resolve the raw map-exchange text and a label for where it came from.

    Returns ``(text, source)`` with ``source`` one of ``"file"`` / ``"stdin"`` /
    ``"interactive"`` (used for the ``settings:`` line). The effects
    (``read_stdin``, ``prompt``) and the ``is_interactive`` check are injected so
    this stays unit-testable without a real TTY.

    - ``--from -`` reads stdin.
    - ``--from <path>`` reads that file (missing/unreadable → :class:`ExchangeError`).
    - no ``--from``: paste interactively when stdin is a TTY, else a clean fatal
      (we cannot prompt a non-interactive stream).
    """
    if from_path == Path("-"):
        return read_stdin(), "stdin"
    if from_path is not None:
        if not from_path.exists():
            raise ExchangeError(f"file not found: {from_path}")
        try:
            return from_path.read_text(), "file"
        except OSError as exc:
            raise ExchangeError(f"could not read {from_path}: {exc}") from exc
    if not is_interactive():
        raise ExchangeError(
            "no map-exchange string given and stdin is not a TTY; "
            "pass --from <path> or --from -"
        )
    return prompt("Paste the map-exchange string: "), "interactive"
