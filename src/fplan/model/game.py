"""Recipe-centric, cleaned in-memory model of Factorio game data, ready for
LP / constraint modeling.

This is the *clean* layer: it turns the raw `GameData` (from `fplan.model.data`)
into typed `Item`/`Recipe`/`Building`/`GameModel` records, performing the data
cleaning the rest of fplan relies on.

Design principles:

- **Recipe is the native unit.** Items are labels that recipes happen to
  output. A recipe-centric model handles multi-output (oil refining),
  multi-route (petroleum-gas from 4 recipes), and probabilistic
  (uranium-processing → fractional outputs) cases uniformly.
- **Crafting, mining, and pumping are all Recipes.** They differ only in
  which Buildings can run them (via `category` matching). Steady-state
  LP downstream doesn't care which kind it is.
- **Buildings are the catalog of physical production sources.** Each has
  primitives (speed, power, category compatibility). For now, modules
  and beacons are NOT modeled — `Facility` exists as a wrapper so the
  module-aware code can slot in later without disturbing the LP layer.
- **All numeric values are floats** (no integer scaling). Probabilistic
  outputs collapse to their expected fractional values.
- **Items are thin metadata.** Cross-references (which recipes produce
  X, which consume X, which techs unlock production routes) are computed
  via helper methods on GameModel rather than stored on Item — keeps
  data immutable and avoids stale-index bugs.

Energy convention:
- Power values are in watts (W). `base_power_w` on a Building is the
  drain when running at full uptime.
- Energy values are in joules (J). `fuel_value_j` on an Item is the
  energy released when burned in a burner energy source.
- Heat capacity is in J / fluid-unit / degree-C.

OUT OF SCOPE (deliberate gaps to be added later):
- Modules and beacons (Facility infrastructure is in place; the
  apply_modules math is a TODO).
- Deployment overhead on a Facility (belts/poles/footprint share). The
  `Facility` fields exist; the L2 migration reintroduces the per-building
  deployment overlay in `make_facility` (see the note there).
- Pollution / emissions accounting.
- Heat network (heat pipes, nuclear reactor neighbour bonus).
- Lab researching dynamics (consumes science packs differently from a
  normal recipe; treat separately downstream).
- Rocket-silo specialized launch dynamics.
- Power-network balance (steam engine output, solar profile, accumulator
  state-of-charge) — the LP layer composes those from Building primitives.
- Burner fuel item consumption per second (derivable from
  building.base_power_w / item.fuel_value_j).
- 2.0 Space Age fields (quality, surfaces, asteroids, planets).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from fplan.model.data import GameData, Technology, load

# ---------------------------------------------------------------------------
# SI unit parsing
# ---------------------------------------------------------------------------

_SI = {"": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
_ENERGY_RE = re.compile(r"^\s*([0-9.eE+\-]+)\s*([kKMGT]?)\s*(W|J)\s*$")


def parse_energy(s) -> float | None:
    """Parse "90kW", "4MJ" etc. to bare numeric magnitude (W or J).
    Returns None for missing/unparseable input."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = _ENERGY_RE.match(str(s))
    if not m:
        return None
    return float(m.group(1)) * _SI[m.group(2)]


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stack:
    """An (item, amount) pair. Used in recipe ingredients and outputs.
    `kind` distinguishes items from fluids."""

    name: str
    amount: float
    kind: str = "item"  # "item" | "fluid"


@dataclass
class Recipe:
    """A process that consumes ingredients and produces outputs over time.

    Includes vanilla crafting recipes, synthetic mining recipes (one per
    mineable resource), and synthetic pumping recipes (one per offshore
    pump). Distinguished by `kind`; matched to Buildings by `category`.
    """

    name: str
    kind: str  # "crafting" | "mining" | "pumping"
    category: str  # matched against Building.categories
    time_seconds: float
    ingredients: list[Stack]
    outputs: list[Stack]  # probability/range already collapsed
    enabled_at_start: bool
    unlocking_techs: list[str]  # any tech in this list enables the recipe


