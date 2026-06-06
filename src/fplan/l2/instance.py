"""L2 — production phases (solver-neutral instance builder).

Reads (scenario, L1 output) and constructs an L2Instance: a linearized
per-tech timestep skeleton with pseudo-recipes for tech research and
rocket launches, plus the derived final-state floors. Does NOT build the
SCIP model — its job is to produce the structure the solver
(:mod:`fplan.l2.solve`) consumes, and to surface structural issues (items
without producers, trigger-based techs, etc.) early. Driven by the CLI
(`fplan rates solve <run>`), which loads the run manifest and the tuning
config, then calls `build_instance`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from fplan import scenario as scenario_mod
from fplan.l2 import deployment as deployment_mod

# Pseudo-recipe definitions live in a shared module so L3 can read
# the same stoichiometry. Re-exported here so existing
# `from l2_phases import PseudoRecipe` callers keep working.
from fplan.l2 import pseudo_recipes
from fplan.l2.config import L2Config, load_config
from fplan.l2.pseudo_recipes import (
    LAUNCH_EVENT_ITEM,
    PseudoRecipe,
)
from fplan.model import Building, Facility, GameModel, Recipe, load_model

# Planning-mode identifiers. The mode NAMES are structural; the per-mode
# end-of-step capacity WEIGHTS and bootstrap SEEDING — along with the character
# stand-in, the policy sets, the caps, and the player-physics constants that
# used to live here — are now tunable values in the L2 config
# (fplan.l2.config / resources/l2-defaults.yaml). See L2Instance.cfg,
# L2Instance.capacity_end_weight(), and L2Instance.deployed_facility().
MODE_LOWER_BOUND = "lower-bound"
MODE_EXPERIMENTAL = "experimental"
MODE_TRAPEZOIDAL = "trapezoidal"
MODES = (MODE_LOWER_BOUND, MODE_EXPERIMENTAL, MODE_TRAPEZOIDAL)

# FUEL_EXCLUDED, BOILER_*, and PseudoRecipe moved to `pseudo_recipes.py`
# (shared with L3). Re-exported via the top-of-file import.


@dataclass(frozen=True)
class L2Step:
    index: int
    research_tech: str | None  # None for the trailing FINAL step
    techs_researched_at_start: frozenset[str]
    techs_researched_at_end: frozenset[str]
    research: PseudoRecipe | None  # None for FINAL or trigger-based
    # Buildings whose crafting recipe is unlocked by techs at start of
    # step, OR present in initial_state.items. Initial-state buildings
    # bypass tech-gating — e.g., the 2 AM1s standing in for the
    # hand-crafting character work from step 0 even before `automation`
    # is researched.
    available_buildings_at_start: frozenset[str] = frozenset()
    # Real recipes forbidden from running in this step, even if their
    # tech and host building are available. Populated by `before_recipe`
    # checkpoints: the named recipe is forbidden in every step where it
    # was previously allowed, *except* the new step the checkpoint
    # carves out. Empty (default) means "no recipe-level restriction."
    forbidden_real_recipes: frozenset[str] = frozenset()
    # Human-readable label for the step. If None, the YAML emitter
    # falls back to `research_tech or "FINAL"`. A `before_recipe`
    # checkpoint sets the label on the step *preceding* the recipe's
    # home (`before_recipe/<r>`); the recipe's home is the genuine last
    # step and keeps label=None so it shows as FINAL.
    label: str | None = None

    def available_recipes(self, model: GameModel) -> list[Recipe]:
        return model.available_recipes(set(self.techs_researched_at_start))

    def recipe_building_pairs(
        self,
        model: GameModel,
        reachable_buildings: frozenset[str] | None = None,
    ) -> list[tuple[str, str]]:
        """(recipe.name, building.name) pairs valid in this step.

        Recipes are strict-gated on techs at start of step. Buildings
        are gated by `available_buildings_at_start` (the per-step
        analogue of reachable: tech-unlocked crafting recipe or in
        initial state). `reachable_buildings` is a cheap scenario-wide
        pre-filter; redundant once `available_buildings_at_start` is
        populated but kept for safety.
        """
        pairs: list[tuple[str, str]] = []
        for r in self.available_recipes(model):
            for b in model.buildings_for(r):
                if (
                    reachable_buildings is not None
                    and b.name not in reachable_buildings
                ):
                    continue
                if (
                    self.available_buildings_at_start
                    and b.name not in self.available_buildings_at_start
                ):
                    continue
                pairs.append((r.name, b.name))
        return pairs


@dataclass(frozen=True)
class ResolvedCheckpoint:
    """A `scenario.Checkpoint` after its trigger has been resolved to a
    concrete step boundary. Carries only what the LP needs: where to
    apply it (`boundary`), and the per-item lower bounds (`items_floor`).
    Other future requires-types add fields here without breaking the LP
    construction call site.
    """

    name: str
    boundary: int
    items_floor: dict[str, float]


@dataclass
class L2Instance:
    scenario: scenario_mod.Scenario
    l1_method: str
    # Planning mode (MODE_LOWER_BOUND / MODE_EXPERIMENTAL /
    # MODE_TRAPEZOIDAL). The LP reads this to decide capacity-constraint
    # timing (strict / per-building / uniform-0.5) and whether the
    # lower-bound infrastructure seed is included.
    mode: str
    steps: tuple[L2Step, ...]
    launches: tuple[PseudoRecipe, ...]
    # Boiler-engine burn pseudo-recipes, one per allowed chemical fuel.
    # Scenario-agnostic but per-instance so the LP can treat them
    # alongside launches without re-deriving from the model.
    burns: tuple[PseudoRecipe, ...]
    final_floors: dict[str, float]
    # Intermediate-state checkpoints — see `scenario.Checkpoint`. Each
    # is `(boundary index, items lower-bound dict)`. The LP adds them
    # as the same constraint shape as `final_floors` but at the
    # checkpoint's resolved boundary instead of the final boundary.
    checkpoints: tuple[ResolvedCheckpoint, ...]
    # Effective t₀ item counts: scenario.initial.items + the character
    # stand-in (`CHARACTER_BUILDING: CHARACTER_COUNT`, summed if the
    # scenario also lists the same building). This is what the LP uses
    # as initial conditions, not scenario.initial.items directly.
    effective_initial_items: dict[str, float]
    # Buildings that could ever exist under this scenario: present in
    # effective_initial_items OR producible by some recipe whose
    # unlocking tech is in the L1 closure. Used to prune (recipe,
    # building) pairs.
    reachable_buildings: frozenset[str]
    # Items obtainable in this scenario: output by a tech-reachable recipe in
    # some step, or supplied in the initial state. Gates fuel choices (burns,
    # per-burner allocation) so unreachable fuels like nuclear-fuel don't add
    # dead variables / wide coefficients.
    producible_items: frozenset[str]
    # Items deliberately omitted from L2 flow tracking. Their consumption
    # by recipes is reported post-solve but no balance constraint is
    # enforced — effectively treated as an unlimited resource at L2.
    # Wood is here because tree placement / production lives in L3.
    excluded_items: frozenset[str]
    # Terminal items removed from the model entirely (no item var, no
    # balance, and no recipe var for recipes producing only these). See
    # `PRUNED_ITEMS`. Unlike `excluded_items`, these are NOT consumed by
    # any kept recipe, so there's nothing to report post-solve.
    pruned_items: frozenset[str]
    # Resource → total tile count on the map, from L3's map probe (see
    # `l3_map.py` and `outputs/03_<seed>_map.yaml`). Empty when no map
    # data is loaded; the LP then skips spatial caps and behaves as
    # before. Keys are resource item names: "iron-ore", "copper-ore",
    # "coal", "stone", "uranium-ore".
    tile_pool: dict[str, float]
    # Total bounded-map area in tiles (width × height from the probe's
    # map_gen_settings). 0.0 when no probe is loaded. Powers the total-
    # area constraint: Σ building × footprint ≤ fraction × map_area.
    map_area: float
    # Number of crude-oil spots on the map. Caps pumpjack count.
    # 0 when no probe is loaded; the LP then skips the pumpjack cap.
    oil_spot_count: int
    # Max offshore-pumps the map can host — pumps sit on water-body
    # perimeters, so a body of area A admits ~4·√A pumps, summed across
    # bodies. Caps offshore-pump count. 0.0 with no water_patches.
    water_pump_cap: float
    # Total wood available on the map = tree_count × WOOD_PER_TREE. The
    # plan's whole-campaign wood consumption is bounded by this. 0.0 when
    # the probe carries no tree_count; the LP then skips the wood budget.
    wood_budget: float
    # Pumpjack crude-oil output multiplier from the richest oil cluster's
    # average yield (see MapData.oil_yield_multiplier). 1.0 with no probe.
    oil_yield_multiplier: float
    # Maximum fraction of map_area the LP may fill with buildings.
    # Default tuned to 0.75; raise to let the LP pack tighter (less
    # walking space), lower to reserve more for player traversal.
    max_area_fraction: float
    # When False, the LP skips ONLY the infrastructure-item reservation
    # (Facility.infrastructure_items is ignored — belts/poles aren't
    # forced to be produced before drill deployment). Spatial caps
    # (per-resource tile pool, total-area, per-oil-spot) ALWAYS fire —
    # without them the LP's bilinear relaxation lets default-victory
    # explode at low t_FINAL and SCIP can't find a primal. The flag is
    # an A/B harness for the infra-flow side only.
    deployment_enabled: bool
    # When False, the LP skips the per-step player-time constraint
    # (walking + placing newly-built facilities and mining trees for
    # wood must fit within each step's duration). A/B harness for the
    # player-time dimension.
    player_time_enabled: bool
    warnings: tuple[str, ...]
    # The resolved L2 tuning config (packaged defaults + any user override).
    # The solver reads its knobs from here; build_instance also uses it to
    # resolve the character stand-in, mode seeding, caps, and policy sets.
    cfg: L2Config

    def capacity_end_weight(self, building_name: str = "") -> float:
        """End-of-step weight in the capacity-constraint interpolation, per
        this instance's mode and the config's per-mode weights. `building_name`
        lets `experimental` apply per-building (raw-extraction) weighting; the
        other modes ignore it."""
        if self.mode == MODE_LOWER_BOUND:
            return self.cfg.lower_bound_weight
        if self.mode == MODE_EXPERIMENTAL:
            return (
                self.cfg.experimental_raw_weight
                if building_name in self.cfg.raw_extraction_buildings
                else self.cfg.experimental_default_weight
            )
        if self.mode == MODE_TRAPEZOIDAL:
            return self.cfg.trapezoidal_weight
        raise ValueError(f"unknown mode {self.mode!r}")

    def deployed_facility(self, model: GameModel, building: Building) -> Facility:
        """Facility with this instance's deployment config overlaid (the
        L2-stage enrichment; the base model factory stays deployment-free)."""
        return deployment_mod.deployed_facility(model, building, self.cfg)

    def all_items(self, model: GameModel) -> set[str]:
        """Items that may carry a nonzero state variable somewhere."""
        seen: set[str] = set()
        seen.update(self.effective_initial_items)
        seen.update(self.final_floors)
        for step in self.steps:
            if step.research:
                seen.update(n for n, _ in step.research.ingredients)
                seen.update(n for n, _ in step.research.outputs)
            for r in step.available_recipes(model):
                seen.update(s.name for s in r.ingredients)
                seen.update(s.name for s in r.outputs)
        for L in self.launches:
            seen.update(n for n, _ in L.ingredients)
            seen.update(n for n, _ in L.outputs)
        for B in self.burns:
            seen.update(n for n, _ in B.ingredients)
            seen.update(n for n, _ in B.outputs)
        return seen


def _buildings_available_at(
    techs_at_start: frozenset[str],
    initial_building_names: frozenset[str],
    model: GameModel,
) -> frozenset[str]:
    """Buildings whose crafting recipe is unlocked by `techs_at_start`,
    PLUS anything in the initial state (which bypasses tech-gating).
    The per-step version of reachability: a building can be `x`'d
    against in step i iff this returns true for that step's start-techs.
    """
    out = set(initial_building_names)
    for b_name in model.buildings:
        if b_name in out:
            continue
        for r in model.recipes_producing(b_name):
            if r.enabled_at_start or any(
                t in techs_at_start for t in r.unlocking_techs
            ):
                out.add(b_name)
                break
    return frozenset(out)


def _compute_reachable_buildings(
    effective_initial_items: dict[str, float],
    l1_layers: list[list[str]],
    initial_techs: frozenset[str],
    model: GameModel,
) -> frozenset[str]:
    """Buildings whose crafting recipe is start-enabled OR unlocked by
    some tech in the scenario's full closure, plus anything already
    in the effective initial items (which includes the character
    stand-in). Buildings outside this set can never physically exist,
    so (recipe, building) pairs involving them are pure variable-count
    overhead.
    """
    all_techs: set[str] = set(initial_techs)
    for L in l1_layers:
        all_techs.update(L)
    initial_names = set(effective_initial_items)
    reachable: set[str] = set()
    for b_name in model.buildings:
        if b_name in initial_names:
            reachable.add(b_name)
            continue
        for r in model.recipes_producing(b_name):
            if r.enabled_at_start or any(t in all_techs for t in r.unlocking_techs):
                reachable.add(b_name)
                break
    return frozenset(reachable)


def linearize_layers(layers: list[list[str]]) -> list[str]:
    """Concatenate L1 layers into a flat per-tech sequence. L1 already
    sorts within each layer alphabetically; we preserve that order.
    Future L1 ordering methods can produce smarter within-layer
    orderings without any change here.
    """
    out: list[str] = []
    for layer in layers:
        out.extend(layer)
    return out


def _resolve_checkpoints(
    raw_checkpoints: tuple,
    steps: list[L2Step],
    model: GameModel,
    reachable_buildings: frozenset[str],
    warnings: list[str],
) -> tuple[ResolvedCheckpoint, ...]:
    """Resolve checkpoints, possibly inserting new steps into `steps`.

    `before_recipe: <r>` semantics: a new step is appended at the
    earliest position where the recipe could currently run (and any
    later position). In every step from that position onward (in the
    pre-existing list), `r` is added to `forbidden_real_recipes`. The
    new step does NOT forbid `r`, so the recipe gets a dedicated
    home there. The checkpoint then resolves to the boundary at the
    START of the new step (= its index in the steps list).

    Item-flow propagation handles downstream effects automatically:
    pseudo-recipes (e.g. launch) that consume `r` can be scheduled in
    any step, but the LP's item-balance constraints will push them
    into the new step too, because earlier steps now have zero
    production of `r`.

    Multiple `before_recipe` checkpoints naming different recipes
    each create their own carved step. (Future refinement: if their
    earliest-allowed positions match, they could share a step. v0
    keeps it simple.)

    The contract: callers pass a mutable `steps` list, and this
    function may append to it. `len(steps)` after the call is the
    new total step count.

    A checkpoint whose trigger can't be matched (no step ever allowed
    the recipe in the current scenario) is dropped with a warning,
    not an error — different scenarios can share a base file without
    every checkpoint applying.
    """
    out: list[ResolvedCheckpoint] = []

    for cp in raw_checkpoints:
        trig = cp.trigger
        if trig.kind != "before_recipe":
            warnings.append(
                f"checkpoint {cp.name!r}: trigger kind {trig.kind!r} not "
                f"recognized; checkpoint dropped"
            )
            continue
        recipe_name = trig.recipe
        recipe = model.recipes.get(recipe_name)
        if recipe is None:
            warnings.append(
                f"checkpoint {cp.name!r}: recipe {recipe_name!r} not found "
                f"in model; checkpoint dropped"
            )
            continue
        # Earliest step where this recipe is currently allowed (tech
        # gated + building gated). Resolver uses the same predicate the
        # LP would use for x_real var creation.
        first_allowed_idx: int | None = None
        for i, step in enumerate(steps):
            pairs = step.recipe_building_pairs(model, reachable_buildings)
            if any(r_name == recipe_name for r_name, _ in pairs):
                first_allowed_idx = i
                break
        if first_allowed_idx is None:
            warnings.append(
                f"checkpoint {cp.name!r}: recipe {recipe_name!r} not "
                f"allowed in any current step; checkpoint dropped"
            )
            continue
        # Forbid the recipe in every step from first_allowed onward.
        # (It was tech-gated out of earlier steps anyway, but adding it
        # there too is a no-op.)
        for i in range(first_allowed_idx, len(steps)):
            old = steps[i]
            steps[i] = L2Step(
                index=old.index,
                research_tech=old.research_tech,
                techs_researched_at_start=old.techs_researched_at_start,
                techs_researched_at_end=old.techs_researched_at_end,
                research=old.research,
                available_buildings_at_start=old.available_buildings_at_start,
                forbidden_real_recipes=old.forbidden_real_recipes | {recipe_name},
            )
        # The carved step is the recipe's dedicated home AND the genuine last
        # step of the plan (rocket-part production + launch happen here), so it
        # keeps the FINAL label (label=None → the emitter shows "FINAL"). The
        # step immediately before it is the `before_recipe/<r>` boundary — the
        # point at which the checkpoint's requirements (e.g. silo present) must
        # hold, entering the recipe's step. (Previously the carved step was
        # mislabeled `carve/<r>` and the predecessor kept "FINAL", which made the
        # displayed FINAL not the actual last step.)
        prior_last = steps[-1]
        steps[-1] = replace(prior_last, label=f"before_recipe/{recipe_name}")
        new_step = L2Step(
            index=len(steps),
            research_tech=None,
            techs_researched_at_start=prior_last.techs_researched_at_end,
            techs_researched_at_end=prior_last.techs_researched_at_end,
            research=None,
            available_buildings_at_start=prior_last.available_buildings_at_start,
            forbidden_real_recipes=frozenset(),
            label=None,
        )
        steps.append(new_step)
        boundary = new_step.index  # start of the new (FINAL) step

        items_floor: dict[str, float] = {}
        for name, count in cp.requires.items:
            items_floor[name] = max(items_floor.get(name, 0.0), float(count))
        out.append(
            ResolvedCheckpoint(
                name=cp.name,
                boundary=boundary,
                items_floor=items_floor,
            )
        )
    return tuple(out)


def _research_pseudo_recipe(
    step_index: int, tech_name: str, model: GameModel
) -> PseudoRecipe | None:
    return pseudo_recipes.for_research(tech_name, model, step_index=step_index)


def _launch_pseudo_recipes(
    rocket_launches: tuple[tuple[str, float], ...],
) -> list[PseudoRecipe]:
    return [
        pseudo_recipes.for_launch(payload, count)
        for payload, count in rocket_launches
        if count > 0
    ]


def _burn_pseudo_recipes(
    model: GameModel, producible: frozenset[str], fuel_excluded: frozenset[str]
) -> list[PseudoRecipe]:
    """Boiler-burn pseudo-recipe per chemical fuel that is actually
    obtainable in this scenario. `producible` is the set of items output by
    a tech-reachable recipe or present in the initial state. Without this
    gate, unreachable fuels (e.g. nuclear-fuel, which needs nuclear research)
    create dead burn variables AND drag a tiny ingredient coefficient
    (1.8MJ / 1.21GJ ≈ 1.5e-3) into the model, widening its coefficient range
    and worsening LP conditioning for no benefit. Mirrors the uranium-mining
    exclusion."""
    out: list[PseudoRecipe] = []
    for fuel_name in model.items:
        if fuel_name not in producible:
            continue
        # for_burn applies fuel_excluded itself (config-driven), so the
        # exclusion is authoritative whether tightened or loosened.
        pr = pseudo_recipes.for_burn(fuel_name, model, fuel_excluded)
        if pr is not None:
            out.append(pr)
    out.sort(key=lambda p: p.name)
    return out


def _producible_items(
    steps: list[L2Step],
    effective_initial_items: dict[str, float],
    model: GameModel,
) -> frozenset[str]:
    """Items obtainable in this scenario: output by some tech-reachable
    recipe in some step, or supplied in the initial state."""
    producible: set[str] = set(effective_initial_items)
    for step in steps:
        for r in step.available_recipes(model):
            for out in r.outputs:
                producible.add(out.name)
    return frozenset(producible)


@dataclass(frozen=True)
class MapData:
    """Parsed L3 map probe — what L2 cares about. Pulled out of the raw
    YAML once so the LP-construction site doesn't reach into the dump.
    """

    # Resource → total tile count, summed across patches.
    tile_pool: dict[str, float]
    # Total tiles on the bounded map (width × height). 0.0 if the probe
    # didn't carry map_gen_settings (older dumps).
    map_area: float
    # Number of crude-oil spots on the map. Caps pumpjack count
    # (one pumpjack per spot).
    oil_spot_count: int
    # Max offshore-pumps the map can host. Pumps sit on the *perimeter*
    # of a water body, so a body of area A (tiles) admits ~4·√A pumps
    # (the perimeter of the equal-area square — a conservative lower
    # bound on real perimeter). Summed across bodies. 0.0 when the probe
    # carries no water_patches (older dumps); the LP then skips the cap.
    water_pump_cap: float
    # Total wood on the map = tree_count × WOOD_PER_TREE. 0.0 when the
    # probe carries no tree_count.
    wood_budget: float
    # Pumpjack crude-oil output multiplier = max over oil clusters of the
    # cluster's average per-spot yield% / 100 (a pumpjack on a 1095%-yield
    # spot produces 10.95× the 100%-yield base). 1.0 when no oil clusters.
    oil_yield_multiplier: float


def load_map_data(path: Path | str | None, wood_per_tree: float) -> MapData:
    """Parse an L3 map probe (`outputs/03_<seed>_map.yaml`). Returns
    `MapData` with all-zero / empty fields if `path` is None or missing.
    """
    empty = MapData(
        tile_pool={},
        map_area=0.0,
        oil_spot_count=0,
        water_pump_cap=0.0,
        wood_budget=0.0,
        oil_yield_multiplier=1.0,
    )
    if path is None:
        return empty
    path = Path(path)
    if not path.exists():
        return empty
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):  # empty or scalar map file → treat as no probe
        return empty
    pool: dict[str, float] = {}
    for p in data.get("patches") or []:
        name = p.get("resource")
        if name is None:
            continue
        pool[name] = pool.get(name, 0.0) + float(p.get("tile_count", 0))
    mgs = data.get("map_gen_settings") or {}
    map_area = float(mgs.get("width", 0)) * float(mgs.get("height", 0))
    oil_spots = data.get("oil_spots") or []
    water_pump_cap = sum(
        4.0 * math.sqrt(float(p.get("tile_count", 0)))
        for p in (data.get("water_patches") or [])
    )
    wood_budget = float(data.get("tree_count", 0) or 0) * wood_per_tree
    clusters = data.get("oil_clusters") or []
    cluster_avgs = [
        float(c["total_yield_pct"]) / c["spot_count"] / 100.0
        for c in clusters
        if c.get("spot_count") and c.get("total_yield_pct") is not None
    ]
    oil_yield_multiplier = max(cluster_avgs) if cluster_avgs else 1.0
    return MapData(
        tile_pool=pool,
        map_area=map_area,
        oil_spot_count=len(oil_spots),
        water_pump_cap=water_pump_cap,
        wood_budget=wood_budget,
        oil_yield_multiplier=oil_yield_multiplier,
    )


def load_tile_pool(path: Path | str | None) -> dict[str, float]:
    """Convenience wrapper kept for callers that only need the tile pool
    (wood-per-tree is irrelevant to the tile pool, so any value works)."""
    return load_map_data(path, 0.0).tile_pool


def _load_l1_output(path: Path) -> dict:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict) or "layers" not in data:
        raise ValueError(f"{path}: not a valid L1 output (missing 'layers')")
    return data


def build_instance(
    scenario_obj: scenario_mod.Scenario,
    l1_output_path: str | Path,
    model: GameModel | None = None,
    mode: str = MODE_EXPERIMENTAL,
    map_probe_path: str | Path | None = None,
    deployment_enabled: bool = True,
    player_time_enabled: bool = True,
    max_area_fraction: float | None = None,
    l2_config: L2Config | None = None,
) -> L2Instance:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    cfg = l2_config if l2_config is not None else load_config()
    # CLI --max-area-fraction overrides the config default when given.
    if max_area_fraction is None:
        max_area_fraction = cfg.max_area_fraction
    model = model if model is not None else load_model()
    l1 = _load_l1_output(Path(l1_output_path))
    layers: list[list[str]] = [list(L) for L in l1["layers"]]
    linear = linearize_layers(layers)
    warnings: list[str] = []

    initial_techs = frozenset(scenario_obj.initial.techs_researched)

    # Fold the hand-crafting character into the effective initial
    # items. Adds to any user-supplied count (the user might also list
    # AM1s for some other reason; we sum rather than overwrite).
    effective_initial_items: dict[str, float] = {
        n: float(c) for n, c in scenario_obj.initial.items
    }
    effective_initial_items[cfg.character_building] = (
        effective_initial_items.get(cfg.character_building, 0.0) + cfg.character_count
    )
    # Mode-specific seeding. Lower-bound seeds "always needed"
    # infrastructure so strict timing has something to use in step 0.
    # Experimental + trapezoidal seed just the raw-extraction loophole
    # (pump) so step 0 has water. (Trapezoidal's 0.5 weight could partly
    # self-bootstrap the pump, but seeding it matches experimental and
    # avoids any step-0 corner case; the lone extra pump is negligible.)
    seed = cfg.lower_bound_seed if mode == MODE_LOWER_BOUND else cfg.experimental_seed
    for name, count in seed.items():
        effective_initial_items[name] = effective_initial_items.get(name, 0.0) + count

    initial_building_names = frozenset(
        n for n in effective_initial_items if n in model.buildings
    )
    cumulative: set[str] = set(initial_techs)

    steps: list[L2Step] = []
    for tech in linear:
        idx = len(steps)
        start = frozenset(cumulative)
        cumulative.add(tech)
        end = frozenset(cumulative)
        research = _research_pseudo_recipe(idx, tech, model)
        if research is None and tech in model.technologies:
            t = model.technologies[tech]
            if t.research_trigger:
                warnings.append(
                    f"step {idx} ({tech!r}): trigger-based research not yet modeled"
                )
            elif not t.ingredients:
                warnings.append(
                    f"step {idx} ({tech!r}): no science-pack cost in data — skipped"
                )
        elif research is None:
            warnings.append(f"step {idx} ({tech!r}): unknown technology")
        avail = _buildings_available_at(start, initial_building_names, model)
        if tech in cfg.split_research_techs and research is not None:
            # First half: an intermediate `<tech>-middle` step doing half
            # the resource cost. The tech is NOT yet researched at its end,
            # so recipe enablement is untouched — it still flips on at the
            # END of the real step below. Distinct pseudo-recipe name keeps
            # the research-equality constraints from colliding.
            half = (research.cycles_required or 0.0) / 2.0
            mid_research = replace(
                research,
                name=f"research/{tech}-middle",
                bound_step=idx,
                cycles_required=half,
            )
            steps.append(
                L2Step(
                    index=idx,
                    research_tech=f"{tech}-middle",
                    techs_researched_at_start=start,
                    techs_researched_at_end=start,
                    research=mid_research,
                    available_buildings_at_start=avail,
                    label=f"{tech}-middle",
                )
            )
            # Second half: the real step, completing the research; the tech
            # becomes available at its end.
            real_idx = len(steps)
            research = replace(research, bound_step=real_idx, cycles_required=half)
            steps.append(
                L2Step(
                    index=real_idx,
                    research_tech=tech,
                    techs_researched_at_start=start,
                    techs_researched_at_end=end,
                    research=research,
                    available_buildings_at_start=avail,
                )
            )
            continue
        steps.append(
            L2Step(
                index=idx,
                research_tech=tech,
                techs_researched_at_start=start,
                techs_researched_at_end=end,
                research=research,
                available_buildings_at_start=avail,
            )
        )

    # Trailing FINAL step: full tech availability, no research; where
    # goal-state production and launches resolve.
    final_start = frozenset(cumulative)
    steps.append(
        L2Step(
            index=len(steps),
            research_tech=None,
            techs_researched_at_start=final_start,
            techs_researched_at_end=final_start,
            research=None,
            available_buildings_at_start=_buildings_available_at(
                final_start, initial_building_names, model
            ),
        )
    )

    launches = tuple(_launch_pseudo_recipes(scenario_obj.goal.rocket_launches))
    producible = _producible_items(steps, effective_initial_items, model)
    burns = tuple(_burn_pseudo_recipes(model, producible, cfg.fuel_excluded))

    final_floors: dict[str, float] = {}
    for item, count in scenario_obj.goal.items_produced:
        final_floors[item] = max(final_floors.get(item, 0.0), float(count))
    total_launches = sum(c for _, c in scenario_obj.goal.rocket_launches)
    if total_launches > 0:
        final_floors[LAUNCH_EVENT_ITEM] = float(total_launches)

    # Structural validation: goal items neither produced by any recipe
    # nor present in the initial state are unreachable.
    for item in final_floors:
        if item == LAUNCH_EVENT_ITEM or item in effective_initial_items:
            continue
        if not model.recipes_producing(item):
            warnings.append(
                f"goal item {item!r}: no recipe produces it and not in initial_state"
            )

    reachable_buildings = _compute_reachable_buildings(
        effective_initial_items, layers, initial_techs, model
    )

    # Resolve checkpoints (may append new steps to `steps`). Needs
    # `reachable_buildings` for the recipe_building_pairs check.
    resolved_checkpoints = _resolve_checkpoints(
        scenario_obj.checkpoints, steps, model, reachable_buildings, warnings
    )

    # Resolve the map probe. The map is supplied explicitly (from the run
    # manifest); absence is fine — spatial caps just don't fire. Even with
    # deployment_enabled=False we still load it; the LP uses tile_pool /
    # map_area / oil_spot_count for unconditional spatial caps. Only the
    # infra-item reservation respects deployment_enabled.
    md = load_map_data(map_probe_path, cfg.wood_per_tree)
    if map_probe_path is not None and not md.tile_pool:
        warnings.append(
            f"map probe at {map_probe_path} had no patches; per-resource caps disabled"
        )
    if (
        md.map_area == 0.0
        and map_probe_path is not None
        and Path(map_probe_path).exists()
    ):
        warnings.append(
            f"map probe at {map_probe_path} lacks map_gen_settings; "
            "total-area cap disabled"
        )

    return L2Instance(
        scenario=scenario_obj,
        l1_method=str(l1.get("method", "?")),
        mode=mode,
        steps=tuple(steps),
        launches=launches,
        burns=burns,
        final_floors=final_floors,
        checkpoints=resolved_checkpoints,
        effective_initial_items=effective_initial_items,
        reachable_buildings=reachable_buildings,
        producible_items=producible,
        excluded_items=frozenset({"wood"}),
        pruned_items=cfg.pruned_items,
        tile_pool=md.tile_pool,
        map_area=md.map_area,
        oil_spot_count=md.oil_spot_count,
        water_pump_cap=md.water_pump_cap,
        wood_budget=md.wood_budget,
        oil_yield_multiplier=md.oil_yield_multiplier,
        max_area_fraction=max_area_fraction,
        deployment_enabled=deployment_enabled,
        player_time_enabled=player_time_enabled,
        warnings=tuple(warnings),
        cfg=cfg,
    )


def _print_summary(inst: L2Instance, model: GameModel) -> None:
    s = inst.scenario
    print(f"Scenario:      {s.name or '(unnamed)'}  ({s.source or 'n/a'})")
    print(f"L1 method:     {inst.l1_method}")
    print(
        f"Steps:         {len(inst.steps)} "
        f"({len(inst.steps) - 1} tech-research + 1 FINAL)"
    )
    print(f"Launches:      {len(inst.launches)} pseudo-recipe(s)")
    print(
        f"Burns:         {len(inst.burns)} pseudo-recipe(s) "
        f"({', '.join(B.ingredients[0][0] for B in inst.burns) or 'none'})"
    )
    print(f"Mode:          {inst.mode}")
    print("Initial items (effective; includes synthetic adjustments):")
    user_items = dict(s.initial.items)
    char_b = inst.cfg.character_building
    for name, val in sorted(inst.effective_initial_items.items()):
        markers = []
        if name == char_b and val > user_items.get(name, 0.0):
            markers.append("*character")
        if (
            inst.mode == MODE_LOWER_BOUND
            and name in inst.cfg.lower_bound_seed
            and val
            > user_items.get(name, 0.0)
            + (inst.cfg.character_count if name == char_b else 0.0)
        ):
            markers.append("*lower-bound seed")
        marker = ("  " + " ".join(markers)) if markers else ""
        print(f"   {name:30s} {val:g}{marker}")
    print(f"Initial techs: {sorted(s.initial.techs_researched) or '(none)'}")
    print("Final floors:")
    for name, val in sorted(inst.final_floors.items()):
        print(f"   {name:30s} ≥ {val:g}")
    items = inst.all_items(model)
    print(f"Distinct items touched: {len(items)}")
    print(
        f"Reachable buildings:    {len(inst.reachable_buildings)} / "
        f"{len(model.buildings)}"
    )
    if inst.warnings:
        print(f"\nWarnings ({len(inst.warnings)}):")
        for w in inst.warnings:
            print(f"  - {w}")
    print("\nSteps:")
    for step in inst.steps:
        tag = step.research_tech or "(FINAL)"
        recipe_count = len(step.available_recipes(model))
        pair_count = len(step.recipe_building_pairs(model, inst.reachable_buildings))
        cyc = ""
        if step.research:
            cyc = (
                f"   research={step.research.cycles_required:g}"
                f" × {step.research.time_seconds:g}s"
                f" of {len(step.research.ingredients)} pack(s)"
            )
        print(
            f"   step {step.index:2d}  {tag:30s}"
            f"  recipes={recipe_count:3d}  pairs={pair_count:4d}{cyc}"
        )
