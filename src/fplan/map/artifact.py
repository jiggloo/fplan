"""Reading, writing, and summarizing a map artifact.

A *map artifact* is a single self-describing YAML bundle (seed + map-gen
settings + extracted patches/oil/water/trees) so a map can be reproduced and
inspected from the file alone. ``from-save`` writes one; ``show`` reads and
summarizes one. These functions are pure I/O + formatting — no Factorio — so
they are fully unit-testable.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ArtifactError(Exception):
    """A map artifact could not be read or is malformed."""


def write_yaml(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def load_artifact(path: Path) -> dict:
    """Load a map artifact, raising :class:`ArtifactError` on any problem."""
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ArtifactError(f"could not read map artifact {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ArtifactError(f"{path}: not a map artifact (expected a mapping)")
    return raw


def summarize(data: dict) -> str:
    """A concise human-readable summary of a map artifact (counts + distances)."""
    n_patches = len(data.get("patches", []))
    n_oil = len(data.get("oil_spots", []))
    n_oil_fields = len(data.get("oil_clusters", []))
    n_water = len(data.get("water_patches", []))
    n_trees = data.get("tree_count")
    water = data.get("water_min_distance")
    water_str = f"{water:.1f}" if water is not None else "n/a"
    return (
        f"seed={data.get('seed')}  radius={data.get('radius')}\n"
        f"  {n_patches} solid resource patches\n"
        f"  {n_oil} oil spots in {n_oil_fields} fields\n"
        f"  {n_water} water bodies (nearest at {water_str})\n"
        f"  {n_trees} trees"
    )