@dataclass
class Building:
    """A physical production source. Catalog entry — not yet a runnable
    unit (see Facility for that)."""

    name: str
    kind: str  # "assembling-machine" | "furnace" |
    # "mining-drill" | "boiler" | "generator" |
    # "solar-panel" | "accumulator" | "reactor" |
    # "lab" | "offshore-pump"
    base_speed: float  # speed multiplier on Recipe.time_seconds
    base_power_w: float  # drain at full uptime; positive for
    # consumers, 0 for fuel-less generators.
    # For burner buildings this is the
    # *fuel-energy* draw — divide by an
    # item's fuel_value_j to get items/sec
    # (e.g., stone-furnace at 90 kW burning
    # coal at 4 MJ = 0.0225 coal/sec).
    categories: tuple[str, ...]  # Recipe categories this building accepts
    # (unified: crafting + resource categories)
    module_slots: int = 0
    energy_source_type: str = (
        "void"  # "electric" | "burner" | "fluid" | "heat" | "void"
    )
    fuel_categories: tuple[str, ...] = ()  # burner only
    # Power-infrastructure-specific primitives. Zero/empty when irrelevant.
    fluid_usage_per_sec: float = 0.0  # generator: input fluid (steam) per sec
    maximum_temperature: float = 0.0  # generator: input temp cap
    target_temperature: float = 0.0  # boiler: output steam temperature
    production_w: float = 0.0  # solar-panel: max output
    buffer_capacity_j: float = 0.0  # accumulator: energy storage
    effectivity: float = 1.0  # generator conversion efficiency
    pumped_fluid: str | None = None  # offshore-pump
    pumped_fluid_per_sec: float = 0.0  # offshore-pump
    science_inputs: tuple[str, ...] = ()  # lab
    # Bare-tile footprint, derived from the prototype's selection_box (or
    # collision_box) — ceil(width) × ceil(height). The "physical-only"
    # number, BEFORE deployment-stage infra share. Drives the L2 total-
    # area constraint for every building, even ones without a custom
    # deployment pattern. 0.0 if the prototype lacked both boxes.
    base_tile_footprint: float = 0.0


@dataclass(frozen=True)
class Facility:
    """A Building configuration (building + modules + deployment overhead)
    with the effective values pre-computed.

    NOTE: module support is not yet implemented. For now `modules` is
    always empty and the effective values equal the building's base
    values. Deployment infrastructure (belts, poles, …) and a refined
    tile footprint are an L2-stage concern; until that migrates, the
    footprint is the bare prototype footprint and infrastructure is empty
    (see `make_facility`).
    """

    building: str  # Building.name
    modules: tuple[str, ...]  # empty for now
    speed: float  # effective speed
    productivity: float  # 1.0 + bonus (1.0 today)
    power_w: float  # effective drain
    categories: tuple[str, ...]  # inherited for category lookups
    # Persistent items deployed alongside each instance (NOT per-cycle).
    # The LP reserves these against item flow: cumulative item production
    # must cover cumulative facility deployment × per-instance share.
    infrastructure_items: dict[str, float] = field(default_factory=dict)
    # Total tiles claimed by ONE deployed instance, including its share
    # of belts/poles/output gap. Drives the L2 spatial cap against the
    # resource tile pool from L3 map data.
    tile_footprint: float = 0.0

    def __hash__(self) -> int:
        # dict is unhashable; key on the building+modules tuple instead.
        return hash((self.building, self.modules))


@dataclass
class RecipeRun:
    """One recipe being run in one facility. The LP variable target.
    All rates are per ONE such facility at full uptime."""

    recipe: str
    facility: Facility
    time_seconds: float  # effective cycle time
    inputs_per_sec: dict[str, float]
    outputs_per_sec: dict[str, float]
    power_w: float


@dataclass
class Item:
    """Thin metadata wrapper. Cross-references live on GameModel as
    methods, not as fields here, so the index can never go stale."""

    name: str
    kind: str  # "item" | "fluid"
    stack_size: int | None = None
    fuel_value_j: float | None = None
    fuel_category: str | None = None
    heat_capacity_j_per_unit_per_deg: float | None = None
    default_temperature: float | None = None
    max_temperature: float | None = None
    # For container items (chests): the entity's stack-slot capacity, read
    # from the `container` prototype's inventory_size. None for non-storage
    # items. Lets buffer constraints size storage from game data.
    inventory_size: int | None = None


