"""Shared L2/L3 pseudo-recipe definitions.

L2 introduces synthetic recipes that don't exist in `factorio_model.py`'s
data: tech research (one per tech), rocket launch (per payload), and
boiler-engine burn (per chemical fuel). Their stoichiometry was
previously hardcoded inside `l2_phases.py`, invisible to L3 — which
caused L3 to silently drop research's science-pack consumption (the
`research/<tech>` recipe names don't appear in `model.recipes`).

This module is the single source of truth: L2 builds PseudoRecipes
here to drive its LP; L3 calls `lookup(name, model)` to resolve
pseudo-recipe stoichiometry when it encounters one in the L2 YAML.

Versioning: bump `VERSION` on any semantic change (new pseudo-recipe
kind, ingredient list shape, constant value change). L2 writes
`pseudo_recipes_version` into its YAML output; L3 cross-checks on
load and warns on mismatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fplan.model import GameModel


VERSION = "1.0.0"


# Item conventions and physical constants. Live here (not in l2_phases)
# so L3 can read them too — e.g., LAUNCH_EVENT_ITEM is what L3 should
# treat as "virtual, not a real item flow".
LAUNCH_EVENT_ITEM = "rocket-launch-event"
ROCKET_PART_ITEM = "rocket-part"
ROCKET_PARTS_PER_LAUNCH = 100.0

# Wood is recipe-eligible but is forbidden as a fuel choice (tree
# placement is an L3 concern). Kept here because the burn pseudo-recipe
# factory excludes it.
FUEL_EXCLUDED: frozenset[str] = frozenset({"wood"})

BOILER_CYCLE_SECONDS = 1.0
BOILER_POWER_W = 1_800_000.0
BOILER_WATER_PER_SECOND = 60.0
BOILER_STEAM_ENGINES_PER_BOILER = 2.0


@dataclass(frozen=True)
class PseudoRecipe:
    """Synthetic recipe shared by L2 (LP construction) and L3 (flow
    graph). Mirrors Recipe's shape so LP code can treat real and
    synthetic recipes uniformly. `capacity_per_building` lists
    (building_name, units_per_cycle) entries — all-of semantics:
    each cycle consumes `units_per_cycle` building-seconds from each
    listed building simultaneously.
    """

    name: str
    kind: str  # "research" | "launch" | "burn"
    time_seconds: float
    ingredients: tuple[tuple[str, float], ...]  # per cycle
    outputs: tuple[tuple[str, float], ...]  # per cycle
    capacity_per_building: tuple[tuple[str, float], ...]
    # If set, hard-bind this pseudo-recipe to step `bound_step` with
    # exactly `cycles_required` executions during that step. L2-only;
    # L3 ignores these.
    bound_step: int | None = None
    cycles_required: float | None = None
    # Joules of electrical energy produced per cycle (boiler burns).
    # Aggregated by the LP into the per-step energy balance side
    # constraint rather than tracked as an item.
    electric_output_j_per_cycle: float = 0.0


# ---------------------------------------------------------------------------
# Factories — used by both L2 (constructing LP) and L3 (lookup by name)
# ---------------------------------------------------------------------------


def for_research(
    tech_name: str,
    model: GameModel,
    *,
    step_index: int | None = None,
    cycles_required: float | None = None,
) -> PseudoRecipe | None:
    """Research pseudo-recipe for a tech. Returns None for
    trigger-based techs (Factorio 2.0): those don't consume science
    packs in labs and need a different L2 representation (TODO).
    """
    t = model.technologies.get(tech_name)
    if t is None or t.research_trigger:
        return None
    if not t.ingredients or t.count is None:
        return None
    return PseudoRecipe(
        name=f"research/{tech_name}",
        kind="research",
        time_seconds=float(t.time or 0.0),
        ingredients=tuple((pack, float(amt)) for pack, amt in t.ingredients),
        outputs=(),
        capacity_per_building=(("lab", 1.0),),
        bound_step=step_index,
        cycles_required=cycles_required
        if cycles_required is not None
        else float(t.count),
    )


def for_launch(payload: str, count: float | None = None) -> PseudoRecipe:
    """Single launch pseudo-recipe for the given payload. `payload`
    may be empty string for a bare launch; the recipe name then
    encodes "bare" as the suffix.
    """
    ingredients: list[tuple[str, float]] = [(ROCKET_PART_ITEM, ROCKET_PARTS_PER_LAUNCH)]
    if payload:
        ingredients.append((payload, 1.0))
    suffix = payload or "bare"
    return PseudoRecipe(
        name=f"launch/{suffix}",
        kind="launch",
        time_seconds=0.0,
        ingredients=tuple(ingredients),
        outputs=((LAUNCH_EVENT_ITEM, 1.0),),
        capacity_per_building=(("rocket-silo", 1.0),),
        cycles_required=(float(count) if count is not None else None),
    )


def for_burn(fuel_name: str, model: GameModel) -> PseudoRecipe | None:
    """Boiler-engine burn pseudo-recipe for one chemical fuel.
    Returns None if the boiler doesn't exist, the item isn't a
    chemical fuel, or the fuel is in FUEL_EXCLUDED.
    """
    boiler = model.buildings.get("boiler")
    if boiler is None or "chemical" not in boiler.fuel_categories:
        return None
    item = model.items.get(fuel_name)
    if item is None or item.fuel_category != "chemical":
        return None
    if not item.fuel_value_j or item.fuel_value_j <= 0:
        return None
    if fuel_name in FUEL_EXCLUDED:
        return None
    t = BOILER_CYCLE_SECONDS
    j_per_cycle = BOILER_POWER_W * t
    water_per_cycle = BOILER_WATER_PER_SECOND * t
    fuel_per_cycle = j_per_cycle / item.fuel_value_j
    return PseudoRecipe(
        name=f"power/burn-{fuel_name}-in-boiler",
        kind="burn",
        time_seconds=t,
        ingredients=(
            (fuel_name, fuel_per_cycle),
            ("water", water_per_cycle),
        ),
        outputs=(),
        capacity_per_building=(
            ("boiler", 1.0),
            ("steam-engine", BOILER_STEAM_ENGINES_PER_BOILER),
        ),
        electric_output_j_per_cycle=j_per_cycle,
    )


def lookup(name: str, model: GameModel) -> PseudoRecipe | None:
    """Resolve a pseudo-recipe name (as it appears in L2 YAML
    `activity[].recipe`) to its stoichiometry. Returns None for
    real recipes — caller should check `model.recipes` first.

    Used by L3 to populate flow-graph edges for pseudo-recipes.
    """
    if name.startswith("research/"):
        return for_research(name[len("research/") :], model)
    if name.startswith("launch/"):
        suffix = name[len("launch/") :]
        payload = "" if suffix == "bare" else suffix
        return for_launch(payload)
    if name.startswith("power/burn-") and name.endswith("-in-boiler"):
        fuel = name[len("power/burn-") : -len("-in-boiler")]
        return for_burn(fuel, model)
    return None
