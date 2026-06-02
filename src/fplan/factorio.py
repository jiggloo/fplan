"""Locating a Factorio installation.

Detection only — finding *where* Factorio lives so commands can read its data
(and, later, run it headless). The execution seam itself is deferred; this
module just encodes the per-OS install layouts.

Only the macOS locations are verified in practice. The Windows and Linux
candidates are taken from the Factorio wiki (https://wiki.factorio.com/Application_directory)
but are untested, so `fplan init` warns when it runs on those platforms.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FactorioInstall:
    """A resolved data directory + executable for one Factorio installation."""

    data_dir: Path
    binary: Path


_LABELS = {"darwin": "macOS", "linux": "Linux", "win32": "Windows"}

# Platform key -> known default install roots (the first is shown as the example
# when prompting the user).
_KNOWN_ROOTS: dict[str, list[str]] = {
    "darwin": [
        "~/Library/Application Support/Steam/steamapps/common/Factorio",
        "/Applications/factorio.app",
    ],
    "win32": [
        "C:/Program Files (x86)/Steam/steamapps/common/Factorio",
        "C:/Program Files/Factorio",
    ],
    "linux": [
        "~/.steam/steam/steamapps/common/Factorio",
        "~/.factorio",
        "~/.var/app/com.valvesoftware.Steam/.steam/steam/steamapps/common/Factorio",
    ],
}

# Platform key -> candidate (data_dir, binary) layouts relative to an install
# root. macOS bundles them inside the .app; Windows/Linux use bin/x64 + data.
_LAYOUTS: dict[str, list[tuple[str, str]]] = {
    "darwin": [
        ("factorio.app/Contents/data", "factorio.app/Contents/MacOS/factorio"),
        ("Contents/data", "Contents/MacOS/factorio"),
        ("data", "MacOS/factorio"),
    ],
    "win32": [("data", "bin/x64/factorio.exe")],
    "linux": [("data", "bin/x64/factorio")],
}


def current_platform() -> str | None:
    """Return the normalized platform key, or None if unrecognized."""
    p = sys.platform
    if p == "darwin":
        return "darwin"
    if p == "win32":
        return "win32"
    if p.startswith("linux"):
        return "linux"
    return None


def platform_label(platform: str) -> str:
    """Human-readable name for a platform key."""
    return _LABELS.get(platform, platform)


def is_untested(platform: str) -> bool:
    """Whether auto-detection is unverified here (everything but macOS today)."""
    return platform != "darwin"


def default_root(platform: str) -> str:
    """The canonical install root, shown to the user as an example."""
    roots = _KNOWN_ROOTS.get(platform, [])
    return roots[0] if roots else ""


def derive_from_root(root: Path, platform: str) -> FactorioInstall:
    """Derive (data_dir, binary) from an install root using the platform layout.

    Returns the first layout whose data directory exists; if none do, returns the
    primary layout anyway so the paths can still be written for the user to fix.
    """
    layouts = _LAYOUTS.get(platform) or [("data", "bin/x64/factorio")]
    for data_suffix, bin_suffix in layouts:
        candidate = FactorioInstall(root / data_suffix, root / bin_suffix)
        if candidate.data_dir.exists():
            return candidate
    data_suffix, bin_suffix = layouts[0]
    return FactorioInstall(root / data_suffix, root / bin_suffix)


def detect(platform: str) -> FactorioInstall | None:
    """Scan the known install roots for this platform; return the first hit."""
    for root_str in _KNOWN_ROOTS.get(platform, []):
        install = derive_from_root(Path(root_str).expanduser(), platform)
        if install.data_dir.exists() and install.binary.exists():
            return install
    return None