# ---------------------------------------------------------------------------
# Helpers for normalizing the heterogeneous raw shapes
# ---------------------------------------------------------------------------


def _as_list(x) -> list:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        try:
            return [x[k] for k in sorted(x.keys())]
        except TypeError:
            return list(x.values())
    return [x]


def _merge_normal(rec_raw: dict) -> dict:
    """1.1 recipes can split into normal/expensive sub-tables. Prefer normal."""
    out = dict(rec_raw)
    normal = out.pop("normal", None)
    out.pop("expensive", None)
    if isinstance(normal, dict):
        out.update(normal)
    return out


def _expected_amount(entry) -> float:
    if isinstance(entry, dict):
        if "amount" in entry:
            base = float(entry["amount"])
        elif "amount_min" in entry and "amount_max" in entry:
            base = (float(entry["amount_min"]) + float(entry["amount_max"])) / 2
        else:
            base = 1.0
        return base * float(entry.get("probability", 1.0))
    if isinstance(entry, list):
        return float(entry[1]) if len(entry) > 1 else 1.0
    return 1.0


def _stack_from_ingredient(entry) -> Stack:
    if isinstance(entry, list):
        return Stack(name=entry[0], amount=float(entry[1] if len(entry) > 1 else 1))
    return Stack(
        name=entry.get("name"),
        amount=float(entry.get("amount", 1)),
        kind=entry.get("type", "item"),
    )


def _stack_from_result(entry) -> Stack:
    if isinstance(entry, list):
        return Stack(name=entry[0], amount=float(entry[1] if len(entry) > 1 else 1))
    return Stack(
        name=entry.get("name"),
        amount=_expected_amount(entry),
        kind=entry.get("type", "item"),
    )


def _recipe_outputs_from_raw(rec: dict) -> list[Stack]:
    if rec.get("results"):
        return [_stack_from_result(e) for e in _as_list(rec["results"])]
    if "result" in rec:
        return [Stack(name=rec["result"], amount=float(rec.get("result_count", 1)))]
    return []


# ---------------------------------------------------------------------------
# Building extraction
# ---------------------------------------------------------------------------


def _module_slots(ent: dict) -> int:
    return int((ent.get("module_specification") or {}).get("module_slots", 0))


def _tile_footprint(ent: dict) -> float:
    """Bare-tile footprint from selection_box (preferred — already grid-aligned)
    or collision_box (ceil()'d to grid). Both are [[x1, y1], [x2, y2]]. Returns
    0.0 if the prototype defines neither.
    """
    import math

    box = ent.get("selection_box") or ent.get("collision_box")
    if not box:
        return 0.0
    try:
        (x1, y1), (x2, y2) = box[0], box[1]
        w = math.ceil(float(x2) - float(x1))
        h = math.ceil(float(y2) - float(y1))
        return float(w * h)
    except (TypeError, ValueError, IndexError):
        return 0.0


def _fuel_categories(es: dict) -> tuple[str, ...]:
    if es.get("fuel_category"):
        return (es["fuel_category"],)
    if es.get("fuel_categories"):
        return tuple(_as_list(es["fuel_categories"]))
    return ()


