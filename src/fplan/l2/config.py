"""L2 tuning config — the externalized, override-able knobs for the solve.

L2 carries a body of *tuning* values that aren't derivable from Factorio's
prototype data: per-building deployment packings, player-physics constants,
spatial caps, planning-mode weights and bootstrap seeding, and the
modeling-scope policy sets. They were module-level literals;
this module externalizes them into a YAML config so a power user can tune them
without editing code.

Resolution: a **packaged default** (``resources/l2-defaults.yaml``) is always
loaded; an optional user file is **deep-merged** on top, so the user specifies
only the keys they want to change. Game-physics facts (boiler/rocket constants
in :mod:`fplan.l2.pseudo_recipes`), the constraint formulation, and the SCIP
random seed deliberately stay in code, not here.

``load_config(path)`` returns a frozen :class:`L2Config`. ``build_instance``
loads it once and stores the resolved values on the ``L2Instance`` the solver
consumes; the config is also referenced (path + hash) in the run manifest for
reproducibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

# Bump on any schema change (new key, renamed key, semantic change). A user
# config declaring an older `version` loads but warns.
VERSION = "1.1.0"

_DEFAULTS_RESOURCE = "l2-defaults.yaml"


@dataclass(frozen=True)
class DeploymentPattern:
    """Persistent infrastructure + total tile footprint for one deployed
    building (see the deployment section of the default config)."""

    infrastructure_items: dict[str, float]
    tile_footprint: float


@dataclass(frozen=True)
class L2Config:
    version: str
    deployment: dict[str, DeploymentPattern]
    # Player physics.
    walking_speed_tps: float
    placement_tick_s: float
    tree_mining_rate_base: float
    tree_mining_rate_steelaxe: float
    wood_per_tree: float
    # Spatial / count caps.
    burner_drill_cap: float
    stone_furnace_cap: float
    chest_inserter_per: float
    chest_tile_footprint: float
    max_area_fraction: float
    # Planning-mode end-of-step weights + bootstrap seeding.
    experimental_raw_weight: float
    experimental_default_weight: float
    trapezoidal_weight: float
    lower_bound_weight: float
    experimental_seed: dict[str, float]
    lower_bound_seed: dict[str, float]
    # Modeling-scope policy sets.
    raw_extraction_buildings: frozenset[str]
    pruned_items: frozenset[str]
    smelting_disabled_buildings: frozenset[str]
    single_machine_recipes: frozenset[str]
    split_research_techs: frozenset[str]
    fuel_excluded: frozenset[str]
    # Rocket-silo module hack: apply the modules/beacons a scenario declares to
    # the silo's rocket-part crafting. False → silo runs at base speed.
    silo_modules_enabled: bool = True

    def deployment_for(self, building_name: str) -> DeploymentPattern:
        """The deployment pattern for a building, or an empty one (no infra,
        zero footprint → no spatial cap) if none is registered."""
        return self.deployment.get(building_name, _EMPTY_PATTERN)


_EMPTY_PATTERN = DeploymentPattern(infrastructure_items={}, tile_footprint=0.0)


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto ``base`` (mappings merge key-by-key;
    every other value, including lists, replaces wholesale)."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(text: str, source: str) -> dict:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{source}: L2 config must be a mapping")
    return data


def _from_dict(d: dict) -> L2Config:
    try:
        deployment = {
            name: DeploymentPattern(
                infrastructure_items={
                    str(k): float(v)
                    for k, v in (entry.get("infrastructure_items") or {}).items()
                },
                tile_footprint=float(entry.get("tile_footprint", 0.0)),
            )
            for name, entry in (d.get("deployment") or {}).items()
        }
        physics = d["physics"]
        caps = d["caps"]
        modes = d["modes"]
        policy = d["policy"]
        return L2Config(
            version=str(d.get("version", "")),
            deployment=deployment,
            walking_speed_tps=float(physics["walking_speed_tps"]),
            placement_tick_s=float(physics["placement_tick_s"]),
            tree_mining_rate_base=float(physics["tree_mining_rate_base"]),
            tree_mining_rate_steelaxe=float(physics["tree_mining_rate_steelaxe"]),
            wood_per_tree=float(physics["wood_per_tree"]),
            burner_drill_cap=float(caps["burner_drill"]),
            stone_furnace_cap=float(caps["stone_furnace"]),
            chest_inserter_per=float(caps["chest_inserter_per"]),
            chest_tile_footprint=float(caps["chest_tile_footprint"]),
            max_area_fraction=float(caps["max_area_fraction"]),
            experimental_raw_weight=float(modes["experimental_raw_weight"]),
            experimental_default_weight=float(modes["experimental_default_weight"]),
            trapezoidal_weight=float(modes["trapezoidal_weight"]),
            lower_bound_weight=float(modes["lower_bound_weight"]),
            experimental_seed={
                str(k): float(v) for k, v in (modes["experimental_seed"] or {}).items()
            },
            lower_bound_seed={
                str(k): float(v) for k, v in (modes["lower_bound_seed"] or {}).items()
            },
            raw_extraction_buildings=frozenset(policy["raw_extraction_buildings"]),
            pruned_items=frozenset(policy["pruned_items"]),
            smelting_disabled_buildings=frozenset(
                policy["smelting_disabled_buildings"]
            ),
            single_machine_recipes=frozenset(policy["single_machine_recipes"]),
            split_research_techs=frozenset(policy["split_research_techs"]),
            fuel_excluded=frozenset(policy["fuel_excluded"]),
            silo_modules_enabled=bool(
                (d.get("silo_modules") or {}).get("enabled", True)
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid L2 config: {exc}") from exc


def default_dict() -> dict:
    """The packaged default config as a parsed dict."""
    src = resources.files("fplan") / "resources" / _DEFAULTS_RESOURCE
    return _load_yaml(src.read_text(), _DEFAULTS_RESOURCE)


def load_config(path: str | Path | None = None) -> L2Config:
    """Load the packaged defaults, deep-merge a user override if given, and
    return a validated :class:`L2Config`. A version mismatch warns (the merge
    still proceeds — forward/backward compatible by design)."""
    merged = default_dict()
    if path is not None:
        p = Path(path)
        over = _load_yaml(p.read_text(), str(p))
        user_version = str(over.get("version", "")) or None
        if user_version and user_version != VERSION:
            print(
                f"note: L2 config {p} declares version {user_version!r}; "
                f"this fplan expects {VERSION!r} — merging anyway.",
            )
        merged = _deep_merge(merged, over)
    return _from_dict(merged)
