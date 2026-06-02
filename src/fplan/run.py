"""Runs — the manifest and the run-directory lifecycle.

A *run* is one execution of the L2→L4 pipeline (L1, the tech-order, is an
*input* to a run, not part of it). A run lives in ``runs/<name>/`` and is
described by a ``manifest.yaml`` that binds the run's inputs — scenario,
tech-order, and map — by reference (path + content hash, see
:mod:`fplan.refs`).

The manifest is deliberately **minimal now and grows per stage**: today it
carries the version, the run name, a created timestamp, and the input
bindings. As L2–L4 land they append their own settings and outcomes; this
loader preserves any keys it doesn't recognize (``Manifest.extra``) so an
older reader round-trips a newer manifest without dropping fields.

This module owns the schema and the create/clone operations; the CLI in
``fplan/cli/run.py`` is a thin wrapper that adds path checks, prompts, and
exit codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fplan import __version__, refs

# Runs live under this directory, one subdirectory per run. Relative to the
# working directory, like maps/ — a managed, git-ignored output location.
RUNS_DIR = Path("runs")
MANIFEST_NAME = "manifest.yaml"

# Top-level manifest keys this version owns. Anything else a (newer) manifest
# carries is preserved verbatim through load/save via `Manifest.extra`.
_KNOWN_KEYS = frozenset({"fplan_version", "run", "created", "inputs"})


@dataclass(frozen=True)
class Manifest:
    run: str
    inputs: dict = field(default_factory=dict)
    fplan_version: str = __version__
    created: str = ""
    # Forward-compat: top-level keys added by later stages (settings,
    # outcomes) that this version doesn't model. Preserved across load/save.
    extra: dict = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        name: str,
        *,
        scenario: str | Path,
        tech_order: str | Path,
        map_path: str | Path,
        created: str = "",
        version: str = __version__,
    ) -> Manifest:
        """A fresh manifest binding the given inputs by reference.

        All three inputs are required — a run is L2→L4, and placement (L3)
        needs a map (L2's spatial caps want one too).
        """
        inputs: dict = {
            "scenario": refs.file_ref(scenario),
            "tech-order": refs.file_ref(tech_order),
            "map": refs.file_ref(map_path),
        }
        return cls(run=name, inputs=inputs, fplan_version=version, created=created)

    def cloned(self, new_name: str, *, created: str = "") -> Manifest:
        """A new manifest with the same input bindings but a fresh identity.

        Carries only the inputs — stage settings/outcomes (``extra``) and any
        on-disk stage artifacts are intentionally left behind, so a clone
        starts clean from the same problem definition.
        """
        return Manifest(
            run=new_name,
            inputs=dict(self.inputs),
            fplan_version=self.fplan_version,
            created=created,
        )

    def to_dict(self) -> dict:
        d: dict = {
            "fplan_version": self.fplan_version,
            "run": self.run,
            "created": self.created,
            "inputs": self.inputs,
        }
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Manifest:
        return cls(
            run=str(d.get("run", "")),
            inputs=d.get("inputs") or {},
            fplan_version=str(d.get("fplan_version", "")),
            created=str(d.get("created", "")),
            extra={k: v for k, v in d.items() if k not in _KNOWN_KEYS},
        )


def run_dir(name: str, *, base: Path = RUNS_DIR) -> Path:
    return base / name


def manifest_path(directory: Path) -> Path:
    return directory / MANIFEST_NAME


def save(directory: Path, manifest: Manifest) -> Path:
    """Write the manifest into ``directory`` (created if needed); return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = manifest_path(directory)
    path.write_text(yaml.safe_dump(manifest.to_dict(), sort_keys=False))
    return path


def load(directory: Path) -> Manifest:
    """Read the manifest from ``directory``."""
    path = manifest_path(directory)
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a mapping")
    return Manifest.from_dict(data)


def stage_artifacts(directory: Path) -> list[str]:
    """Filenames in a run dir other than the manifest — the stage outputs
    produced so far (rates/layout/execution as those stages land)."""
    return sorted(
        p.name for p in directory.iterdir() if p.is_file() and p.name != MANIFEST_NAME
    )