def _build_crafter(name, ent, kind) -> Building:
    es = ent.get("energy_source") or {}
    return Building(
        name=name,
        kind=kind,
        base_speed=float(ent.get("crafting_speed", 1.0)),
        base_power_w=parse_energy(ent.get("energy_usage")) or 0.0,
        categories=tuple(_as_list(ent.get("crafting_categories"))),
        module_slots=_module_slots(ent),
        energy_source_type=es.get("type", "void"),
        fuel_categories=_fuel_categories(es),
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_drill(name, ent) -> Building:
    es = ent.get("energy_source") or {}
    return Building(
        name=name,
        kind="mining-drill",
        base_speed=float(ent.get("mining_speed", 1.0)),
        base_power_w=parse_energy(ent.get("energy_usage")) or 0.0,
        categories=tuple(_as_list(ent.get("resource_categories"))),
        module_slots=_module_slots(ent),
        energy_source_type=es.get("type", "void"),
        fuel_categories=_fuel_categories(es),
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_boiler(name, ent) -> Building:
    es = ent.get("energy_source") or {}
    consumption = parse_energy(ent.get("energy_consumption")) or 0.0
    return Building(
        name=name,
        kind="boiler",
        base_speed=1.0,
        base_power_w=consumption,
        categories=(),
        energy_source_type=es.get("type", "void"),
        fuel_categories=_fuel_categories(es),
        target_temperature=float(ent.get("target_temperature", 0)),
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_generator(name, ent) -> Building:
    # Generators' power OUTPUT is derived, not stored. The formula is:
    #   power_w = fluid_usage_per_sec * heat_cap_J_per_unit_per_deg
    #             * (input_temp - default_temp) * effectivity
    # where heat_cap and default_temp come from the input fluid (e.g.,
    # `items["steam"]`). For steam-engine on 165°C steam this works out
    # to 30 * 200 * 150 * 1.0 = 900_000 W. Downstream code that needs
    # the output should compute it from these primitives.
    return Building(
        name=name,
        kind="generator",
        base_speed=1.0,
        base_power_w=0.0,  # generates, doesn't consume electrically
        categories=(),
        energy_source_type="fluid",
        fluid_usage_per_sec=float(ent.get("fluid_usage_per_tick", 0)) * 60,
        maximum_temperature=float(ent.get("maximum_temperature", 0)),
        effectivity=float(ent.get("effectivity", 1.0)),
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_solar(name, ent) -> Building:
    return Building(
        name=name,
        kind="solar-panel",
        base_speed=1.0,
        base_power_w=0.0,
        categories=(),
        energy_source_type="electric",
        production_w=parse_energy(ent.get("production")) or 0.0,
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_accumulator(name, ent) -> Building:
    es = ent.get("energy_source") or {}
    return Building(
        name=name,
        kind="accumulator",
        base_speed=1.0,
        base_power_w=0.0,
        categories=(),
        energy_source_type="electric",
        buffer_capacity_j=parse_energy(es.get("buffer_capacity")) or 0.0,
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_reactor(name, ent) -> Building:
    es = ent.get("energy_source") or {}
    consumption = parse_energy(ent.get("consumption")) or 0.0
    return Building(
        name=name,
        kind="reactor",
        base_speed=1.0,
        base_power_w=consumption,
        categories=(),
        energy_source_type="burner",
        fuel_categories=(es.get("fuel_category", "nuclear"),),
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_lab(name, ent) -> Building:
    es = ent.get("energy_source") or {}
    return Building(
        name=name,
        kind="lab",
        base_speed=float(ent.get("researching_speed", 1.0)),
        base_power_w=parse_energy(ent.get("energy_usage")) or 0.0,
        categories=(),
        module_slots=_module_slots(ent),
        energy_source_type=es.get("type", "void"),
        science_inputs=tuple(_as_list(ent.get("inputs"))),
        base_tile_footprint=_tile_footprint(ent),
    )


def _build_offshore_pump(name, ent) -> Building:
    fluid = ent.get("fluid")
    rate = float(ent.get("pumping_speed", 0)) * 60  # per-tick → per-second
    # Pumps run "pumping" recipes; we encode that via a unique category per
    # pump (so each pump only ever drives its own synthetic recipe).
    cat = f"pumping/{name}"
    return Building(
        name=name,
        kind="offshore-pump",
        base_speed=1.0,
        base_power_w=0.0,
        categories=(cat,),
        energy_source_type="void",
        pumped_fluid=fluid,
        pumped_fluid_per_sec=rate,
        base_tile_footprint=_tile_footprint(ent),
    )


_BUILDING_BUILDERS = {
    "assembling-machine": lambda n, e: _build_crafter(n, e, "assembling-machine"),
    "furnace": lambda n, e: _build_crafter(n, e, "furnace"),
    # rocket-silo is its own entity type but behaves as a fixed-recipe
    # crafter (`crafting_categories=['rocket-building']`, electric
    # energy source, `crafting_speed=1`, `fixed_recipe='rocket-part'`).
    # Without this entry the `rocket-part` recipe has no host building
    # and any goal requiring a launch becomes structurally infeasible.
    "rocket-silo": lambda n, e: _build_crafter(n, e, "rocket-silo"),
    "mining-drill": _build_drill,
    "boiler": _build_boiler,
    "generator": _build_generator,
    "solar-panel": _build_solar,
    "accumulator": _build_accumulator,
    "reactor": _build_reactor,
    "lab": _build_lab,
    "offshore-pump": _build_offshore_pump,
}


def _extract_buildings(raw: dict) -> dict[str, Building]:
    out: dict[str, Building] = {}
    for raw_type, builder in _BUILDING_BUILDERS.items():
        for name, ent in (raw.get(raw_type) or {}).items():
            out[name] = builder(name, ent)
    return out


# ---------------------------------------------------------------------------
# Recipe extraction — crafting, mining (synthetic), pumping (synthetic)
# ---------------------------------------------------------------------------


def _crafting_recipes(raw: dict, techs: dict[str, Technology]) -> dict[str, Recipe]:
    recipe_to_techs: dict[str, list[str]] = {}
    for tname, t in techs.items():
        for r in t.unlocks_recipes:
            recipe_to_techs.setdefault(r, []).append(tname)

    out: dict[str, Recipe] = {}
    for name, raw_rec in (raw.get("recipe") or {}).items():
        rec = _merge_normal(raw_rec)
        category = rec.get("category", "crafting")
        # `enabled` defaults to true in Factorio if omitted.
        enabled = rec.get("enabled", True)
        if enabled is None:
            enabled = True
        out[name] = Recipe(
            name=name,
            kind="crafting",
            category=category,
            time_seconds=float(rec.get("energy_required", 0.5)),
            ingredients=[
                _stack_from_ingredient(e) for e in _as_list(rec.get("ingredients"))
            ],
            outputs=_recipe_outputs_from_raw(rec),
            enabled_at_start=bool(enabled),
            unlocking_techs=sorted(recipe_to_techs.get(name, [])),
        )
    return out


def _mining_recipes(raw: dict) -> dict[str, Recipe]:
    """Synthesize one Recipe per mineable resource.

    Two caveats worth knowing:

    1. **Pumpjack on crude-oil** yields 10 crude-oil/s only at 100% patch
       yield. Resource patches deplete over time toward a 20% floor.
       The Recipe records the max rate; downstream code that simulates
       a long horizon should multiply by an expected yield factor.

    2. **Uranium-ore requires sulfuric-acid** as a fluid ingredient.
       This is captured here (recipe gets the fluid in `ingredients`),
       BUT `GameModel.buildings_for(recipe)` matches purely by
       category — so burner-mining-drill will appear as a candidate
       despite having no fluid input. Filter to drills with a fluid
       input box if the recipe has fluid ingredients.
    """
    out: dict[str, Recipe] = {}
    for name, ent in (raw.get("resource") or {}).items():
        minable = ent.get("minable") or {}
        if not minable:
            continue
        category = ent.get("category", "basic-solid")
        ingredients: list[Stack] = []
        if minable.get("required_fluid"):
            ingredients.append(
                Stack(
                    name=minable["required_fluid"],
                    amount=float(minable.get("fluid_amount", 0)),
                    kind="fluid",
                )
            )
        out[f"mine/{name}"] = Recipe(
            name=f"mine/{name}",
            kind="mining",
            category=category,
            time_seconds=float(minable.get("mining_time", 1.0)),
            ingredients=ingredients,
            outputs=_recipe_outputs_from_raw(minable),
            # Mining isn't directly tech-gated — the drill is. We treat
            # the recipe as start-enabled because vanilla starts with the
            # burner-mining-drill recipe unlocked.
            enabled_at_start=True,
            unlocking_techs=[],
        )
    return out


def _pumping_recipes(buildings: dict[str, Building]) -> dict[str, Recipe]:
    out: dict[str, Recipe] = {}
    for b in buildings.values():
        if b.kind != "offshore-pump" or not b.pumped_fluid:
            continue
        # 1-second synthetic cycle: per second one pump produces
        # pumped_fluid_per_sec units of fluid.
        out[f"pump/{b.name}"] = Recipe(
            name=f"pump/{b.name}",
            kind="pumping",
            category=f"pumping/{b.name}",
            time_seconds=1.0,
            ingredients=[],
            outputs=[
                Stack(name=b.pumped_fluid, amount=b.pumped_fluid_per_sec, kind="fluid")
            ],
            enabled_at_start=True,
            unlocking_techs=[],
        )
    return out


# ---------------------------------------------------------------------------
# Item extraction
# ---------------------------------------------------------------------------

_ITEM_TYPES = (
    "item",
    "ammo",
    "gun",
    "capsule",
    "tool",
    "armor",
    "repair-tool",
    "mining-tool",
    "item-with-entity-data",
    "rail-planner",
    "item-with-inventory",
    "item-with-label",
    "item-with-tags",
    "selection-tool",
    "blueprint",
    "blueprint-book",
    "module",
)


def _extract_items(raw: dict) -> dict[str, Item]:
    items: dict[str, Item] = {}
    for raw_type in _ITEM_TYPES:
        for name, ent in (raw.get(raw_type) or {}).items():
            items[name] = Item(
                name=name,
                kind="item",
                stack_size=ent.get("stack_size"),
                fuel_value_j=parse_energy(ent.get("fuel_value")),
                fuel_category=ent.get("fuel_category"),
            )
    for name, ent in (raw.get("fluid") or {}).items():
        items[name] = Item(
            name=name,
            kind="fluid",
            heat_capacity_j_per_unit_per_deg=parse_energy(ent.get("heat_capacity")),
            default_temperature=ent.get("default_temperature"),
            max_temperature=ent.get("max_temperature"),
            fuel_value_j=parse_energy(ent.get("fuel_value")),
        )
    # Chests carry their stack-slot capacity on the `container` entity
    # prototype (inventory_size), not the item; attach it to the matching
    # item so buffer constraints can size storage from game data.
    for name, ent in (raw.get("container") or {}).items():
        if name in items and ent.get("inventory_size") is not None:
            items[name].inventory_size = int(ent["inventory_size"])
    return items


# ---------------------------------------------------------------------------
# GameModel — top-level container with lazy cross-reference helpers
# ---------------------------------------------------------------------------


@dataclass
class GameModel:
    items: dict[str, Item]
    recipes: dict[str, Recipe]
    buildings: dict[str, Building]
    technologies: dict[str, Technology]

    # --- Cross-reference helpers (computed on demand, no stored index) ---

    def recipes_producing(self, item_name: str) -> list[Recipe]:
        return [
            r
            for r in self.recipes.values()
            if any(o.name == item_name for o in r.outputs)
        ]

    def recipes_consuming(self, item_name: str) -> list[Recipe]:
        return [
            r
            for r in self.recipes.values()
            if any(i.name == item_name for i in r.ingredients)
        ]

    def buildings_for(self, recipe: Recipe) -> list[Building]:
        """Buildings whose categories include this recipe's category.

        NOTE: this is purely category-based — it does NOT filter out
        buildings that can't physically accept the recipe's fluid input.
        For example, `mine/uranium-ore` requires sulfuric-acid, but a
        burner-mining-drill (no fluid box) will still be returned. If
        your recipe has fluid ingredients, additionally restrict to
        buildings with an input fluid box.
        """
        return [b for b in self.buildings.values() if recipe.category in b.categories]

    def available_recipes(self, researched: set[str]) -> list[Recipe]:
        """Recipes that are unlocked given a set of researched techs.
        A recipe is available iff it's enabled at start OR any of its
        unlocking techs is in the set."""
        return [
            r
            for r in self.recipes.values()
            if r.enabled_at_start or any(t in researched for t in r.unlocking_techs)
        ]

    def unlocking_techs_for(self, item_name: str) -> set[str]:
        """Union of unlocking techs across all recipes that produce this
        item. Empty if any producing recipe is enabled at start."""
        techs: set[str] = set()
        any_start = False
        for r in self.recipes_producing(item_name):
            if r.enabled_at_start:
                any_start = True
            techs.update(r.unlocking_techs)
        return set() if any_start else techs

    def total_recipe_seconds(self, item_name: str) -> float:
        """Cumulative crafting seconds per unit of item, summed over all
        upstream recipes.

        For each producing recipe, attributes recipe time across ALL outputs
        equally (multi-output recipes — oil refining — share their time
        proportionally per unit produced). For items with multiple producing
        recipes, picks the cheapest path. Cycles (Kovarex) are skipped: the
        cyclic branch returns infinity so `min()` selects a non-cyclic path.

        Items with no producing recipe (e.g., wood treated as initial-only)
        return 0.0.
        """
        cache: dict[str, float] = self.__dict__.setdefault("_recipe_seconds_cache", {})
        return self._recipe_seconds(item_name, frozenset(), cache)

    def _recipe_seconds(
        self, item_name: str, visiting: frozenset[str], cache: dict[str, float]
    ) -> float:
        if item_name in cache:
            return cache[item_name]
        if item_name in visiting:
            return float("inf")
        visiting_now = visiting | {item_name}
        candidates: list[float] = []
        for r in self.recipes_producing(item_name):
            total_out = sum(s.amount for s in r.outputs if s.amount > 0)
            if total_out <= 0:
                continue
            ing_total = 0.0
            cyclic = False
            for ing in r.ingredients:
                c = self._recipe_seconds(ing.name, visiting_now, cache)
                if c == float("inf"):
                    cyclic = True
                    break
                ing_total += ing.amount * c
            if cyclic:
                continue
            per_unit = (r.time_seconds + ing_total) / total_out
            candidates.append(per_unit)
        result = min(candidates) if candidates else 0.0
        cache[item_name] = result
        return result

    # --- Materialization: Building -> Facility -> RecipeRun ---

    def make_facility(
        self, building: Building, modules: tuple[str, ...] = ()
    ) -> Facility:
        """Build a Facility from a Building configuration.

        TODO: when module support lands, apply speed/productivity/consumption
        effects here. The current implementation copies base values verbatim.

        Deployment overhead (infrastructure_items + a refined tile_footprint)
        is an L2-stage overlay; until that migrates, the footprint is the bare
        prototype footprint and infrastructure is empty. The L2 migration
        reintroduces the per-building deployment lookup here.
        """
        if modules:
            raise NotImplementedError(
                "Module effects are not yet modeled; "
                "Facility currently only supports modules=()."
            )
        return Facility(
            building=building.name,
            modules=(),
            speed=building.base_speed,
            productivity=1.0,
            power_w=building.base_power_w,
            categories=building.categories,
            infrastructure_items={},
            tile_footprint=building.base_tile_footprint,
        )

    def run(self, recipe: Recipe, facility: Facility) -> RecipeRun:
        """Materialize a (recipe, facility) pairing into per-second rates.

        Raises if the facility's categories don't include the recipe's
        category — catch mistakes early.
        """
        if recipe.category not in facility.categories:
            raise ValueError(
                f"facility {facility.building!r} cannot run recipe "
                f"{recipe.name!r}: category {recipe.category!r} "
                f"not in {facility.categories}"
            )
        cycle_time = recipe.time_seconds / facility.speed
        inputs_per_sec = {
            ing.name: ing.amount / cycle_time for ing in recipe.ingredients
        }
        outputs_per_sec = {
            o.name: o.amount * facility.productivity / cycle_time
            for o in recipe.outputs
        }
        return RecipeRun(
            recipe=recipe.name,
            facility=facility,
            time_seconds=cycle_time,
            inputs_per_sec=inputs_per_sec,
            outputs_per_sec=outputs_per_sec,
            power_w=facility.power_w,
        )


# ---------------------------------------------------------------------------
# Rocket-silo module hack
# ---------------------------------------------------------------------------
# Deliberate game-data post-process (NOT a canonical fact); a stand-in until
# Facility.apply_modules lands. Models the silo's 4 module slots filled with
# productivity-module-1, plus 20 beacons (2 speed-module-1 each) ringing it,
# transmitted at the beacon's 0.5 distribution_effectivity. Module effects are
# from the game data — speed-module: +0.2 speed / +0.5 consumption;
# productivity-module: +0.04 productivity / -0.05 speed / +0.4 consumption;
# beacon: 480kW, 0.5 effectivity. Resulting silo bonuses:
#   speed       = 4·(-0.05) + 40·0.2·0.5  = +3.80  -> ×4.80 crafting speed
#   productivity= 4·0.04                  = +0.16  -> ×1.16 rocket-part output
#   consumption = 4·0.4 + 40·0.5·0.5      = +11.6  -> silo 250kW -> 3.15MW
#   beacon power= 20·480kW                = 9.6MW  -> rig total 12.75MW
# Productivity is bonus OUTPUT per craft (not faster crafts), so it scales the
# rocket-part recipe output rather than the silo speed. The modules' own
# production cost is NOT modeled here — it's required via the scenario's
# items_produced / checkpoint (40 speed-module + 4 productivity-module).
SILO_PROD_MODULES = 4
SILO_BEACON_COUNT = 20
BEACON_SPEED_MODULES_EACH = 2
BEACON_EFFECTIVITY = 0.5
BEACON_POWER_W = 480_000.0
_SPEED_MOD = {"speed": 0.2, "consumption": 0.5}
_PROD_MOD = {"productivity": 0.04, "speed": -0.05, "consumption": 0.4}


def _apply_rocket_silo_modules(
    buildings: dict[str, Building], recipes: dict[str, Recipe]
) -> None:
    silo = buildings.get("rocket-silo")
    if silo is not None:
        n_beacon_mods = SILO_BEACON_COUNT * BEACON_SPEED_MODULES_EACH
        speed_bonus = (
            SILO_PROD_MODULES * _PROD_MOD["speed"]
            + n_beacon_mods * _SPEED_MOD["speed"] * BEACON_EFFECTIVITY
        )
        consumption_bonus = (
            SILO_PROD_MODULES * _PROD_MOD["consumption"]
            + n_beacon_mods * _SPEED_MOD["consumption"] * BEACON_EFFECTIVITY
        )
        power = (
            silo.base_power_w * (1.0 + consumption_bonus)
            + SILO_BEACON_COUNT * BEACON_POWER_W
        )
        buildings["rocket-silo"] = replace(
            silo,
            base_speed=silo.base_speed * (1.0 + speed_bonus),
            base_power_w=power,
        )
    # Productivity = extra output per craft (not faster crafts): scale the
    # rocket-part recipe output. rocket-part is only made in the silo, so this
    # is safe globally.
    prod_bonus = SILO_PROD_MODULES * _PROD_MOD["productivity"]
    rp = recipes.get("rocket-part")
    if rp is not None and prod_bonus:
        rp.outputs = [
            replace(s, amount=s.amount * (1.0 + prod_bonus)) for s in rp.outputs
        ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_model(
    raw: GameData | None = None, *, data_dir: Path | None = None
) -> GameModel:
    """Build a cleaned `GameModel`.

    Pass ``raw`` (a `GameData`, e.g. from a fixture or a prior load) to clean it
    directly, or ``data_dir`` to load from a Factorio install first.
    """
    if raw is not None:
        g = raw
    elif data_dir is not None:
        g = load(data_dir)  # pragma: no cover - integration (runs Lua over game data)
    else:
        raise ValueError("load_model() requires either raw= or data_dir=")
    buildings = _extract_buildings(g.raw)
    recipes = _crafting_recipes(g.raw, g.technologies)
    recipes.update(_mining_recipes(g.raw))
    recipes.update(_pumping_recipes(buildings))
    _apply_rocket_silo_modules(buildings, recipes)
    items = _extract_items(g.raw)
    return GameModel(
        items=items,
        recipes=recipes,
        buildings=buildings,
        technologies=g.technologies,
    )
