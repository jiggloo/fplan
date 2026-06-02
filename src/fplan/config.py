"""Reading and writing the fplan config file (``.fplan-config.yaml``).

The config lives in the current working directory only — never a user-home or
global location. CLI arguments override config-file values; there is no
environment-variable support.

This module is deliberately CLI-framework-free: it loads/validates/renders, and
raises :class:`ConfigError` for fatal problems. The CLI layer decides how to
surface those (fatal to stderr for commands that require Factorio, a warning to
stdout for those that don't).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_NAME = ".fplan-config.yaml"


class ConfigError(Exception):
    """A fatal configuration problem (unparsable file, missing required paths)."""


@dataclass(frozen=True)
class FplanConfig:
    """A loaded config. ``source`` is the file it came from, or None if absent."""

    data_dir: Path | None
    binary: Path | None
    source: Path | None

    @property
    def present(self) -> bool:
        return self.source is not None


def default_config_path() -> Path:
    """The default config location: ``./.fplan-config.yaml``."""
    return Path.cwd() / DEFAULT_CONFIG_NAME


def _as_path(value: object, field: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(
            f"factorio.{field} must be a string path, got {type(value).__name__}"
        )
    if not value.strip():
        return None
    return Path(value).expanduser()


def load_config(config_file: Path | None = None) -> FplanConfig:
    """Load the config.

    With ``config_file`` given, that exact file must exist (else ConfigError).
    Otherwise the default ``./.fplan-config.yaml`` is used when present; if it is
    absent, an empty config (``source=None``) is returned.
    """
    if config_file is not None:
        if not config_file.exists():
            raise ConfigError(f"config file not found: {config_file}")
        path = config_file
    else:
        path = default_config_path()
        if not path.exists():
            return FplanConfig(data_dir=None, binary=None, source=None)

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level YAML must be a mapping")
    factorio = raw.get("factorio") or {}
    if not isinstance(factorio, dict):
        raise ConfigError(f"{path}: 'factorio' must be a mapping")
    return FplanConfig(
        data_dir=_as_path(factorio.get("data_dir"), "data_dir"),
        binary=_as_path(factorio.get("binary"), "binary"),
        source=path,
    )


def require_data_dir(config: FplanConfig) -> Path:
    """Return the Factorio data dir, or raise ConfigError if it's unusable.

    For commands that cannot run without the game model. Built and tested ahead
    of need — no stage calls it yet (all stages are stubs).
    """
    if config.data_dir is None:
        raise ConfigError(
            f"no Factorio data directory configured — run `fplan init` to create "
            f"{DEFAULT_CONFIG_NAME}, or pass --config-file"
        )
    if not config.data_dir.exists():
        raise ConfigError(
            f"configured Factorio data directory does not exist: {config.data_dir}"
        )
    return config.data_dir


def require_binary(config: FplanConfig) -> Path:
    """Return the Factorio executable, canonicalized, or raise ConfigError.

    For commands that *run* Factorio (the first being ``map from-save``). The
    path is ``resolve()``-d so the subprocess sees a canonical, absolute target
    rather than whatever relative/symlinked form the config happened to hold.
    """
    if config.binary is None:
        raise ConfigError(
            f"no Factorio executable configured — run `fplan init` to create "
            f"{DEFAULT_CONFIG_NAME}, or pass --config-file"
        )
    if not config.binary.exists():
        raise ConfigError(
            f"configured Factorio executable does not exist: {config.binary}"
        )
    if not config.binary.is_file():
        raise ConfigError(
            f"configured Factorio executable is not a file: {config.binary}"
        )
    return config.binary.resolve()


def render_config(data_dir: str | None, binary: str | None) -> str:
    """Render the config file text (with comments) that ``fplan init`` writes.

    Path values are emitted via ``json.dumps`` — a JSON string is a valid YAML
    double-quoted scalar — so quotes/newlines in a path can't corrupt the file
    or inject extra keys.
    """
    return (
        "# .fplan-config.yaml — fplan configuration.\n"
        "# CLI arguments will override these values where commands support it.\n"
        "# There is no environment-variable support. Delete this file and re-run\n"
        "# `fplan init` to regenerate it.\n"
        "factorio:\n"
        "  # Data directory (prototype files). Required by commands that load the\n"
        "  # game model (tech-order, rates, inspect, ...).\n"
        f"  data_dir: {json.dumps(data_dir or '')}\n"
        "  # Executable. Required by commands that run Factorio headless\n"
        "  # (map from-save, ...).\n"
        f"  binary: {json.dumps(binary or '')}\n"
    )
