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


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def summarize(data: dict) -> str:
    """A human-readable summary of a map artifact.

    Breaks the solid-resource patches down per type (count, total tiles, and the
    nearest patch's distance + size), and gives oil and water the same
    nearest-distance treatment. All distances are in tiles from spawn.
    """
    lines = [f"seed={data.get('seed')}  radius={data.get('radius')} tiles"]

    patches = data.get("patches", [])
    by_resource: dict[str, list[dict]] = {}
    for p in patches:
        by_resource.setdefault(p["resource"], []).append(p)
    lines.append(
        f"resources: {_count(len(patches), 'patch', 'patches')} "
        f"across {_count(len(by_resource), 'type', 'types')}"
    )
    for resource in sorted(by_resource):
        group = by_resource[resource]
        total_tiles = sum(p["tile_count"] for p in group)
        nearest = min(group, key=lambda p: p["distance"])
        lines.append(
            f"  {resource}: {_count(len(group), 'patch', 'patches')}, "
            f"{total_tiles} tiles total; nearest {nearest['distance']:.1f} tiles "
            f"away ({nearest['tile_count']} tiles)"
        )

    oil_spots = data.get("oil_spots", [])
    oil_clusters = data.get("oil_clusters", [])
    if oil_clusters:
        nearest_field = min(c["distance"] for c in oil_clusters)
        total_spots = sum(c["spot_count"] for c in oil_clusters) or 1
        avg_yield = sum(c["total_yield_pct"] for c in oil_clusters) / total_spots
        lines.append(
            f"oil: {_count(len(oil_spots), 'spot', 'spots')} in "
            f"{_count(len(oil_clusters), 'field', 'fields')}; "
            f"nearest field {nearest_field:.1f} tiles away; "
            f"avg yield {avg_yield:.0f}%/spot"
        )
    elif oil_spots:
        lines.append(f"oil: {_count(len(oil_spots), 'spot', 'spots')}")

    water = data.get("water_min_distance")
    n_water = len(data.get("water_patches", []))
    water_str = f"{water:.1f} tiles away" if water is not None else "n/a"
    lines.append(f"water: {_count(n_water, 'body', 'bodies')}; nearest {water_str}")

    lines.append(f"trees: {data.get('tree_count')}")
    return "\n".join(lines)
