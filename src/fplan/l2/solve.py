"""L2 LP via SCIP (pyscipopt).

V2 (adds energy round): production/consumption balance, per-step
duration variables, per-building bilinear capacity, hard-bound
research, launch equality, goal-floor inequalities, per-step electric
energy balance, per-burner-building fuel allocation. Objective:
`min t_FINAL = Σ duration[i]`. Per (building, step):

    Σ_{r,p runnable on b} (x[r,b,i] · recipe_time_r · weight_b)
      ≤  item[b, tier] · speed_b · duration[i]

Pseudo-recipes with multi-building capacity (the boiler-engine burn
pair) contribute to multiple (b, i) sums via their
`capacity_per_building` weights. The `item[b, tier] · duration[i]`
product is the nonconvex bilinear term; SCIP handles the nonconvex NLP
natively via spatial branch-and-bound (all variables are continuous — no
integer variables, so it is an NLP, not a MINLP).

Energy: a per-step side constraint
    Σ_electric_consumers (x · recipe_time · power_mw / speed)
      ≤  Σ_burns (x · electric_output_mj_per_cycle)
gates electric production. The LP works in MJ and MW internally
(coefficients stay near unity, avoiding SCIP numerical troubles); the
data model still exposes raw J/W and conversion happens at LP-
construction points. The player hand-craft facility draws no grid power,
so it never appears in the demand sum (see PLAYER_CRAFT_SPEED). Burner
buildings (stone-furnace, burner-mining-drill, steel-furnace) have
per-(building, step) fuel-burn variables that deduct from the chosen fuel
item and supply that building's energy demand. Wood is excluded as fuel
everywhere.

Solver isolation: this is the only module that imports pyscipopt.
`L2Instance` and :mod:`fplan.l2.instance` stay solver-neutral.

Still NOT modeled (subsequent rounds): steam as an intermediate item,
modules and beacons, nuclear (reactor + turbine + fuel cells).

Driven by the CLI: `fplan rates solve <run>` (see :mod:`fplan.cli.rates`).
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from pyscipopt import Model, quicksum

from fplan.l2 import backend as l2_backend
from fplan.l2 import instance as l2_phases
from fplan.l2.instance import L2Instance, PseudoRecipe
from fplan.model import GameModel, Recipe


@dataclass
class Solution:
    status: str
    objective: float | None
    # Real-recipe activity: (recipe, building, step) -> cycles
    x_real: dict[tuple[str, str, int], float]
    # Pseudo-recipe activity: (pseudo-recipe-name, step) -> cycles.
    # Pseudo-recipes can list multiple buildings in capacity_per_building
    # (e.g., burn = boiler+steam-engine), so the building dimension is
    # dropped here — the LP carries one cycle variable per (pseudo, step).
    x_pseudo: dict[tuple[str, int], float]
    # Player hand-crafting activity: (recipe, step) -> cycles. The character
    # is the implicit fixed-count facility, so there is no building dimension.
    x_hand: dict[tuple[str, int], float]
    item: dict[tuple[str, int], float]  # (item, tier) -> count
    duration: dict[int, float]  # step -> seconds
    # Per-ore electric-drill assignment: (ore, tier) -> drill count on that ore.
    # Surfaces which drills sit on which patch for L3 placement.
    drill_assign: dict[tuple[str, int], float]
    # Per-output steel-furnace assignment: (output, tier) -> furnace count
    # committed to smelting that product. Surfaces which furnaces smelt what
    # for L3 placement (a furnace can't switch product mid-run).
    furnace_assign: dict[tuple[str, int], float]
    excluded_consumed: dict[str, float]  # tracked-but-not-constrained items
    # Fuel chosen per (burner-building, step):
    # (fuel, building, step) -> units burned.
    # Boiler is NOT here — its fuel is encoded directly in the burn
    # pseudo-recipe ingredients.
    fuel_burn: dict[tuple[str, str, int], float]
    # Per-step electric energy bookkeeping (joules).
    # Per-step electric energy bookkeeping in MJ (matches the LP's
    # internal rescaling; multiply by 1e6 if you need joules).
    electric_demand: dict[int, float]  # MJ — grid demand
    electric_supply: dict[int, float]  # MJ — from burn pseudo-recipes
    n_vars: int
    n_constrs: int
    # Solver diagnostics — useful when status != "optimal" or for
    # spotting nonconvex spatial-B&B runs that stopped at a gap.
    n_nodes: int = 0
    gap: float = 0.0
    dual_bound: float | None = None
    solve_time_s: float = 0.0
    # B&B tree shape (spatial branch-and-bound over the bilinear terms).
    # total_nodes counts nodes across ALL runs (≥ n_nodes, which is the
    # current run only); max_depth is the deepest node reached. The
    # effective branching factor is derived from them — see
    # _effective_branching_factor.
    n_total_nodes: int = 0
    max_depth: int = 0
    branching_factor: float | None = None
    # SCIP randomization seed used for this run; emitted in the YAML
    # so a "lucky" run can be re-played exactly by passing --seed N.
    seed: int | None = None


# Internal energy scaling: the LP works in MJ and MW so coefficients
# stay in a sane range (0.1 – 1e3) instead of spanning J/W (1e1 – 1e7)
# alongside seconds (~10²) and rates (~1). SCIP's spatial branch-and-bound
# hits "unresolved numerical troubles" on the wider span; rescaling
# preserves the model exactly while putting every term within ~6 orders
# of magnitude of each other. Conversion happens at LP-construction and
# post-solve extraction points; the model layer (fplan.model) still
# exposes raw J/W and is not changed.
_J_PER_MJ = 1.0e6

# Recipes whose whole-plan demand is at most one unit, so the craft is a
# single indivisible job that can't be parallelized across machines. The
# pooled capacity constraint (Σ t·x ≤ count·speed·duration) alone lets the
# LP "build" such an item in a step far shorter than its craft time by
# stacking machines at a fraction-of-a-craft each (measured: ~20 assemblers
# building 1 rocket-silo in a 2 s step). For these we add a single-machine
# wall-clock constraint forcing the step to last at least one full craft
# (see the constraint block in build_lp). It is valid ONLY because demand is
# ≤ 1 — it serializes the recipe, so applying it to a multi-unit recipe would
# wrongly forbid legitimate parallel construction.

# --- Player hand-crafting facility -----------------------------------------
# The character is one fixed-count serial crafter, not a built machine. Its
# capacity term is `PLAYER_CRAFT_SPEED · duration[i]` — a CONSTANT count times
# the duration variable, so it is LINEAR (no count×duration bilinear term, the
# thing that made a "player-as-facility" expensive in the old analysis). It
# draws no electricity, and hand-crafts proceed in the BACKGROUND while the
# character walks / places / fells, so hand-craft time is NOT part of the
# serial `player_time` budget — it's a separate per-step bound.
#
# This replaces the old 2×AM1 stand-in (folded into effective_initial_items),
# which (a) rode inside item[assembling-machine-1]'s bilinear capacity term
# and (b) needed the char_credit electric carve-out. Both are now gone.
PLAYER_CRAFT_SPEED = 1.0  # character crafting_speed: 1.0 recipe-sec/sec
# Recipe categories the character can hand-craft. From the `character`
# prototype's crafting_categories: exactly {"crafting"} — narrower than an AM1,
# which also carries basic-crafting / advanced-crafting. Recipes outside this
# set need a built machine, exactly as in game.
HAND_CRAFT_CATEGORIES = frozenset({"crafting"})

# Realism regularizer (NOT a hard physical law). Factorio research is
# *continuous* — many labs sum fractional progress — so unlike the indivisible
# rocket-silo craft, a research step has no true one-cycle floor: the pooled
# lab capacity lets it shrink below a single cycle by stacking labs (measured:
# ~110 labs finishing a 75-unit research in 6.8 s). That is unrealistic for a
# WR TAS and produces spiky sub-cycle steps that fight downstream rate-
# flattening. When enabled, force every research step to last at least one
# cycle of its science (duration[i] ≥ tech.time_seconds). Caveat: the model
# ignores research-speed bonuses (lab base_speed is constant 1.0), so this
# uses *base* cycle times and slightly over-constrains late-game steps where
# research-speed would otherwise shorten the real cycle.
ENFORCE_RESEARCH_CYCLE_FLOOR = True


def _lab_speed_mult(inst, model) -> dict[int, float]:
    """Per-step lab speed multiplier from completed research-speed techs.
    A tech's `laboratory-speed` bonus (research-speed-N: +0.2, +0.3, …)
    applies to FUTURE steps, after it completes, so step i's labs run at
    base × (1 + Σ bonuses of research-speed techs researched in steps < i).
    Used by the lab capacity constraint, the research cycle-time floor, and
    the post-solve lab-utilization report so all three stay consistent."""
    mult: dict[int, float] = {}
    bonus = 0.0
    for i, step in enumerate(inst.steps):
        mult[i] = 1.0 + bonus
        r = step.research
        if r is not None and r.name.startswith("research/"):
            tech = model.technologies.get(r.name.split("/", 1)[1])
            if tech is not None:
                bonus += tech.lab_speed_bonus
    return mult


def _net_coefs(
    ingredients: list[tuple[str, float]],
    outputs: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Net per-cycle coefficient per item. Net = Σ output − Σ input;
    items with zero net are dropped. Items appearing only on one side
    yield a one-sided coefficient (the common case); both-sided
    appearances (Kovarex-style) collapse to a single net term — not
    used by the current TAS scenarios but supported.
    """
    coefs: dict[str, float] = {}
    for n, amt in ingredients:
        coefs[n] = coefs.get(n, 0.0) - amt
    for n, amt in outputs:
        coefs[n] = coefs.get(n, 0.0) + amt
    return [(n, c) for n, c in coefs.items() if c != 0.0]


def _recipe_net_coefs(r: Recipe) -> list[tuple[str, float]]:
    return _net_coefs(
        [(s.name, s.amount) for s in r.ingredients],
        [(s.name, s.amount) for s in r.outputs],
    )


def _pseudo_net_coefs(p: PseudoRecipe) -> list[tuple[str, float]]:
    return _net_coefs(list(p.ingredients), list(p.outputs))


def _scale_item_units(
    net_coefs: dict[str, list[tuple[str, float]]],
    item_name: str,
    factor: float,
) -> None:
    """Rescale an item's modeling unit by `factor`: multiply its coefficient
    in every recipe (output *and* ingredient) so the LP variable counts
    `item / factor`-sized sub-units. Used to shrink an oversized storage
    coefficient — storage-tank holds 25000 fluid, so with factor=100 the
    variable counts 0.01-tank units (250 fluid each), turning the 25000
    coefficient in the fluid buffer into 250 and tightening conditioning.
    The recipe then yields 100 sub-units per craft (physics conserved:
    100 × 250 = 25000). Mutates net_coefs."""
    if factor == 1.0:
        return
    for name, coefs in net_coefs.items():
        if any(it == item_name for it, _ in coefs):
            net_coefs[name] = [
                (it, c * factor if it == item_name else c) for it, c in coefs
            ]


def _scale_oil_yield(
    net_coefs: dict[str, list[tuple[str, float]]],
    model: GameModel,
    multiplier: float,
) -> None:
    """Scale crude-oil EXTRACTION by the map's richest-cluster yield (a
    pumpjack on a high-yield spot produces multiplier× the 100%-yield base).
    Only mining recipes' positive crude-oil output is scaled — never the
    oil-processing recipes that consume crude-oil. Mutates net_coefs."""
    if multiplier == 1.0:
        return
    for r in model.recipes.values():
        if r.kind != "mining":
            continue
        net_coefs[r.name] = [
            (item, c * multiplier if (item == "crude-oil" and c > 0) else c)
            for item, c in net_coefs[r.name]
        ]


def _safe(name: str) -> str:
    """SCIP var/constraint name sanitizer."""
    return name.replace("/", "_").replace("|", "_")


def _end_state_count(inst: L2Instance, item: str) -> float:
    """How many of `item` the scenario's end state requires.

    The max over the goal's `items_produced` and every checkpoint's
    `items_floor` (a checkpoint floor is a hard "≥ this many present"
    at its boundary). A rocket launch additionally needs the silo
    *present* — but one silo serves any number of launches, so its
    contribution is a flat 1, never multiplied by the launch count.
    Used to gate the single-machine constraint, which is only valid at
    exactly one unit.
    """
    goal = inst.scenario.goal
    counts = [c for n, c in goal.items_produced if n == item]
    counts += [
        cp.items_floor[item] for cp in inst.checkpoints if item in cp.items_floor
    ]
    if item == "rocket-silo" and any(c for _, c in goal.rocket_launches):
        counts.append(1.0)
    return max(counts) if counts else 0.0


def build_lp(
    inst: L2Instance,
    model: GameModel,
    verbose: bool = False,
    time_limit_s: float | None = None,
    gap_limit: float | None = None,
    stall_nodes: int | None = None,
    node_limit: int | None = None,
    seed: int | None = None,
    lp_algorithm: str | None = None,
) -> tuple[Model, dict]:
    """Construct the v2 SCIP model for `inst` (energy-aware).

    Returns (scip_model, handles). `handles` exposes variable refs
    for post-solve extraction:
        handles["x_real"]:     {(recipe, building, step) -> Var}
        handles["x_pseudo"]:   {(pseudo-recipe-name, step) -> Var}
        handles["item"]:       {(item, tier) -> Var}
        handles["duration"]:   {step -> Var}
        handles["fuel_burn"]:  {(fuel, burner-building, step) -> Var}
        handles["x_hand"]:     {(recipe, step) -> Var}  (player hand-craft)
        handles["elec_demand_lin"]: {step -> linear expr (joules consumed)}
        handles["elec_supply_lin"]: {step -> linear expr (joules supplied by burns)}
        handles["tracked_items"]: frozenset[str]
        handles["n_tiers"]: int
    """
    m = Model("l2_v2")
    if not verbose:
        m.hideOutput()
    # Termination conditions: any unset value leaves SCIP at its
    # default (effectively unbounded for time/nodes, 0.0 for gap).
    if time_limit_s is not None:
        m.setParam("limits/time", float(time_limit_s))
    if gap_limit is not None:
        m.setParam("limits/gap", float(gap_limit))
    if stall_nodes is not None:
        m.setParam("limits/stallnodes", int(stall_nodes))
    if node_limit is not None:
        m.setParam("limits/nodes", int(node_limit))
    # Track LP basis condition number; emit a warning when it exceeds
    # this threshold. 1e8 is a common "starting to be worrying" mark;
    # 1e12 is "the LP is unsolvable to working precision." SCIP also
    # surfaces the estimate in its post-solve statistics.
    m.setParam("lp/conditionlimit", 1e8)
    # Randomization seed: SCIP's randomseedshift is added to ALL its
    # internal random seeds, so this single knob is sufficient to vary
    # heuristic schedules, branching tie-breakers, etc., between runs.
    # Caller picks the seed (the CLI in main() randomizes by default
    # and prints it so successful seeds can be re-used for repro).
    if seed is not None:
        m.setParam("randomization/randomseedshift", int(seed))
    # LP algorithm for the root + node LPs. Given as a method label
    # ("simplex"/"barrier") and mapped to SCIP's code via fplan.l2.backend.
    # Barrier (HiGHS only) is an interior-point method that sidesteps the simplex
    # degeneracy that stalls the nonconvex root LP on larger models; a
    # SoPlex-linked SCIP silently falls back to simplex. Unset leaves SCIP's
    # default. Sets both the initial and resolve LP algorithms.
    if lp_algorithm is not None:
        code = l2_backend.lp_algorithm_code(lp_algorithm)
        m.setParam("lp/initalgorithm", code)
        m.setParam("lp/resolvealgorithm", code)

    initial_items = inst.effective_initial_items
    excluded = inst.excluded_items
    pruned = inst.pruned_items
    all_items = inst.all_items(model)
    # `pruned` items get no var / balance at all (vs `excluded`, which keep
    # their ingredient role and are reported post-solve). Recipes producing
    # only pruned items get no var either (the x_real loop below).
    tracked = frozenset(all_items - excluded - pruned)

    n_steps = len(inst.steps)
    n_tiers = n_steps + 1

    # Catalogue pseudo-recipes (research, launches, burns) so the rest
    # of the build can look them up by name without re-scanning.
    pseudo_by_name: dict[str, PseudoRecipe] = {}
    for step in inst.steps:
        if step.research:
            pseudo_by_name[step.research.name] = step.research
    for L in inst.launches:
        pseudo_by_name[L.name] = L
    for B in inst.burns:
        pseudo_by_name[B.name] = B

    # Pre-compute net coefficients for every recipe (real + pseudo).
    net_coefs: dict[str, list[tuple[str, float]]] = {}
    for r in model.recipes.values():
        net_coefs[r.name] = _recipe_net_coefs(r)
    for p in pseudo_by_name.values():
        net_coefs[p.name] = _pseudo_net_coefs(p)
    _scale_oil_yield(net_coefs, model, inst.oil_yield_multiplier)
    _scale_item_units(net_coefs, "storage-tank", STORAGE_TANK_SCALE)
    for _chest in STORAGE_CHESTS:
        _scale_item_units(net_coefs, _chest, CHEST_SCALE)

    # Recipe times: real recipes from the model, pseudo-recipes from
    # the instance. Pre-fetched for the capacity / energy loops.
    recipe_time: dict[str, float] = {
        r.name: r.time_seconds for r in model.recipes.values()
    }
    for p in pseudo_by_name.values():
        recipe_time[p.name] = p.time_seconds

    # --- decision variables ---

    # Real recipes: one var per (recipe, building, step), pruned by
    # strict tech-availability + reachable-buildings + per-step
    # building availability. `before_recipe` checkpoints add the named
    # recipe to `forbidden_real_recipes` on every step that the
    # checkpoint excludes from running it — those (recipe, b, step)
    # entries get no variable created here.
    x_real: dict[tuple[str, str, int], object] = {}
    for i, step in enumerate(inst.steps):
        for r_name, b_name in step.recipe_building_pairs(
            model, inst.reachable_buildings
        ):
            if r_name in step.forbidden_real_recipes:
                continue
            # Disabled smelting buildings (electric-furnace) get no activity
            # var at all — smelting is served by stone + the per-output-split
            # steel furnace, keeping the bilinear capacity-term count down.
            if b_name in inst.cfg.smelting_disabled_buildings:
                continue
            # Drop the recipe entirely when every output is pruned — there's
            # no balance to feed, so it could only dump into the void. The
            # `all(...)` guard keeps multi-output recipes that co-produce a
            # tracked item (none today, but oil-style recipes would qualify).
            r_obj = model.recipes.get(r_name)
            if (
                r_obj is not None
                and r_obj.outputs
                and all(s.name in pruned for s in r_obj.outputs)
            ):
                continue
            x_real[(r_name, b_name, i)] = m.addVar(
                name=_safe(f"x_{r_name}_{b_name}_{i}"),
                lb=0.0,
                vtype="C",
            )

    # Player hand-crafting: one var per (recipe, step) for recipes the
    # character can hand-craft. No building dimension — the single character
    # is the implicit, fixed-count facility, available from t₀ (recipes are
    # still strict tech-gated via available_recipes). Pruned like x_real:
    # forbidden recipes and all-pruned-output recipes get no var.
    x_hand: dict[tuple[str, int], object] = {}
    for i, step in enumerate(inst.steps):
        for r in step.available_recipes(model):
            if r.category not in HAND_CRAFT_CATEGORIES:
                continue
            if r.name in step.forbidden_real_recipes:
                continue
            if r.outputs and all(s.name in pruned for s in r.outputs):
                continue
            x_hand[(r.name, i)] = m.addVar(
                name=_safe(f"hand_{r.name}_{i}"),
                lb=0.0,
                vtype="C",
            )

    # Pseudo-recipes: one var per (pseudo, step). Multi-building
    # capacity (boiler+steam-engine) is encoded via capacity_per_building
    # entries, not the variable's keying.
    x_pseudo: dict[tuple[str, int], object] = {}

    def _all_caps_available(p: PseudoRecipe, step: l2_phases.L2Step) -> bool:
        return all(
            b_name in step.available_buildings_at_start
            for b_name, _ in p.capacity_per_building
        )

    # Research: only in its bound step.
    for i, step in enumerate(inst.steps):
        if step.research is not None and _all_caps_available(step.research, step):
            x_pseudo[(step.research.name, i)] = m.addVar(
                name=_safe(f"x_{step.research.name}_{i}"),
                lb=0.0,
                vtype="C",
            )
    # Launches: any step where the silo is available. With
    # `before_recipe: rocket-part` checkpoints, launches naturally
    # settle into the step where rocket-parts can be produced,
    # because item-flow forbids launches in steps with zero parts.
    for L in inst.launches:
        for i, step in enumerate(inst.steps):
            if _all_caps_available(L, step):
                x_pseudo[(L.name, i)] = m.addVar(
                    name=_safe(f"x_{L.name}_{i}"),
                    lb=0.0,
                    vtype="C",
                )
    # Burns: any step where boiler AND steam-engine are both available.
    for B in inst.burns:
        for i, step in enumerate(inst.steps):
            if _all_caps_available(B, step):
                x_pseudo[(B.name, i)] = m.addVar(
                    name=_safe(f"x_{B.name}_{i}"),
                    lb=0.0,
                    vtype="C",
                )

    # item[n, tier] — state variable per tracked item per tier.
    #
    # Building-count vars get an explicit upper bound. Every bilinear
    # capacity term is item[building, ·] × duration[i]: duration is
    # capped at MAX_STEP_DURATION precisely so the McCormick envelope is
    # finite, but without a matching cap on the OTHER factor (the count)
    # half of each envelope stays degenerate and the NLP subsolver
    # (IPOPT, via SCIP's subnlp heuristic — empirically the only source
    # of good primals on default-victory) searches an unbounded box,
    # hits its own time limit, and fails to return the incumbent. The
    # total-area constraint already *implies* count ≤ area_budget /
    # footprint; stating it per-variable hands presolve and the NLP that
    # bound from the start. Tightened further by the per-resource tile
    # pool (drills) and per-oil-spot (pumpjacks) where they apply. With
    # no map data (area_budget == 0) we leave counts unbounded — those
    # scenarios are small and solve regardless.
    area_budget = inst.max_area_fraction * inst.map_area
    total_tile_pool = sum(inst.tile_pool.values())

    def _building_count_ub(b_name: str) -> float | None:
        b = model.buildings.get(b_name)
        if b is None:
            return None
        # Pumpjacks are per-spot, not per-tile — independent of map area.
        # Offshore-pumps are per-water-perimeter, likewise area-independent.
        hard_cap: float | None = None
        if b_name == "pumpjack" and inst.oil_spot_count > 0:
            hard_cap = float(inst.oil_spot_count)
        elif b_name == "offshore-pump" and inst.water_pump_cap > 0:
            hard_cap = float(inst.water_pump_cap)
        # Area-derived ceiling — only when map data is present.
        #
        # NB: this stays *loose* on purpose. The bound's job is to give
        # the NLP subsolver a finite box, not to encode policy. A tight
        # cap on a building the min-time relaxation wants to over-build
        # (e.g. forcing rocket-silo ≤ 2, which is physically true) pushes
        # the feasible region to a spot subnlp's IPOPT solve can't reach
        # an interior point of, and SCIP then returns NO incumbent at all
        # within the time limit. Measured: silo ub ∈ {2, 20} → 0 primals;
        # area-derived (~2558) → the ~2818s plan, reliably. So we cap only
        # by what physically fits, and let the objective pick the count.
        area_ub: float | None = None
        if area_budget > 0:
            fp = inst.deployed_facility(model, b).tile_footprint
            if fp > 0:
                area_ub = area_budget / fp
                if (
                    b.kind == "mining-drill"
                    and b_name != "burner-mining-drill"
                    and total_tile_pool > 0
                ):
                    # Can't pack more drills than the combined patches hold.
                    area_ub = min(area_ub, total_tile_pool / fp)
        cands = [c for c in (hard_cap, area_ub) if c is not None]
        return min(cands) if cands else None

    item_vars: dict[tuple[str, int], object] = {}
    for n in tracked:
        ub = _building_count_ub(n) if n in model.buildings else None
        for tier in range(n_tiers):
            item_vars[(n, tier)] = m.addVar(
                name=_safe(f"item_{n}_{tier}"),
                lb=0.0,
                ub=ub,
                vtype="C",
            )

    # Per-step duration variables (seconds). Capped at MAX_STEP_DURATION
    # so the spatial B&B over bilinear capacity constraints has finite
    # variable bounds — otherwise the McCormick envelope is infinitely
    # loose and SCIP warns "cannot guarantee finite termination."
    # This also sets the McCormick envelope width for every bilinear
    # `count × duration` term, so it can't go arbitrarily high — 3600s and
    # 900s were both measured to loosen the relaxation enough that SCIP found
    # zero primals on default-victory once the ore-specific drill split (more
    # bilinear terms) landed. 600s is the proven-tractable value.
    MAX_STEP_DURATION = 600.0
    duration_vars: dict[int, object] = {
        i: m.addVar(name=f"duration_{i}", lb=0.0, ub=MAX_STEP_DURATION, vtype="C")
        for i in range(n_steps)
    }

    # --- fuel allocation for non-boiler burner buildings ---
    # Stone-furnace, steel-furnace, burner-mining-drill all consume
    # chemical fuel during recipe execution. Their recipes don't list
    # fuel as an ingredient, so we add per-(building, step) fuel-burn
    # variables that:
    #   (a) deduct from the chosen fuel item's flow, and
    #   (b) supply the building's electric/heat demand (constraint below).
    # Wood is excluded from the allowed fuel set per the config's fuel_excluded.
    # Fuels not producible in this scenario (e.g. nuclear-fuel without nuclear
    # research) are excluded too — otherwise they add dead fuel_burn variables
    # and drag their fuel-value coefficient (nuclear-fuel = 1210 MJ) into the
    # energy balance, widening the coefficient range for no benefit.
    allowed_fuels: list[str] = sorted(
        n
        for n, it in model.items.items()
        if it.fuel_category == "chemical"
        and it.fuel_value_j
        and it.fuel_value_j > 0
        and n not in inst.cfg.fuel_excluded
        and n in inst.producible_items
    )
    burner_buildings: dict[str, object] = {}  # name -> Building
    for b_name, b in model.buildings.items():
        if b.energy_source_type != "burner":
            continue
        if b_name == "boiler":
            # Boiler fuel is encoded explicitly in burn pseudo-recipe ingredients.
            continue
        if "chemical" not in b.fuel_categories:
            continue  # nuclear-only burners (reactor) deferred
        if b_name not in inst.reachable_buildings:
            continue
        burner_buildings[b_name] = b

    fuel_burn: dict[tuple[str, str, int], object] = {}
    for b_name in burner_buildings:
        for i, step in enumerate(inst.steps):
            if b_name not in step.available_buildings_at_start:
                continue
            for fuel in allowed_fuels:
                fuel_burn[(fuel, b_name, i)] = m.addVar(
                    name=_safe(f"burn_{fuel}_{b_name}_{i}"),
                    lb=0.0,
                    vtype="C",
                )

    # --- flow constraints ---

    # Initial conditions: item[n, 0] == initial[n].
    for n in tracked:
        m.addCons(
            item_vars[(n, 0)] == initial_items.get(n, 0.0),
            name=_safe(f"init_{n}"),
        )

    # Accumulate per-(item, step) flow terms from x_real, x_pseudo,
    # and fuel_burn (the last only contributes negative consumption).
    flow_terms: dict[tuple[str, int], list] = {
        (n, i): [] for n in tracked for i in range(n_steps)
    }
    for (r_name, _b, i), var in x_real.items():
        for item_name, c in net_coefs.get(r_name, ()):
            if item_name in tracked:
                flow_terms[(item_name, i)].append(c * var)
    for (p_name, i), var in x_pseudo.items():
        for item_name, c in net_coefs.get(p_name, ()):
            if item_name in tracked:
                flow_terms[(item_name, i)].append(c * var)
    for (r_name, i), var in x_hand.items():
        for item_name, c in net_coefs.get(r_name, ()):
            if item_name in tracked:
                flow_terms[(item_name, i)].append(c * var)
    for (fuel, _b, i), var in fuel_burn.items():
        if fuel in tracked:
            flow_terms[(fuel, i)].append(-1.0 * var)

    for n in tracked:
        for i in range(n_steps):
            m.addCons(
                item_vars[(n, i + 1)]
                == item_vars[(n, i)] + quicksum(flow_terms[(n, i)]),
                name=_safe(f"flow_{n}_{i}"),
            )

    # Goal floors at the final tier.
    for n, floor in inst.final_floors.items():
        if n not in tracked:
            continue
        m.addCons(
            item_vars[(n, n_tiers - 1)] >= floor,
            name=_safe(f"floor_{n}"),
        )

    # Intermediate-state checkpoints (scenario.Checkpoint, resolved in
    # l2_phases). Same constraint shape as final_floors, but applied at
    # the checkpoint's resolved step boundary. Forces items to be
    # present at specific moments — closes LP-relaxation cheats like
    # "produce 0.374 rocket-silos amortized over the final step."
    for cp in inst.checkpoints:
        b = cp.boundary
        if b < 0 or b >= n_tiers:
            continue  # defensive: resolver should have caught
        for n, floor in cp.items_floor.items():
            if n not in tracked:
                continue
            m.addCons(
                item_vars[(n, b)] >= floor,
                name=_safe(f"ckpt_{cp.name}_{n}_b{b}"),
            )

    # Research equality: x_pseudo[(research, i)] == cycles_required.
    for i, step in enumerate(inst.steps):
        r = step.research
        if r is None:
            continue
        if (r.name, i) not in x_pseudo:
            continue
        m.addCons(
            x_pseudo[(r.name, i)] == (r.cycles_required or 0.0),
            name=_safe(f"research_{r.name}"),
        )

    # Per-step lab speed multiplier from completed research-speed techs; used
    # by BOTH the lab capacity constraint and the cycle-time floor below.
    lab_speed_mult = _lab_speed_mult(inst, model)

    # Research cycle-time floor (realism regularizer; see the comment on
    # ENFORCE_RESEARCH_CYCLE_FLOOR). Forces each research step to last at least
    # one cycle of its science, killing the sub-cycle steps the pooled lab
    # capacity would otherwise allow by stacking labs. Uses the EFFECTIVE cycle
    # time (base / lab speed), so research-speed bonuses correctly shorten the
    # floor late game.
    if ENFORCE_RESEARCH_CYCLE_FLOOR:
        for i, step in enumerate(inst.steps):
            r = step.research
            if r is None or (r.name, i) not in x_pseudo:
                continue
            cycle_s = float(r.time_seconds or 0.0) / lab_speed_mult[i]
            if cycle_s > 0:
                m.addCons(
                    duration_vars[i] >= cycle_s,
                    name=_safe(f"research_cycle_floor_{i}"),
                )

    # Launch equality: total cycles across all steps.
    for L in inst.launches:
        m.addCons(
            quicksum(
                x_pseudo[(L.name, i)] for i in range(n_steps) if (L.name, i) in x_pseudo
            )
            == (L.cycles_required or 0.0),
            name=_safe(f"launch_{L.name}"),
        )

    # --- capacity constraints (one per (building, step)) ---

    # Build (b, i) -> [(recipe_name, var, weight)] from BOTH real and
    # pseudo activity. Real recipes contribute weight=1.0 on their host
    # building. Pseudo-recipes contribute one entry per
    # capacity_per_building entry, with the entry's weight.
    cap_terms: dict[tuple[str, int], list[tuple[str, object, float]]] = {}
    for (r_name, b_name, i), var in x_real.items():
        cap_terms.setdefault((b_name, i), []).append((r_name, var, 1.0))
    for (p_name, i), var in x_pseudo.items():
        p = pseudo_by_name[p_name]
        for b_name, weight in p.capacity_per_building:
            cap_terms.setdefault((b_name, i), []).append((p_name, var, weight))

    # Capacity uses a per-building interpolation between start- and
    # end-of-step count (see l2_phases.capacity_end_weight):
    #   lower-bound:   weight=0 → effective = item[b, i]
    #   experimental:  weight=0 for raw extractors (drills, pumps),
    #                  weight=1 → item[b, i+1] for everything else
    for (b_name, i), entries in cap_terms.items():
        if not any(recipe_time.get(r, 0.0) > 0.0 for r, _, _ in entries):
            continue
        # Electric drills (per-ore) and steel furnaces (per-output) are split
        # below — a drill on iron can't switch to copper, a furnace smelting
        # iron can't switch to copper. Their pooled capacity is replaced by
        # per-assignment capacity, so skip them here.
        if b_name in (ELECTRIC_MINING_DRILL, STEEL_FURNACE):
            continue
        building = model.buildings.get(b_name)
        if building is None:
            continue
        end_w = inst.capacity_end_weight(b_name)
        start_w = 1.0 - end_w
        start_key = (b_name, i)
        end_key = (b_name, i + 1)
        if start_w > 0 and start_key not in item_vars:
            continue
        if end_w > 0 and end_key not in item_vars:
            continue
        effective_count_terms = []
        if start_w > 0:
            effective_count_terms.append(start_w * item_vars[start_key])
        if end_w > 0:
            effective_count_terms.append(end_w * item_vars[end_key])
        effective_count = quicksum(effective_count_terms)
        lhs = quicksum(
            recipe_time.get(r, 0.0) * weight * var for r, var, weight in entries
        )
        speed = building.base_speed
        if b_name == "lab":
            speed = speed * lab_speed_mult[i]  # research-speed bonus
        rhs = effective_count * speed * duration_vars[i]
        m.addCons(lhs <= rhs, name=_safe(f"cap_{b_name}_{i}"))

    # --- player hand-crafting capacity (one fixed-count serial actor) ---
    #
    # Σ_r recipe_time[r] · x_hand[r,i]  ≤  PLAYER_CRAFT_SPEED · duration[i]
    #
    # LINEAR: PLAYER_CRAFT_SPEED is a constant (count = 1 character), so the
    # RHS is constant×duration, not the count×duration product that makes the
    # built-machine capacity constraints bilinear. Hand-crafts run in the
    # background (parallel to the serial player_time budget), so the full
    # duration is available for crafting.
    for i in range(n_steps):
        hand_terms = [
            recipe_time.get(r_name, 0.0) * var
            for (r_name, ii), var in x_hand.items()
            if ii == i
        ]
        if hand_terms:
            m.addCons(
                quicksum(hand_terms) <= PLAYER_CRAFT_SPEED * duration_vars[i],
                name=_safe(f"hand_cap_{i}"),
            )

    # --- single-machine constraint for indivisible singleton crafts ---
    #
    # The pooled capacity constraint bounds output from above by capacity but
    # never says a single indivisible unit needs a whole craft-time on one
    # machine, so for a ≤1-unit recipe it lets `duration ≥ t/(count·speed)`
    # shrink toward zero as count grows. For r in inst.cfg.single_machine_recipes we
    # force the per-step single-machine wall-clock instead:
    #     Σ_b (recipe_time(r) / base_speed(b)) · x[r,b,i]  ≤  duration[i]
    # Purely linear in existing vars — no new variable, no bilinear term. With
    # x[silo,·,i] ≤ 1 (whole-plan demand 1) this is exact: building the silo
    # forces its step to last ≥ recipe_time/speed (40 s on an AM2, 24 s on an
    # AM3). It SERIALIZES the recipe across the step, so it is added ONLY when
    # the scenario's end state demands exactly one of the output item — gated
    # by _end_state_count below. Demanding ≥2 would wrongly forbid the
    # legitimate parallel build of the 2nd+ unit, so we skip (and log) those.
    for r_name in inst.cfg.single_machine_recipes:
        t = recipe_time.get(r_name, 0.0)
        if t <= 0:
            continue
        r = model.recipes.get(r_name)
        out_item = r.outputs[0].name if (r and r.outputs) else None
        demand = _end_state_count(inst, out_item) if out_item else 0.0
        if abs(demand - 1.0) > 1e-9:
            print(
                f"[single-machine] skipping {r_name}: end-state demand "
                f"for {out_item} is {demand:g}, not 1 (constraint serializes, "
                f"valid only at exactly 1)",
                file=sys.stderr,
            )
            continue
        for i in range(n_steps):
            terms = [
                (t / building.base_speed) * var
                for (rn, b_name, ii), var in x_real.items()
                if rn == r_name and ii == i
                for building in (model.buildings.get(b_name),)
                if building is not None and building.base_speed
            ]
            # The character is also a valid single machine for this craft when
            # it can hand-craft the recipe. At PLAYER_CRAFT_SPEED (1.0) it
            # builds the silo in t seconds — faster than an AM2 (0.75 → t/0.75)
            # but slower than an AM3 (1.25). With this term the LP picks the
            # fastest available builder; without it the player option was
            # invisible and the silo was pinned to whatever assembler existed.
            if (r_name, i) in x_hand:
                terms.append((t / PLAYER_CRAFT_SPEED) * x_hand[(r_name, i)])
            if terms:
                m.addCons(
                    quicksum(terms) <= duration_vars[i],
                    name=_safe(f"singlecraft_{r_name}_{i}"),
                )

    # --- electric-mining-drill: per-ore assignment (drills can't switch ore) ---
    #
    # Unlike assemblers, an electric drill sits on a patch and can't change
    # what it mines. So instead of one pooled capacity, each ore gets its own
    # drill-assignment count d[ore, i] that is non-decreasing (no repurposing)
    # and sums to the total drill count. These are first-class, persisted to
    # the output as `electric-mining-drill@<ore>` so L3 placement knows how
    # many drills sit on each patch. Bilinear (d × duration), same kind as the
    # pooled capacity it replaces. Ores are derived from the reachable mining
    # pairs, so an ore needing un-researched tech (e.g. uranium) never appears.
    drill_assign: dict[tuple[str, int], object] = {}
    if ELECTRIC_MINING_DRILL in inst.reachable_buildings:
        drill = model.buildings[ELECTRIC_MINING_DRILL]
        drill_speed = drill.base_speed
        drill_fp = inst.deployed_facility(model, drill).tile_footprint
        drill_end_w = inst.capacity_end_weight(ELECTRIC_MINING_DRILL)
        # An ore is worth modeling only if something downstream uses it —
        # consumed as a recipe ingredient or burned as fuel. uranium-ore is
        # mineable in the data (enabled_at_start) but has no consumer without
        # uranium-processing research, so on default-victory it's dead weight.
        # Skip those ores (no per-ore vars) and force their electric-drill
        # mining to zero (you don't place drills on ore nobody uses).
        needed_ores: set[str] = set()
        for step in inst.steps:
            for r in step.available_recipes(model):
                if r.kind == "mining":
                    continue
                for ing in r.ingredients:
                    needed_ores.add(ing.name)
        for n, it in model.items.items():
            if it.fuel_value_j:
                needed_ores.add(n)

        # ore -> mining recipe names actually paired with the electric drill.
        ore_recipes: dict[str, set[str]] = {}
        unused_drill_mining: list[tuple[str, str, int]] = []
        for r_name, b_name, i in x_real:
            if b_name != ELECTRIC_MINING_DRILL:
                continue
            r = model.recipes.get(r_name)
            if r is None or r.kind != "mining":
                continue
            ore = r.outputs[0].name if r.outputs else None
            if ore in needed_ores:
                ore_recipes.setdefault(ore, set()).add(r_name)
            else:
                unused_drill_mining.append((r_name, b_name, i))

        # Forbid mining unused ores on electric drills (no demand, no vars).
        for key in unused_drill_mining:
            m.addCons(x_real[key] == 0.0, name=_safe(f"no_mine_{key[0]}_{key[2]}"))

        for ore in sorted(ore_recipes):
            # UB: drills that physically fit on this ore's patch (finite box
            # for the McCormick envelope of the d × duration term).
            if ore in inst.tile_pool and drill_fp > 0:
                ore_ub = inst.tile_pool[ore] / drill_fp
            else:
                ore_ub = _building_count_ub(ELECTRIC_MINING_DRILL)
            for tier in range(n_tiers):
                drill_assign[(ore, tier)] = m.addVar(
                    name=_safe(f"drill_{ore}_{tier}"), lb=0.0, ub=ore_ub, vtype="C"
                )

        for ore, recipes in ore_recipes.items():
            for i in range(n_steps):
                # Per-ore capacity, mirroring the pooled form but on d[ore,·].
                eff_terms = []
                if (1.0 - drill_end_w) > 0:
                    eff_terms.append((1.0 - drill_end_w) * drill_assign[(ore, i)])
                if drill_end_w > 0:
                    eff_terms.append(drill_end_w * drill_assign[(ore, i + 1)])
                lhs = quicksum(
                    recipe_time.get(r, 0.0) * var
                    for (r_name, b_name, ii), var in x_real.items()
                    if ii == i and b_name == ELECTRIC_MINING_DRILL and r_name in recipes
                    for r in (r_name,)
                )
                m.addCons(
                    lhs <= quicksum(eff_terms) * drill_speed * duration_vars[i],
                    name=_safe(f"cap_drill_{ore}_{i}"),
                )

        for tier in range(n_tiers):
            # Assignments sum to the (single) drill count that drives area /
            # infra / player-time, so the split never inflates those.
            m.addCons(
                quicksum(drill_assign[(ore, tier)] for ore in ore_recipes)
                <= item_vars[(ELECTRIC_MINING_DRILL, tier)],
                name=_safe(f"drill_total_{tier}"),
            )
            # Non-decreasing per ore: a drill placed on an ore stays there.
            if tier + 1 < n_tiers:
                for ore in ore_recipes:
                    m.addCons(
                        drill_assign[(ore, tier + 1)] >= drill_assign[(ore, tier)],
                        name=_safe(f"drill_mono_{ore}_{tier}"),
                    )

    # --- steel-furnace: per-output assignment (a furnace can't switch product) ---
    #
    # Exactly the electric-drill story, one tier up the chain: a steel furnace
    # fed iron-ore can't be retasked to smelt copper between steps. Pooled
    # capacity would let the LP melt iron now and copper later on the "same"
    # furnaces, flattening nothing. So each smelted output gets its own
    # furnace-assignment count that is non-decreasing (no repurposing) and sums
    # to the steel-furnace total. First-class, persisted as
    # `steel-furnace@<output>` (e.g. steel-furnace@iron-plate) for L3 placement
    # and the viz. Bilinear (count × duration), the same kind as the pooled
    # capacity it replaces — kept minimal by disabling electric-furnace smelting
    # and leaving stone furnaces pooled.
    furnace_assign: dict[tuple[str, int], object] = {}
    if STEEL_FURNACE in inst.reachable_buildings:
        furnace = model.buildings[STEEL_FURNACE]
        furnace_speed = furnace.base_speed
        furnace_end_w = inst.capacity_end_weight(STEEL_FURNACE)
        # smelted output item -> smelting recipe names paired with steel-furnace.
        out_recipes: dict[str, set[str]] = {}
        for r_name, b_name, _i in x_real:
            if b_name != STEEL_FURNACE:
                continue
            r = model.recipes.get(r_name)
            if r is None or not r.outputs:
                continue
            out_recipes.setdefault(r.outputs[0].name, set()).add(r_name)

        for out in sorted(out_recipes):
            out_ub = _building_count_ub(STEEL_FURNACE)
            for tier in range(n_tiers):
                furnace_assign[(out, tier)] = m.addVar(
                    name=_safe(f"furnace_{out}_{tier}"), lb=0.0, ub=out_ub, vtype="C"
                )

        for out, recipes in out_recipes.items():
            for i in range(n_steps):
                # Per-output capacity, mirroring the pooled form on f[out,·].
                eff_terms = []
                if (1.0 - furnace_end_w) > 0:
                    eff_terms.append((1.0 - furnace_end_w) * furnace_assign[(out, i)])
                if furnace_end_w > 0:
                    eff_terms.append(furnace_end_w * furnace_assign[(out, i + 1)])
                lhs = quicksum(
                    recipe_time.get(r_name, 0.0) * var
                    for (r_name, b_name, ii), var in x_real.items()
                    if ii == i and b_name == STEEL_FURNACE and r_name in recipes
                )
                m.addCons(
                    lhs <= quicksum(eff_terms) * furnace_speed * duration_vars[i],
                    name=_safe(f"cap_furnace_{out}_{i}"),
                )

        for tier in range(n_tiers):
            # Assignments sum to the (single) furnace count that drives area /
            # infra / player-time, so the split never inflates those.
            m.addCons(
                quicksum(furnace_assign[(out, tier)] for out in out_recipes)
                <= item_vars[(STEEL_FURNACE, tier)],
                name=_safe(f"furnace_total_{tier}"),
            )
            # Non-decreasing per output: a furnace committed to a product stays.
            if tier + 1 < n_tiers:
                for out in out_recipes:
                    m.addCons(
                        furnace_assign[(out, tier + 1)] >= furnace_assign[(out, tier)],
                        name=_safe(f"furnace_mono_{out}_{tier}"),
                    )

    # --- spatial caps (unconditional) + infrastructure reservation (flag-gated) ---
    #
    # Spatial caps make the LP's bilinear relaxation behave: without them,
    # `count × duration` can be driven huge-and-short at the relaxation,
    # SCIP fails to find a primal on default-victory. With them, extraction
    # density is hard-capped by the L3 map data.
    #
    # Per-resource spatial cap, per (resource r, mining-building b, step i):
    #   Σ x[r',b,i] · recipe_time[r']
    #     ≤ (tile_pool[r] / facility(b).tile_footprint)
    #       · facility(b).speed · duration[i]
    #
    # The LHS is the same recipe-seconds sum as the capacity constraint above.
    # The RHS swaps `effective_count` for `tile_pool/footprint` — the max
    # number of buildings of type b that physically fit on resource r. So
    # the constraint says: extraction rate is capped by what would fit on
    # the patch, regardless of how many drills the LP wants to build.
    #
    # Linear (tile_pool and footprint are constants); no new variables.
    if inst.tile_pool:
        # Pre-compute: resource → list of (mining-recipe-name, output-amount).
        # Only kind=="mining" recipes; pumping (oil) needs a different
        # constraint shape and isn't covered until pumpjack lands in the
        # deployment registry.
        mining_recipes_by_resource: dict[str, list[str]] = {}
        for r in model.recipes.values():
            if r.kind != "mining":
                continue
            for out in r.outputs:
                if out.name in inst.tile_pool:
                    mining_recipes_by_resource.setdefault(out.name, []).append(r.name)

        for resource, recipe_names in mining_recipes_by_resource.items():
            tile_pool = inst.tile_pool[resource]
            for b_name, b in model.buildings.items():
                if b.kind != "mining-drill":
                    continue
                facility = inst.deployed_facility(model, b)
                if facility.tile_footprint <= 0:
                    continue
                max_drills = tile_pool / facility.tile_footprint
                for i in range(n_steps):
                    terms = []
                    for r_name in recipe_names:
                        key = (r_name, b_name, i)
                        if key not in x_real:
                            continue
                        t = recipe_time.get(r_name, 0.0)
                        if t == 0:
                            continue
                        terms.append(t * x_real[key])
                    if not terms:
                        continue
                    rhs = max_drills * facility.speed * duration_vars[i]
                    m.addCons(
                        quicksum(terms) <= rhs,
                        name=_safe(f"space_{resource}_{b_name}_{i}"),
                    )

    # Total-area cap, per step:
    #   Σ_all_buildings item[b, i] · facility(b).tile_footprint
    #     ≤ max_area_fraction · map_area
    #
    # Every building contributes via its Facility footprint (deployment-
    # override-or-base). Without this, the LP's bilinear `count × duration`
    # term lets total building density blow up at low t_FINAL and SCIP
    # never finds a primal on default-victory.
    if inst.map_area > 0 and inst.max_area_fraction > 0:
        area_budget = inst.max_area_fraction * inst.map_area
        for i in range(n_tiers):
            terms = []
            for b_name in model.buildings:
                if b_name not in inst.reachable_buildings:
                    continue
                if (b_name, i) not in item_vars:
                    continue
                facility = inst.deployed_facility(model, model.buildings[b_name])
                if facility.tile_footprint <= 0:
                    continue
                terms.append(facility.tile_footprint * item_vars[(b_name, i)])
            if not terms:
                continue
            m.addCons(
                quicksum(terms) <= area_budget,
                name=_safe(f"map_area_{i}"),
            )

    # Per-oil-spot cap on pumpjacks (one pumpjack per spot):
    #   item[pumpjack, i] ≤ oil_spot_count
    # Same shape as a regular bound but goes through addCons so it
    # shows up alongside the other spatial caps in the model dump.
    if inst.oil_spot_count > 0 and "pumpjack" in inst.reachable_buildings:
        for i in range(n_tiers):
            if ("pumpjack", i) not in item_vars:
                continue
            m.addCons(
                item_vars[("pumpjack", i)] <= float(inst.oil_spot_count),
                name=_safe(f"oil_spots_{i}"),
            )

    # Per-water-perimeter cap on offshore-pumps (pumps sit on the perimeter
    # of a water body, ~4·√area each, summed across bodies):
    #   item[offshore-pump, i] ≤ water_pump_cap
    if inst.water_pump_cap > 0 and "offshore-pump" in inst.reachable_buildings:
        for i in range(n_tiers):
            if ("offshore-pump", i) not in item_vars:
                continue
            m.addCons(
                item_vars[("offshore-pump", i)] <= float(inst.water_pump_cap),
                name=_safe(f"water_pumps_{i}"),
            )

    # Whole-campaign wood budget: total wood consumed across every step
    # (real recipes + pseudo-recipes) ≤ the wood standing on the map
    # (tree_count × WOOD_PER_TREE). Wood is an excluded item — no per-step
    # balance constraint — but its finite map supply still bounds the
    # cumulative draw. One global linear constraint over the activity vars.
    if inst.wood_budget > 0:
        wood_terms = []
        for (r_name, _b, _i), var in x_real.items():
            amt = _ingredient_amount(r_name, "wood", model, pseudo_by_name)
            if amt > 0:
                wood_terms.append(amt * var)
        for (p_name, _i), var in x_pseudo.items():
            amt = _ingredient_amount(p_name, "wood", model, pseudo_by_name)
            if amt > 0:
                wood_terms.append(amt * var)
        # Hand-crafted wood consumers too (small-electric-pole, wooden-chest
        # are 'crafting'-category and hand-craftable).
        for (r_name, _i), var in x_hand.items():
            amt = _ingredient_amount(r_name, "wood", model, pseudo_by_name)
            if amt > 0:
                wood_terms.append(amt * var)
        if wood_terms:
            m.addCons(
                quicksum(wood_terms) <= float(inst.wood_budget),
                name=_safe("wood_budget"),
            )

    # Per-step player-time constraint. The single character acts serially,
    # so everything it must do during step i has to fit inside duration[i]:
    #
    #   Σ_b (newly-placed b in step i) · (walk_b + place_b)         [facilities]
    #   + (wood consumed in step i / WOOD_PER_TREE) / mining_rate[i] [tree felling]
    #   ≤ duration[i]
    #
    # walk_b = 2·√footprint / WALKING_SPEED (out to the build site and back);
    # place_b = (1 + Σ infra_entities) / 60 (one entity placed per game tick,
    # building + its belts/poles/inserters from Facility.infrastructure_items).
    # "Newly-placed" is the per-step delta item[b,i+1] − item[b,i] — counts
    # are non-decreasing and tier 0 is pinned to the initial inventory, so the
    # seeded character / bootstrap buildings are never charged. All terms are
    # linear in existing vars: no new variables, no bilinear coupling.
    if inst.player_time_enabled:
        place_time = {
            b_name: _placement_player_time(inst, b_name, model)
            for b_name in model.buildings
        }
        # Storage chests are placed entities too (1×1 footprint + their one
        # inserter), but they're items not buildings, so add them explicitly.
        # Divided by CHEST_SCALE because the chest variable counts 0.01-chest
        # units, so its per-step delta is CHEST_SCALE× the physical count.
        chest_place_time = (
            2.0 * math.sqrt(inst.cfg.chest_tile_footprint) / inst.cfg.walking_speed_tps
            + (1.0 + inst.cfg.chest_inserter_per) * inst.cfg.placement_tick_s
        ) / CHEST_SCALE
        for chest in STORAGE_CHESTS:
            place_time[chest] = chest_place_time
        # Per-cycle wood draw of every recipe that uses wood (real + pseudo).
        wood_per_recipe: dict[str, float] = {}
        for r_name, _b, _i in x_real:
            if r_name not in wood_per_recipe:
                wood_per_recipe[r_name] = _ingredient_amount(
                    r_name, "wood", model, pseudo_by_name
                )
        for p_name, _i in x_pseudo:
            if p_name not in wood_per_recipe:
                wood_per_recipe[p_name] = _ingredient_amount(
                    p_name, "wood", model, pseudo_by_name
                )
        for r_name, _i in x_hand:
            if r_name not in wood_per_recipe:
                wood_per_recipe[r_name] = _ingredient_amount(
                    r_name, "wood", model, pseudo_by_name
                )

        # steel-axe doubles the felling rate once researched; the research
        # step itself still uses the slow axe (completes at its end).
        steel_axe_step = next(
            (
                i
                for i, s in enumerate(inst.steps)
                if s.research and s.research.name == STEEL_AXE_RESEARCH
            ),
            None,
        )

        for i in range(n_steps):
            terms = []
            for b_name, pt in place_time.items():
                if pt <= 0:
                    continue
                if (b_name, i) in item_vars and (b_name, i + 1) in item_vars:
                    terms.append(
                        pt * (item_vars[(b_name, i + 1)] - item_vars[(b_name, i)])
                    )
            rate = (
                inst.cfg.tree_mining_rate_steelaxe
                if steel_axe_step is not None and i > steel_axe_step
                else inst.cfg.tree_mining_rate_base
            )
            wood_to_time = 1.0 / (inst.cfg.wood_per_tree * rate)
            for (r_name, _b, ii), var in x_real.items():
                if ii == i and wood_per_recipe.get(r_name, 0.0) > 0:
                    terms.append(wood_per_recipe[r_name] * wood_to_time * var)
            for (p_name, ii), var in x_pseudo.items():
                if ii == i and wood_per_recipe.get(p_name, 0.0) > 0:
                    terms.append(wood_per_recipe[p_name] * wood_to_time * var)
            # Felling the wood a hand-crafted pole/chest needs is still a
            # serial action even though the crafting itself is background.
            for (r_name, ii), var in x_hand.items():
                if ii == i and wood_per_recipe.get(r_name, 0.0) > 0:
                    terms.append(wood_per_recipe[r_name] * wood_to_time * var)
            if terms:
                m.addCons(
                    quicksum(terms) <= duration_vars[i], name=_safe(f"player_time_{i}")
                )

    # Hard cap on burner-mining-drills to force transition to electric
    # drills. Player conventionally bootstraps with hand-placed burner
    # drills then switches; without this cap the LP can keep stacking
    # burners (which have no infrastructure overhead and 0 power draw
    # in the LP's electric-balance) indefinitely.
    BURNER_DRILL_CAP = inst.cfg.burner_drill_cap
    if "burner-mining-drill" in inst.reachable_buildings:
        for i in range(n_tiers):
            if ("burner-mining-drill", i) not in item_vars:
                continue
            m.addCons(
                item_vars[("burner-mining-drill", i)] <= BURNER_DRILL_CAP,
                name=_safe(f"burner_cap_{i}"),
            )

    # Cap on pooled stone furnaces (the unsplit smelting building), forcing
    # the transition to per-output-committed steel furnaces — mirrors the
    # burner-drill cap. See inst.cfg.stone_furnace_cap.
    if "stone-furnace" in inst.reachable_buildings:
        for i in range(n_tiers):
            if ("stone-furnace", i) not in item_vars:
                continue
            m.addCons(
                item_vars[("stone-furnace", i)] <= inst.cfg.stone_furnace_cap,
                name=_safe(f"stone_furnace_cap_{i}"),
            )

    # Fluid-buffer cap. Surplus fluid held at any tier boundary must fit in
    # built fluid storage. A pipe/tank holds one fluid type at a time, so the
    # honest bound is the aggregate pool: total surplus across ALL fluids ≤
    # total storage capacity (the LP relaxation of dedicating each storage
    # entity to a single fluid and summing).
    #   Σ_f item[f, i] ≤ 100·pipe[i] + 100·pipe-to-ground[i]
    #                    + 25000·storage-tank[i]   for every tier i
    # Linear (capacities are constants), couples fluid surplus to the
    # storage-building counts — both existing vars, no new ones. Without it
    # the LP parks unbounded free fluid (e.g. ~3.8e9 water).
    fluid_items = sorted(
        n
        for n in tracked
        if n != "steam"  # collapsed into the boiler-engine pseudo-recipe
        and (it := model.items.get(n)) is not None
        and it.kind == "fluid"
    )
    if fluid_items:
        for i in range(n_tiers):
            surplus = quicksum(
                item_vars[(f, i)] for f in fluid_items if (f, i) in item_vars
            )
            cap = quicksum(
                c * item_vars[(b, i)]
                for b, c in FLUID_STORAGE_CAPACITY
                if (b, i) in item_vars
            )
            m.addCons(surplus <= cap, name=_safe(f"fluid_buffer_{i}"))

    # Solid-item banking cap. Banked surplus of the curated solid items must
    # fit in built stack-slot storage. Per item the surplus is expressed in
    # stacks (item[n,i] / stack_size[n]) minus one stack assumed in transit
    # on the belt; that −1 is allowed to go negative (an item below a stack
    # frees belt space — inter-recipe belt buffering isn't modeled), which
    # keeps the constraint linear. Per tier i:
    #   Σ_n (item[n,i]/stack_size[n] − 1)
    #     ≤ 32·iron-chest[i] + 48·steel-chest[i] + 80   (player)
    # Slot counts come from game data (container inventory_size; character 80).
    # Everything is in centi-stack units (×CHEST_SCALE): the per-item stack
    # term (CHEST_SCALE/stack_size)·item is ×100 larger, lifting its coefficient
    # off the conditioning floor, while the chest variables (already rescaled to
    # 0.01-chest units in net_coefs) keep their inventory_size coefficient.
    # Player inventory is a bounded variable, not a large RHS constant — coef 1,
    # the 8000 lives in its upper bound.
    banked = sorted(
        n
        for n in BANKED_STORAGE_ITEMS
        if (it := model.items.get(n)) is not None and it.stack_size
    )
    if banked:
        chest_slots = [
            (b, float(model.items[b].inventory_size))
            for b in STORAGE_CHESTS
            if model.items.get(b) is not None and model.items[b].inventory_size
        ]
        player_cap = PLAYER_INVENTORY_SLOTS * CHEST_SCALE
        for i in range(n_tiers):
            stacks = quicksum(
                (CHEST_SCALE / model.items[n].stack_size) * item_vars[(n, i)]
                for n in banked
                if (n, i) in item_vars
            )
            n_present = sum(1 for n in banked if (n, i) in item_vars)
            slots = quicksum(
                slot * item_vars[(b, i)]
                for b, slot in chest_slots
                if (b, i) in item_vars
            )
            player_space = m.addVar(
                name=_safe(f"player_space_{i}"), lb=0.0, ub=player_cap, vtype="C"
            )
            # stacks − (1 stack/item, scaled) ≤ chest slots + player slots
            m.addCons(
                stacks - n_present * CHEST_SCALE <= slots + player_space,
                name=_safe(f"item_banking_{i}"),
            )

    # Infrastructure reservation, per (infra item k, step i):
    #   item[k, i] ≥ Σ_b item[b, i] · infra_per_building[b][k]
    #
    # Reads: cumulative production of infra item k must cover all deployed
    # buildings' shared/persistent commitment of k. Belts placed on the
    # ground for a drill row aren't available to be consumed by other
    # recipes. Soft "reservation" rather than per-cycle consumption,
    # because the LP's count is non-decreasing — a once-deployed belt
    # stays deployed.
    if inst.deployment_enabled:
        # Gather (building, infra_item, per-building-amount). Skip
        # buildings without a deployment pattern (empty dict).
        infra_terms_by_item: dict[str, list[tuple[str, float]]] = {}
        for b_name, b in model.buildings.items():
            if b_name not in inst.reachable_buildings:
                continue
            facility = inst.deployed_facility(model, b)
            for k, amt in facility.infrastructure_items.items():
                if amt > 0:
                    infra_terms_by_item.setdefault(k, []).append((b_name, amt))

        # Storage chests (solid-item banking cap) each need one inserter to
        # load them. Chests are items, not buildings, so add their inserter
        # reservation explicitly. Divided by CHEST_SCALE because the chest
        # variable is in 0.01-chest units (1 inserter per 100 such units).
        for chest in STORAGE_CHESTS:
            if chest in tracked:
                infra_terms_by_item.setdefault("inserter", []).append(
                    (chest, inst.cfg.chest_inserter_per / CHEST_SCALE)
                )

        for k, contributors in infra_terms_by_item.items():
            if k not in tracked:
                # Infra item isn't tracked (not in any recipe touched by this
                # scenario). Warn-and-skip rather than silently miss; loud
                # failure is preferable to a missing constraint.
                # Surfaces a real gap: typically means the infra recipe
                # wasn't tech-gated in by this scenario's L1 closure.
                continue
            for i in range(n_tiers):
                # Constraint applies to each item-tier (boundary between
                # steps). item[b, i] is the building count at boundary i.
                #
                # Initial-state buildings (character stand-in, bootstrap
                # seed, user-supplied initial inventory) come with their
                # infra implicitly — they're either non-physical (the
                # character isn't a real AM1) or magicked-in for
                # bootstrap purposes. Only buildings BUILT during the
                # scenario trigger infra reservation. Subtract the
                # initial contribution out so the constraint only binds
                # on the post-initial delta.
                building_terms = []
                initial_offset = 0.0
                for b_name, amt in contributors:
                    if (b_name, i) not in item_vars:
                        continue
                    building_terms.append(amt * item_vars[(b_name, i)])
                    initial_offset += amt * initial_items.get(b_name, 0.0)
                if not building_terms:
                    continue
                m.addCons(
                    item_vars[(k, i)] + initial_offset >= quicksum(building_terms),
                    name=_safe(f"infra_{k}_{i}"),
                )

    # --- energy balance: per-burner-building (fuel) and per-step (electric) ---

    # Per-(burner-building, step) fuel-energy balance, in MJ:
    #   Σ_fuels fuel_burn[f,b,i] · fuel_value_mj[f]
    #     ≥ Σ_recipes x[r,b,i] · recipe_time[r] · base_power_mw[b] / base_speed[b]
    # The /speed factor: x cycles × recipe_time gives recipe-seconds;
    # one wall-second of a building running at speed s eats s
    # recipe-seconds, so wall-time = recipe-time / s and energy
    # consumed = power × wall-time = recipe-time × power / speed.
    for b_name, b in burner_buildings.items():
        b_power_mw = b.base_power_w / _J_PER_MJ
        for i in range(n_steps):
            demand_terms = []
            for (r_name, b2, i2), var in x_real.items():
                if b2 != b_name or i2 != i:
                    continue
                t = recipe_time.get(r_name, 0.0)
                if t == 0:
                    continue
                demand_terms.append(t * b_power_mw / b.base_speed * var)
            if not demand_terms:
                continue
            supply_terms = [
                (model.items[fuel].fuel_value_j / _J_PER_MJ)
                * fuel_burn[(fuel, b_name, i)]
                for fuel in allowed_fuels
                if (fuel, b_name, i) in fuel_burn
            ]
            m.addCons(
                quicksum(demand_terms) <= quicksum(supply_terms),
                name=_safe(f"fuel_energy_{b_name}_{i}"),
            )

    # Per-step electric energy balance:
    #   Σ electric-consumer demand  ≤  Σ burn supply
    # The player draws no grid power — hand-crafting (x_hand) is a separate,
    # power-free facility, so it never appears in the demand sum. Built AM1s,
    # being real electric machines, do draw and contribute here normally.
    # (The old char_credit carve-out — crediting up to 2 AM1s' draw to "the
    # player" — is gone with the 2×AM1 stand-in it existed to offset.)
    # Burns and burner buildings contribute nothing here (they're not
    # electric); their boilers/steam-engines have base_power_w that's
    # heat-output / 0 respectively, not grid draw.
    elec_demand_lin: dict[int, object] = {}
    elec_supply_lin: dict[int, object] = {}

    # All energy terms below are in MJ (or MW × duration). The
    # rescaling matches the per-burner block above; SCIP works in
    # MJ/MW internally to keep coefficients near unity.
    for i in range(n_steps):
        demand_terms = []
        # Real-recipe electric demand.
        for (r_name, b_name, i2), var in x_real.items():
            if i2 != i:
                continue
            b = model.buildings.get(b_name)
            if b is None or b.energy_source_type != "electric":
                continue
            if b.base_power_w <= 0:
                continue
            t = recipe_time.get(r_name, 0.0)
            if t == 0:
                continue
            b_power_mw = b.base_power_w / _J_PER_MJ
            term = t * b_power_mw / b.base_speed * var
            demand_terms.append(term)
        # Pseudo-recipe electric demand (lab is electric).
        for (p_name, i2), var in x_pseudo.items():
            if i2 != i:
                continue
            p = pseudo_by_name[p_name]
            t = recipe_time.get(p_name, 0.0)
            if t == 0:
                continue
            for b_name, weight in p.capacity_per_building:
                b = model.buildings.get(b_name)
                if b is None or b.energy_source_type != "electric":
                    continue
                if b.base_power_w <= 0:
                    continue
                b_power_mw = b.base_power_w / _J_PER_MJ
                demand_terms.append(t * weight * b_power_mw / b.base_speed * var)

        # Burn supply (MJ per cycle, x in cycles).
        supply_terms = [
            (B.electric_output_j_per_cycle / _J_PER_MJ) * x_pseudo[(B.name, i)]
            for B in inst.burns
            if (B.name, i) in x_pseudo and B.electric_output_j_per_cycle > 0
        ]

        elec_demand_lin[i] = quicksum(demand_terms)
        elec_supply_lin[i] = quicksum(supply_terms)

        m.addCons(
            elec_demand_lin[i] <= elec_supply_lin[i],
            name=f"electric_balance_{i}",
        )

    # Objective: minimize t_FINAL = Σ duration[i].
    m.setObjective(quicksum(duration_vars.values()), sense="minimize")

    handles = {
        "x_real": x_real,
        "x_pseudo": x_pseudo,
        "x_hand": x_hand,
        "item": item_vars,
        "duration": duration_vars,
        "drill_assign": drill_assign,
        "furnace_assign": furnace_assign,
        "fuel_burn": fuel_burn,
        "elec_demand_lin": elec_demand_lin,
        "elec_supply_lin": elec_supply_lin,
        "tracked_items": tracked,
        "n_tiers": n_tiers,
        "pseudo_by_name": pseudo_by_name,
    }
    return m, handles


# --- Fluid storage (capacity per built entity, one fluid type each) --------
# Vanilla 1.1: pipe and pipe-to-ground each hold 100, storage-tank 25000.
# The storage-tank variable is rescaled by STORAGE_TANK_SCALE (see
# _scale_item_units) so it counts 0.01-tank sub-units (250 fluid each)
# instead of whole tanks — that turns the 25000 fluid-buffer coefficient
# into 250 and pulls the worst within-row ratio from 25000 toward ~250.
STORAGE_TANK_SCALE = 100.0
FLUID_STORAGE_CAPACITY = (
    ("pipe", 100.0),
    ("pipe-to-ground", 100.0),
    ("storage-tank", 25000.0 / STORAGE_TANK_SCALE),
)

# --- Solid-item banking (stack-slot storage) -------------------------------
# Curated set of non-fluid, non-infrastructure items observed banking beyond
# 1× stack in the reference solve (seed 1007). The banking constraint sums
# only these so the per-item "−1 stack" belt-space credit can't manufacture
# phantom storage across the whole item list. Data-derived; revise as the
# plan's banking profile shifts. Infra items (belts/poles/inserters/pipes)
# are excluded — their surplus is deployed on the ground, not chest-banked.
BANKED_STORAGE_ITEMS = frozenset(
    {
        "coal",
        "copper-ore",
        "iron-ore",
        "stone",
        "iron-plate",
        "copper-plate",
        "steel-plate",
        "solid-fuel",
        "engine-unit",
        "plastic-bar",
        "rocket-fuel",
        "sulfur",
        "copper-cable",
        "rail",
        "stone-brick",
        "iron-gear-wheel",
        "iron-stick",
        "low-density-structure",
        "concrete",
        "electric-engine-unit",
        "electronic-circuit",
        "processing-unit",
        "advanced-circuit",
        "beacon",
        "productivity-module",
        "rocket-control-unit",
        "automation-science-pack",
        "logistic-science-pack",
        "chemical-science-pack",
        "production-science-pack",
    }
)
# Stack-slot storage entities. Per-chest slot counts come from the game data
# (container inventory_size); the player's 80 slots are the vanilla character
# inventory. Chests each require one inserter (infra) and player-time to place.
PLAYER_INVENTORY_SLOTS = 80.0
STORAGE_CHESTS = ("iron-chest", "steel-chest")
# The chest variables are rescaled to 0.01-chest units (recipe yields 100/
# craft, like the storage-tank), and the banking constraint is written in the
# matching 0.01-stack ("centi-stack") units. Together that keeps the chest
# coefficient at its inventory_size (32/48) while the per-item stack coefficient
# (1/stack_size) is ×100 larger — pulling item_banking's within-row ratio from
# ~9600 down to ~96 and raising the global coefficient floor. Chest infra /
# player-time / output are divided back by CHEST_SCALE to stay physical.
CHEST_SCALE = 100.0

# Electric mining drills can't switch what ore they mine (placed on a patch),
# so their capacity is split per-ore (see the per-ore assignment block).
ELECTRIC_MINING_DRILL = "electric-mining-drill"

# Steel furnaces can't switch what they smelt either (a furnace fed iron-ore
# can't become a copper smelter mid-run), so they get the same per-output
# assignment split as the electric drill (see the steel-furnace block).
STEEL_FURNACE = "steel-furnace"
# Each extra smelting building multiplies the per-output bilinear capacity
# terms. To keep that minimal we disable electric-furnace smelting outright
# and split only the steel furnace; stone furnaces stay pooled (capped, see
# inst.cfg.stone_furnace_cap) because splitting them would also entangle the early
# stone-furnace/boiler bootstrap.
# Cap on pooled (unsplit) stone-furnace count, forcing transition to the
# split steel furnaces — the smelting analogue of BURNER_DRILL_CAP. Stone
# furnaces are transitional bootstrap smelters; without a cap the LP would
# lean on their pooled flexibility instead of committing steel furnaces to
# specific products.


# --- Player-time model (single character, serial actions per step) ---------
STEEL_AXE_RESEARCH = "research/steel-axe"

# Utilization at/above which a building counts as a binding bottleneck.
# Utilization itself (recipe-seconds-used / capacity-seconds) is the
# continuous criticality signal L3 consumes; `saturated` is just the
# convenience flag for the binding tail. See docs/L2-to-L3-handoff.md
# Theme 2 — this is the constraint-tightness signal L2's solver uniquely
# knows and would otherwise discard at the boundary.
SATURATION_THRESHOLD = 0.98


def _placement_player_time_parts(
    inst: L2Instance, b_name: str, model: GameModel
) -> tuple[float, float]:
    """(walk, place) seconds to place one building of type `b_name`: walk
    out to a footprint-sized site and back (2·√A / speed), and one game
    tick per placed entity (the building itself + its amortized
    infrastructure: belts, poles, inserters). (0, 0) for unknown buildings."""
    b = model.buildings.get(b_name)
    if b is None:
        return 0.0, 0.0
    facility = inst.deployed_facility(model, b)
    fp = facility.tile_footprint
    walk = 2.0 * math.sqrt(fp) / inst.cfg.walking_speed_tps if fp > 0 else 0.0
    infra = sum((facility.infrastructure_items or {}).values())
    place = (1.0 + infra) * inst.cfg.placement_tick_s
    return walk, place


def _placement_player_time(inst: L2Instance, b_name: str, model: GameModel) -> float:
    """Total per-building placement player-time (walk + place). See
    `_placement_player_time_parts` for the breakdown."""
    walk, place = _placement_player_time_parts(inst, b_name, model)
    return walk + place


def _ingredient_amount(
    r_name: str,
    item_name: str,
    model: GameModel,
    pseudo_by_name: dict[str, PseudoRecipe],
) -> float:
    """Per-cycle consumption of `item_name` by recipe (real or pseudo)
    `r_name`. 0 if not an ingredient.
    """
    r = model.recipes.get(r_name)
    if r is not None:
        for s in r.ingredients:
            if s.name == item_name:
                return s.amount
        return 0.0
    p = pseudo_by_name.get(r_name)
    if p is not None:
        for n, amt in p.ingredients:
            if n == item_name:
                return amt
    return 0.0


def _empty_solution(status: str, n_vars: int, n_constrs: int) -> Solution:
    return Solution(
        status=status,
        objective=None,
        x_real={},
        x_pseudo={},
        x_hand={},
        item={},
        duration={},
        drill_assign={},
        furnace_assign={},
        excluded_consumed={},
        fuel_burn={},
        electric_demand={},
        electric_supply={},
        n_vars=n_vars,
        n_constrs=n_constrs,
    )


def _effective_branching_factor(total_nodes: int, max_depth: int) -> float | None:
    """Effective branching factor of the spatial-B&B tree.

    SCIP's spatial branching is binary (one variable's domain split in
    two → 2 children), so the *structural* factor is 2 by construction;
    what varies — and what this reports — is the *effective* one after
    cutoffs and pruning. For a tree of N nodes reaching depth d, a
    balanced shape gives N ≈ b**d, so b_eff = N**(1/d): →1 means a deep
    narrow spine (almost every node pruned to a single child), →2 means a
    full, barely-pruned binary tree. Returns None when there was no
    branching (solved at the root) so the ratio is undefined.
    """
    if max_depth <= 0 or total_nodes <= 1:
        return None
    return total_nodes ** (1.0 / max_depth)


def _solver_diagnostics(m: Model) -> dict:
    """Best-effort post-solve diagnostics. SCIP getters may raise on
    certain statuses (e.g. before optimize is called); guard each."""
    out: dict = {}
    for name, getter in (
        ("n_nodes", lambda: int(m.getNNodes())),
        ("n_total_nodes", lambda: int(m.getNTotalNodes())),
        ("max_depth", lambda: int(m.getMaxDepth())),
        ("gap", lambda: float(m.getGap())),
        ("dual_bound", lambda: float(m.getDualbound())),
        ("solve_time_s", lambda: float(m.getSolvingTime())),
    ):
        try:
            out[name] = getter()
        except Exception:
            out[name] = None if name == "dual_bound" else 0
    out["branching_factor"] = _effective_branching_factor(
        out.get("n_total_nodes", 0) or 0, out.get("max_depth", 0) or 0
    )
    return out


def _dump_bnb_stats(diag: dict) -> None:
    """Print the B&B tree shape. SCIP's printStatistics() reports raw node
    counts and max depth but never an effective branching factor; this
    surfaces all three together (see _effective_branching_factor)."""
    total = diag.get("n_total_nodes", 0) or 0
    depth = diag.get("max_depth", 0) or 0
    bf = diag.get("branching_factor")
    print(f"[bnb-tree] total nodes (all runs): {total}", flush=True)
    print(f"[bnb-tree] max depth:              {depth}", flush=True)
    if bf is not None:
        print(
            f"[bnb-tree] effective branching factor (nodes^(1/depth)): {bf:.4f}",
            flush=True,
        )
    else:
        print(
            "[bnb-tree] effective branching factor: n/a "
            "(no branching — solved at root)",
            flush=True,
        )


def _dump_constraint_stats(m: Model, verbose: bool = False) -> dict:
    """Walk all linear constraints and report coefficient-magnitude
    statistics. Helps diagnose ill-conditioning: if min and max
    coefficients span many orders of magnitude, the LP solver may
    struggle with numerical precision. Nonlinear constraints aren't
    introspectable through this API path (pyscipopt's NLP surface is
    thinner), so they're counted but not analyzed.
    """
    import math

    n_linear = 0
    n_nonlinear = 0
    coef_min_abs = math.inf
    coef_max_abs = 0.0
    worst_ratio = 1.0
    worst_cons_name = ""
    extreme_lines: list[str] = []
    # Track DISTINCT |coefficient| values across the whole model (deduped to
    # 6 significant figures), each with a representative (constraint, variable)
    # and a count of how many terms share it. Reporting distinct values keeps
    # the top/bottom-5 informative — otherwise the list is just the same
    # 25000 repeated once per step. Keyed by the rounded value; the distinct-
    # value count is small (coefficients are recipe times, fuel values, slot
    # counts, …), so the dict stays tiny.
    coef_reps: dict[float, list] = {}  # rounded |coef| -> [cons, var, count]
    for cons in m.getConss():
        ctype = cons.getConshdlrName()
        if ctype == "linear":
            n_linear += 1
            try:
                vals = m.getValsLinear(cons)
            except Exception:
                continue
            if not vals:
                continue
            abss = []
            for var, v in vals.items():
                if v == 0.0:
                    continue
                a = abs(v)
                abss.append(a)
                key = float(f"{a:.6e}")
                rep = coef_reps.get(key)
                if rep is None:
                    vname = getattr(var, "name", None) or str(var)
                    coef_reps[key] = [cons.name, vname, 1]
                else:
                    rep[2] += 1
            if not abss:
                continue
            local_min = min(abss)
            local_max = max(abss)
            coef_min_abs = min(coef_min_abs, local_min)
            coef_max_abs = max(coef_max_abs, local_max)
            ratio = local_max / local_min if local_min > 0 else math.inf
            if ratio > worst_ratio:
                worst_ratio = ratio
                worst_cons_name = cons.name
            if ratio >= 1e8:
                extreme_lines.append(
                    f"  {cons.name}: ratio={ratio:.2e} "
                    f"(min={local_min:.2e}, max={local_max:.2e})"
                )
        else:
            n_nonlinear += 1
    print(f"[constraint-stats] linear={n_linear} nonlinear={n_nonlinear}", flush=True)
    print(
        f"[constraint-stats] coef abs: min={coef_min_abs:.3e} max={coef_max_abs:.3e}",
        flush=True,
    )
    print(
        f"[constraint-stats] worst per-constraint coef ratio: "
        f"{worst_ratio:.3e} ({worst_cons_name})",
        flush=True,
    )
    distinct = sorted(coef_reps)
    top5 = [(k, *coef_reps[k]) for k in distinct[-5:][::-1]]
    bot5 = [(k, *coef_reps[k]) for k in distinct[:5]]
    print(
        f"[constraint-stats] 5 largest distinct |coef| (of {len(distinct)} distinct):",
        flush=True,
    )
    for a, cname, vname, cnt in top5:
        print(f"    {a:.3e}  ×{cnt:<5d} e.g. {cname} · {vname}", flush=True)
    print("[constraint-stats] 5 smallest distinct |coef|:", flush=True)
    for a, cname, vname, cnt in bot5:
        print(f"    {a:.3e}  ×{cnt:<5d} e.g. {cname} · {vname}", flush=True)
    if extreme_lines and verbose:
        print(
            f"[constraint-stats] {len(extreme_lines)} constraint(s) with ratio ≥ 1e8:",
            flush=True,
        )
        for line in extreme_lines[:20]:
            print(line, flush=True)
        if len(extreme_lines) > 20:
            print(f"  ... and {len(extreme_lines) - 20} more", flush=True)
    return {
        "n_linear": n_linear,
        "n_nonlinear": n_nonlinear,
        "coef_min_abs": coef_min_abs,
        "coef_max_abs": coef_max_abs,
        "worst_ratio": worst_ratio,
        "worst_cons_name": worst_cons_name,
        "n_extreme": len(extreme_lines),
        "largest_coefs": top5,
        "smallest_coefs": bot5,
    }


def solve(
    inst: L2Instance,
    model: GameModel,
    verbose: bool = False,
    time_limit_s: float | None = None,
    gap_limit: float | None = None,
    stall_nodes: int | None = None,
    node_limit: int | None = None,
    seed: int | None = None,
    lp_algorithm: str | None = None,
) -> tuple[Solution, Model, dict]:
    m, handles = build_lp(
        inst,
        model,
        verbose=verbose,
        time_limit_s=time_limit_s,
        gap_limit=gap_limit,
        stall_nodes=stall_nodes,
        node_limit=node_limit,
        seed=seed,
        lp_algorithm=lp_algorithm,
    )
    _dump_constraint_stats(m, verbose=verbose)
    m.optimize()
    if verbose:
        # Full SCIP statistics dump (timing, LP iters, primal-heur table,
        # cut breakdown, branching) — invaluable for diagnosing why a
        # run failed to find a primal or stalled.
        m.printStatistics()
    status = m.getStatus()
    n_vars = m.getNVars()
    n_constrs = m.getNConss()
    diag = _solver_diagnostics(m)
    if verbose:
        _dump_bnb_stats(diag)

    # Statuses where no primal solution exists at all (infeasible /
    # not started). Early-termination statuses (timelimit, gaplimit,
    # stallnodelimit, nodelimit, userinterrupt) DO have a feasible
    # incumbent, which we want to extract just like an optimal one.
    has_solution = status == "optimal" or m.getNSols() > 0
    if not has_solution:
        sol = _empty_solution(status, n_vars, n_constrs)
        sol.n_nodes = diag.get("n_nodes", 0) or 0
        sol.n_total_nodes = diag.get("n_total_nodes", 0) or 0
        sol.max_depth = diag.get("max_depth", 0) or 0
        sol.branching_factor = diag.get("branching_factor")
        sol.gap = diag.get("gap", 0.0) or 0.0
        sol.dual_bound = diag.get("dual_bound")
        sol.solve_time_s = diag.get("solve_time_s", 0.0) or 0.0
        return sol, m, handles

    tol = 1e-9
    x_real_sol = {
        k: m.getVal(v) for k, v in handles["x_real"].items() if m.getVal(v) > tol
    }
    x_pseudo_sol = {
        k: m.getVal(v) for k, v in handles["x_pseudo"].items() if m.getVal(v) > tol
    }
    x_hand_sol = {
        k: m.getVal(v) for k, v in handles["x_hand"].items() if m.getVal(v) > tol
    }
    item_sol = {
        k: m.getVal(v) for k, v in handles["item"].items() if abs(m.getVal(v)) > tol
    }
    duration_sol = {k: m.getVal(v) for k, v in handles["duration"].items()}
    drill_assign_sol = {
        k: m.getVal(v)
        for k, v in handles["drill_assign"].items()
        if abs(m.getVal(v)) > tol
    }
    furnace_assign_sol = {
        k: m.getVal(v)
        for k, v in handles["furnace_assign"].items()
        if abs(m.getVal(v)) > tol
    }
    fuel_burn_sol = {
        k: m.getVal(v) for k, v in handles["fuel_burn"].items() if m.getVal(v) > tol
    }

    # Recompute per-step electric demand/supply post-solve from the
    # extracted x values (more portable than evaluating SCIP linear-expr
    # objects directly, which lack a stable .getValue() interface).
    pseudo_by_name = handles["pseudo_by_name"]
    recipe_time = {r.name: r.time_seconds for r in model.recipes.values()}
    for p in pseudo_by_name.values():
        recipe_time[p.name] = p.time_seconds
    # Energy units: MJ (matches the LP rescaling).
    elec_demand_sol: dict[int, float] = {i: 0.0 for i in range(len(inst.steps))}
    elec_supply_sol: dict[int, float] = {i: 0.0 for i in range(len(inst.steps))}
    for (r_name, b_name, i), val in x_real_sol.items():
        b = model.buildings.get(b_name)
        if b is None or b.energy_source_type != "electric" or b.base_power_w <= 0:
            continue
        t = recipe_time.get(r_name, 0.0)
        elec_demand_sol[i] += val * t * (b.base_power_w / _J_PER_MJ) / b.base_speed
    for (p_name, i), val in x_pseudo_sol.items():
        p = pseudo_by_name[p_name]
        t = recipe_time.get(p_name, 0.0)
        for b_name, weight in p.capacity_per_building:
            b = model.buildings.get(b_name)
            if b is None or b.energy_source_type != "electric" or b.base_power_w <= 0:
                continue
            elec_demand_sol[i] += (
                val * t * weight * (b.base_power_w / _J_PER_MJ) / b.base_speed
            )
        if p.electric_output_j_per_cycle > 0:
            elec_supply_sol[i] += val * (p.electric_output_j_per_cycle / _J_PER_MJ)

    excluded_consumed: dict[str, float] = {ex: 0.0 for ex in inst.excluded_items}
    for (r_name, _b, _i), val in x_real_sol.items():
        for ex in inst.excluded_items:
            amt = _ingredient_amount(r_name, ex, model, pseudo_by_name)
            if amt > 0:
                excluded_consumed[ex] += amt * val
    for (p_name, _i), val in x_pseudo_sol.items():
        for ex in inst.excluded_items:
            amt = _ingredient_amount(p_name, ex, model, pseudo_by_name)
            if amt > 0:
                excluded_consumed[ex] += amt * val

    return (
        Solution(
            status=status,
            objective=m.getObjVal(),
            x_real=x_real_sol,
            x_pseudo=x_pseudo_sol,
            x_hand=x_hand_sol,
            item=item_sol,
            duration=duration_sol,
            drill_assign=drill_assign_sol,
            furnace_assign=furnace_assign_sol,
            excluded_consumed=excluded_consumed,
            fuel_burn=fuel_burn_sol,
            electric_demand=elec_demand_sol,
            electric_supply=elec_supply_sol,
            n_vars=n_vars,
            n_constrs=n_constrs,
            n_nodes=diag.get("n_nodes", 0) or 0,
            n_total_nodes=diag.get("n_total_nodes", 0) or 0,
            max_depth=diag.get("max_depth", 0) or 0,
            branching_factor=diag.get("branching_factor"),
            gap=diag.get("gap", 0.0) or 0.0,
            dual_bound=diag.get("dual_bound"),
            solve_time_s=diag.get("solve_time_s", 0.0) or 0.0,
        ),
        m,
        handles,
    )


def _capacity_utilization(
    inst: L2Instance,
    sol: Solution,
    model: GameModel,
    recipe_time: dict[str, float],
    pseudo_by_name: dict[str, PseudoRecipe],
) -> dict[int, list[dict]]:
    """Per-(building, step) capacity utilization, computed post-solve with
    the SAME formula as build_lp's capacity constraints:
    `recipe_seconds_used / capacity_seconds`, where capacity_seconds =
    effective_count · base_speed · duration. Utilization → 1 marks a binding
    bottleneck — the criticality signal L3 needs for the critical-path
    objective and late-hardening (docs/L2-to-L3-handoff.md Theme 2). This is
    information the solver already has; emitting it stops it being destroyed
    at the boundary. We read constraint *tightness* from the primal solution
    rather than SCIP duals (a nonconvex NLP doesn't give clean shadow
    prices). Split families (electric drills per ore, steel furnaces per
    output) report per-assignment rows labelled `building@target`, matching
    the mining_/smelting_assignment naming L3 already consumes.
    """
    tol = 1e-6
    n_steps = len(inst.steps)
    by_step: dict[int, list[dict]] = {i: [] for i in range(n_steps)}

    def _emit(i: int, label: str, lhs: float, cap: float) -> None:
        if lhs < tol and cap < tol:
            return
        util = (lhs / cap) if cap > tol else None
        by_step[i].append(
            {
                "building": label,
                "recipe_seconds_used": float(lhs),
                "capacity_seconds": float(cap),
                "utilization": float(util) if util is not None else None,
                "saturated": util is not None and util >= SATURATION_THRESHOLD,
            }
        )

    # Pooled buildings (everything but the two split families), mirroring the
    # cap_terms construction in build_lp: real recipes weight 1.0 on their
    # host building, pseudo-recipes one entry per capacity_per_building.
    cap_terms: dict[tuple[str, int], list[tuple[str, float, float]]] = {}
    for (r_name, b_name, i), v in sol.x_real.items():
        cap_terms.setdefault((b_name, i), []).append((r_name, v, 1.0))
    for (p_name, i), v in sol.x_pseudo.items():
        p = pseudo_by_name.get(p_name)
        if p is None:
            continue
        for b_name, weight in p.capacity_per_building:
            cap_terms.setdefault((b_name, i), []).append((p_name, v, weight))

    lab_mult = _lab_speed_mult(inst, model)
    for (b_name, i), entries in cap_terms.items():
        if b_name in (ELECTRIC_MINING_DRILL, STEEL_FURNACE):
            continue
        building = model.buildings.get(b_name)
        if building is None or not building.base_speed:
            continue
        if not any(recipe_time.get(r, 0.0) > 0.0 for r, _, _ in entries):
            continue
        end_w = inst.capacity_end_weight(b_name)
        start_w = 1.0 - end_w
        eff = start_w * float(sol.item.get((b_name, i), 0.0)) + end_w * float(
            sol.item.get((b_name, i + 1), 0.0)
        )
        lhs = sum(recipe_time.get(r, 0.0) * w * v for r, v, w in entries)
        speed = building.base_speed * (lab_mult[i] if b_name == "lab" else 1.0)
        cap = eff * speed * float(sol.duration.get(i, 0.0))
        _emit(i, b_name, lhs, cap)

    # Electric drills, per ore (capacity drawn only on that ore's assigned
    # drills — a drill on iron can't serve copper).
    drill = model.buildings.get(ELECTRIC_MINING_DRILL)
    if drill is not None and sol.drill_assign:
        drill_end_w = inst.capacity_end_weight(ELECTRIC_MINING_DRILL)
        ore_secs: dict[tuple[str, int], float] = {}
        for (r_name, b_name, i), v in sol.x_real.items():
            if b_name != ELECTRIC_MINING_DRILL:
                continue
            r = model.recipes.get(r_name)
            if r is None or r.kind != "mining" or not r.outputs:
                continue
            ore = r.outputs[0].name
            ore_secs[(ore, i)] = (
                ore_secs.get((ore, i), 0.0) + recipe_time.get(r_name, 0.0) * v
            )
        for ore in sorted({o for (o, _t) in sol.drill_assign}):
            for i in range(n_steps):
                eff = (1.0 - drill_end_w) * float(
                    sol.drill_assign.get((ore, i), 0.0)
                ) + drill_end_w * float(sol.drill_assign.get((ore, i + 1), 0.0))
                cap = eff * drill.base_speed * float(sol.duration.get(i, 0.0))
                _emit(
                    i,
                    f"{ELECTRIC_MINING_DRILL}@{ore}",
                    ore_secs.get((ore, i), 0.0),
                    cap,
                )

    # Steel furnaces, per output.
    furnace = model.buildings.get(STEEL_FURNACE)
    if furnace is not None and sol.furnace_assign:
        furnace_end_w = inst.capacity_end_weight(STEEL_FURNACE)
        out_secs: dict[tuple[str, int], float] = {}
        for (r_name, b_name, i), v in sol.x_real.items():
            if b_name != STEEL_FURNACE:
                continue
            r = model.recipes.get(r_name)
            if r is None or not r.outputs:
                continue
            out = r.outputs[0].name
            out_secs[(out, i)] = (
                out_secs.get((out, i), 0.0) + recipe_time.get(r_name, 0.0) * v
            )
        for out in sorted({o for (o, _t) in sol.furnace_assign}):
            for i in range(n_steps):
                eff = (1.0 - furnace_end_w) * float(
                    sol.furnace_assign.get((out, i), 0.0)
                ) + furnace_end_w * float(sol.furnace_assign.get((out, i + 1), 0.0))
                cap = eff * furnace.base_speed * float(sol.duration.get(i, 0.0))
                _emit(i, f"{STEEL_FURNACE}@{out}", out_secs.get((out, i), 0.0), cap)

    for i in by_step:
        by_step[i].sort(key=lambda u: -(u["utilization"] or 0.0))
    return by_step


def _per_step_records(
    inst: L2Instance,
    sol: Solution,
    model: GameModel,
) -> list[dict]:
    """Build per-step output records: activity, fuel-burn, energy
    (joules + watts), per-item production/consumption (units + per-sec
    rates), tier counts. Rate fields are intended for comparison
    against the in-game production GUI which reports per-second rates
    over a rolling window.
    """
    pseudo_by_name: dict[str, PseudoRecipe] = {}
    for step in inst.steps:
        if step.research:
            pseudo_by_name[step.research.name] = step.research
    for L in inst.launches:
        pseudo_by_name[L.name] = L
    for B in inst.burns:
        pseudo_by_name[B.name] = B

    recipe_time = {r.name: r.time_seconds for r in model.recipes.values()}
    for p in pseudo_by_name.values():
        recipe_time[p.name] = p.time_seconds

    net_coefs: dict[str, list[tuple[str, float]]] = {}
    for r in model.recipes.values():
        net_coefs[r.name] = _recipe_net_coefs(r)
    for p in pseudo_by_name.values():
        net_coefs[p.name] = _pseudo_net_coefs(p)
    _scale_oil_yield(net_coefs, model, inst.oil_yield_multiplier)
    _scale_item_units(net_coefs, "storage-tank", STORAGE_TANK_SCALE)
    for _chest in STORAGE_CHESTS:
        _scale_item_units(net_coefs, _chest, CHEST_SCALE)

    tracked = sorted(inst.all_items(model) - inst.excluded_items)
    excluded = sorted(inst.excluded_items)

    # Drop rows whose only "activity" is numerical dust below this
    # threshold (in absolute cycles or unit counts).
    tol = 1e-6

    # Player-time breakdown precompute (mirrors the per-step player_time
    # constraint in build_lp). Walk/place per building, chest place-time,
    # per-recipe wood draw, and the steel-axe felling-rate boundary. Only
    # when the constraint is active; otherwise no breakdown is emitted.
    pt_walk_place: dict[str, tuple[float, float]] = {}
    wood_per_recipe: dict[str, float] = {}
    steel_axe_step: int | None = None
    if inst.player_time_enabled:
        for b_name in model.buildings:
            pt_walk_place[b_name] = _placement_player_time_parts(inst, b_name, model)
        chest_walk = (
            2.0 * math.sqrt(inst.cfg.chest_tile_footprint) / inst.cfg.walking_speed_tps
        ) / CHEST_SCALE
        chest_place = (
            (1.0 + inst.cfg.chest_inserter_per)
            * inst.cfg.placement_tick_s
            / CHEST_SCALE
        )
        for chest in STORAGE_CHESTS:
            pt_walk_place[chest] = (chest_walk, chest_place)
        for r_name in (
            {k[0] for k in sol.x_real}
            | {k[0] for k in sol.x_pseudo}
            | {k[0] for k in sol.x_hand}
        ):
            wood_per_recipe[r_name] = _ingredient_amount(
                r_name, "wood", model, pseudo_by_name
            )
        steel_axe_step = next(
            (
                i
                for i, s in enumerate(inst.steps)
                if s.research and s.research.name == STEEL_AXE_RESEARCH
            ),
            None,
        )

    # Per-(building, step) utilization / criticality (handoff Theme 2).
    util_by_step = _capacity_utilization(inst, sol, model, recipe_time, pseudo_by_name)

    records: list[dict] = []
    for i, step in enumerate(inst.steps):
        d = float(sol.duration.get(i, 0.0))
        # Sub-microsecond durations behave as "no activity" for rate
        # purposes — the LP leaves tiny residuals at the empty FINAL
        # step and dividing by them manufactures meaningless gigawatts.
        rate_d = d if d > 1e-6 else 0.0

        # --- recipe activity ---
        activity: list[dict] = []
        for (r_name, b_name, ii), v in sol.x_real.items():
            if ii != i or v < tol:
                continue
            activity.append(
                {
                    "recipe": r_name,
                    "building": b_name,
                    "cycles": float(v),
                    "recipe_sec_used": float(v * recipe_time.get(r_name, 0.0)),
                }
            )
        for (p_name, ii), v in sol.x_pseudo.items():
            if ii != i or v < tol:
                continue
            p = pseudo_by_name[p_name]
            activity.append(
                {
                    "recipe": p_name,
                    "building": "+".join(b for b, _ in p.capacity_per_building),
                    "cycles": float(v),
                    "recipe_sec_used": float(v * p.time_seconds),
                }
            )
        # Player hand-crafting (the fixed-count character facility).
        for (r_name, ii), v in sol.x_hand.items():
            if ii != i or v < tol:
                continue
            activity.append(
                {
                    "recipe": r_name,
                    "building": "character",
                    "cycles": float(v),
                    "recipe_sec_used": float(v * recipe_time.get(r_name, 0.0)),
                }
            )
        activity.sort(key=lambda a: -a["cycles"])

        # --- fuel burn (non-boiler burners) ---
        fuel_burn: list[dict] = []
        for (fuel, b_name, ii), v in sol.fuel_burn.items():
            if ii != i or v < tol:
                continue
            fuel_burn.append(
                {
                    "fuel": fuel,
                    "building": b_name,
                    "units": float(v),
                    "rate_per_s": (float(v) / rate_d) if rate_d > 0 else None,
                }
            )
        fuel_burn.sort(key=lambda f: -f["units"])

        # --- energy (MJ over step + MW averaged) ---
        # Internal LP unit is MJ; MW falls out as MJ / sec. The Factorio
        # in-game power UI also displays MW, so direct comparison.
        e_demand = float(sol.electric_demand.get(i, 0.0))
        e_supply = float(sol.electric_supply.get(i, 0.0))
        energy = {
            "electric_demand_mj": e_demand,
            "electric_supply_mj": e_supply,
            "electric_demand_mw": (e_demand / rate_d) if rate_d > 0 else None,
            "electric_supply_mw": (e_supply / rate_d) if rate_d > 0 else None,
        }

        # --- player-time breakdown (seconds), mirroring the constraint ---
        player_time = None
        if inst.player_time_enabled:
            movement = 0.0
            placement = 0.0
            for b_name, (w, p) in pt_walk_place.items():
                if w <= 0 and p <= 0:
                    continue
                delta = float(sol.item.get((b_name, i + 1), 0.0)) - float(
                    sol.item.get((b_name, i), 0.0)
                )
                if delta == 0.0:
                    continue
                movement += w * delta
                placement += p * delta
            rate = (
                inst.cfg.tree_mining_rate_steelaxe
                if steel_axe_step is not None and i > steel_axe_step
                else inst.cfg.tree_mining_rate_base
            )
            wood_to_time = 1.0 / (inst.cfg.wood_per_tree * rate)
            wood_cutting = 0.0
            for (r_name, _b, ii), v in sol.x_real.items():
                if ii == i and wood_per_recipe.get(r_name, 0.0) > 0:
                    wood_cutting += wood_per_recipe[r_name] * wood_to_time * v
            for (p_name, ii), v in sol.x_pseudo.items():
                if ii == i and wood_per_recipe.get(p_name, 0.0) > 0:
                    wood_cutting += wood_per_recipe[p_name] * wood_to_time * v
            for (r_name, ii), v in sol.x_hand.items():
                if ii == i and wood_per_recipe.get(r_name, 0.0) > 0:
                    wood_cutting += wood_per_recipe[r_name] * wood_to_time * v
            total_pt = movement + placement + wood_cutting
            player_time = {
                "movement_s": movement,
                "placement_s": placement,
                "wood_cutting_s": wood_cutting,
                "total_s": total_pt,
                # Slack the constraint leaves: how much of the step the
                # single character spends doing nothing. Floats can leave a
                # sub-tol negative residual at the binding edge; clamp it.
                "idle_s": d - total_pt if (d - total_pt) > tol else 0.0,
            }

        # --- per-item flows ---
        # Production = Σ (+coef × cycles) across all recipes in step.
        # Consumption = Σ (−coef × cycles) + fuel_burn deductions.
        produced: dict[str, float] = {}
        consumed: dict[str, float] = {}
        cycles_by_recipe: dict[str, float] = {}
        for a in activity:
            cycles_by_recipe[a["recipe"]] = (
                cycles_by_recipe.get(a["recipe"], 0.0) + a["cycles"]
            )
        for r_name, total_cycles in cycles_by_recipe.items():
            for item_name, c in net_coefs.get(r_name, ()):
                if c > 0:
                    produced[item_name] = (
                        produced.get(item_name, 0.0) + c * total_cycles
                    )
                else:
                    consumed[item_name] = (
                        consumed.get(item_name, 0.0) + (-c) * total_cycles
                    )
        for fb in fuel_burn:
            consumed[fb["fuel"]] = consumed.get(fb["fuel"], 0.0) + fb["units"]

        items_record: list[dict] = []
        for n in tracked:
            start = float(sol.item.get((n, i), 0.0))
            end = float(sol.item.get((n, i + 1), 0.0))
            p_units = produced.get(n, 0.0)
            c_units = consumed.get(n, 0.0)
            if (
                p_units < tol
                and c_units < tol
                and abs(end - start) < tol
                and start < tol
            ):
                continue
            # Undo the unit rescales (storage-tank → 0.01-tank, chests →
            # 0.01-chest LP units) so the output reports whole physical entities.
            if n == "storage-tank":
                disp = 1.0 / STORAGE_TANK_SCALE
            elif n in STORAGE_CHESTS:
                disp = 1.0 / CHEST_SCALE
            else:
                disp = 1.0
            cons_rate = (c_units * disp / rate_d) if rate_d > 0 else None
            items_record.append(
                {
                    "name": n,
                    "count_start": start * disp,
                    "count_end": end * disp,
                    "produced": p_units * disp,
                    "consumed": c_units * disp,
                    "production_rate_per_s": (p_units * disp / rate_d)
                    if rate_d > 0
                    else None,
                    "consumption_rate_per_s": cons_rate,
                    # Buffer depth as latency tolerance (handoff Theme 2): seconds
                    # of consumption the end-of-step inventory covers. High → the
                    # flow tolerates a distant producer (loose placement); ~0 →
                    # just-in-time, must co-locate. Null when nothing consumes it.
                    "buffer_seconds": max(0.0, end * disp / cons_rate)
                    if (cons_rate and cons_rate > 0)
                    else None,
                }
            )

        # Per-ore electric-drill assignment: which drills sit on which patch.
        # First-class so L3 placement can put each ore's drills on its patch.
        ores = sorted({ore for (ore, _t) in sol.drill_assign})
        mining_assignment = []
        for ore in ores:
            start = float(sol.drill_assign.get((ore, i), 0.0))
            end = float(sol.drill_assign.get((ore, i + 1), 0.0))
            if start < tol and end < tol:
                continue
            mining_assignment.append(
                {
                    "building": f"{ELECTRIC_MINING_DRILL}@{ore}",
                    "ore": ore,
                    "count_start": start,
                    "count_end": end,
                }
            )

        # Per-output steel-furnace assignment: which furnaces smelt which
        # product (a furnace can't switch product mid-run). Same first-class
        # treatment as the drill split, emitted as `steel-furnace@<output>`.
        outputs = sorted({o for (o, _t) in sol.furnace_assign})
        smelting_assignment = []
        for out in outputs:
            start = float(sol.furnace_assign.get((out, i), 0.0))
            end = float(sol.furnace_assign.get((out, i + 1), 0.0))
            if start < tol and end < tol:
                continue
            smelting_assignment.append(
                {
                    "building": f"{STEEL_FURNACE}@{out}",
                    "output": out,
                    "count_start": start,
                    "count_end": end,
                }
            )

        # Excluded items: report only consumption (no flow constraint).
        excluded_items_record: list[dict] = []
        for n in excluded:
            c_units = consumed.get(n, 0.0)
            if c_units < tol:
                continue
            excluded_items_record.append(
                {
                    "name": n,
                    "consumed": c_units,
                    "consumption_rate_per_s": (c_units / rate_d)
                    if rate_d > 0
                    else None,
                }
            )

        record: dict = {
            "index": i,
            "label": step.label or step.research_tech or "FINAL",
            "duration_s": d,
            "energy": energy,
        }
        if step.research:
            record["research"] = {
                "tech": step.research_tech,
                "cycles": float(step.research.cycles_required or 0.0),
                "time_per_cycle_s": float(step.research.time_seconds),
            }
        if activity:
            record["activity"] = activity
        if fuel_burn:
            record["fuel_burn"] = fuel_burn
        if items_record:
            record["items"] = items_record
        if mining_assignment:
            record["mining_assignment"] = mining_assignment
        if smelting_assignment:
            record["smelting_assignment"] = smelting_assignment
        if player_time is not None:
            record["player_time"] = player_time
        if util_by_step.get(i):
            record["capacity"] = util_by_step[i]
        if excluded_items_record:
            record["excluded_items"] = excluded_items_record
        records.append(record)

    return records


def _solution_dict(
    inst: L2Instance,
    sol: Solution,
    model: GameModel,
) -> dict:
    """Top-level dict suitable for YAML serialization."""
    n_tiers = len(inst.steps) + 1
    final_items: list[dict] = []
    tracked = sorted(inst.all_items(model) - inst.excluded_items)
    for n in tracked:
        v = float(sol.item.get((n, n_tiers - 1), 0.0))
        floor = inst.final_floors.get(n)
        if abs(v) < 1e-6 and floor is None:
            continue
        entry: dict = {"name": n, "count": v}
        if floor is not None:
            entry["floor"] = float(floor)
        final_items.append(entry)

    from fplan.l2 import pseudo_recipes as _pr

    return {
        "scenario": inst.scenario.name,
        "source": inst.scenario.source,
        "l1_method": inst.l1_method,
        "mode": inst.mode,
        # In-game time of the initial state (see scenario.InitialState).
        # The solve is relative to this t₀; the visualizer shifts the
        # timeline by it. 0 when the scenario has no timestamp.
        "initial_time_s": float(inst.scenario.initial.timestamp_s),
        # Schema version of the shared pseudo-recipe stoichiometry that
        # produced this output. L3 cross-checks on load.
        "pseudo_recipes_version": _pr.VERSION,
        "solver": {
            "status": sol.status,
            "objective_s": (
                float(sol.objective) if sol.objective is not None else None
            ),
            "dual_bound": sol.dual_bound,
            "gap": sol.gap,
            "n_nodes": sol.n_nodes,
            "solve_time_s": sol.solve_time_s,
            "variables": sol.n_vars,
            "constraints": sol.n_constrs,
            "seed": sol.seed,
        },
        "steps": _per_step_records(inst, sol, model),
        "final_items": final_items,
        "excluded_consumption_total": {
            n: float(v) for n, v in sorted(sol.excluded_consumed.items())
        },
    }


def write_solution(
    inst: L2Instance,
    sol: Solution,
    model: GameModel,
    path: Path,
) -> None:
    """Serialize the solved L2 instance to YAML at `path`. Always
    writes solver metadata; per-step records only when status is
    optimal (infeasible runs leave step list empty for diagnostics).
    """
    # Persist whenever we have a feasible incumbent — that includes
    # early-termination statuses (timelimit, gaplimit, stallnodelimit).
    # Truly empty cases (infeasible / unstarted) write a stub.
    if sol.objective is not None:
        data = _solution_dict(inst, sol, model)
    else:
        from fplan.l2 import pseudo_recipes as _pr

        data = {
            "scenario": inst.scenario.name,
            "source": inst.scenario.source,
            "l1_method": inst.l1_method,
            "mode": inst.mode,
            "initial_time_s": float(inst.scenario.initial.timestamp_s),
            "pseudo_recipes_version": _pr.VERSION,
            "solver": {
                "status": sol.status,
                "objective_s": None,
                "dual_bound": sol.dual_bound,
                "gap": sol.gap,
                "n_nodes": sol.n_nodes,
                "solve_time_s": sol.solve_time_s,
                "variables": sol.n_vars,
                "constraints": sol.n_constrs,
                "seed": sol.seed,
            },
            "steps": [],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)


def _default_output_path(inst: L2Instance) -> Path:
    """Default to `outputs/02_<scenario>_<mode>.yaml`. Mode is in the
    name so the two complementary runs (lower/upper bound) coexist on
    disk rather than overwriting each other.
    """
    name = inst.scenario.name.strip() or "phases"
    return Path("outputs") / f"02_{name}_{inst.mode}.yaml"


def _print_solution(inst: L2Instance, sol: Solution, model: GameModel) -> None:
    print(f"Mode:        {inst.mode}")
    print(f"Status:      {sol.status}")
    if sol.seed is not None:
        print(f"Seed:        {sol.seed}   (re-pass with --seed {sol.seed})")
    print(f"Variables:   {sol.n_vars}")
    print(f"Constraints: {sol.n_constrs}")
    if sol.objective is not None:
        print(f"Objective:   {sol.objective:.6g}   (t_FINAL in seconds)")
    if sol.dual_bound is not None:
        print(
            f"Dual bound:  {sol.dual_bound:.6g}   "
            f"(LP-relaxation lower bound on t_FINAL)"
        )
    print(f"Gap:         {sol.gap * 100:.3f}%   (0 = proved optimal)")
    print(
        f"B&B nodes:   {sol.n_nodes}   (current run; total all runs: "
        f"{sol.n_total_nodes})"
    )
    print(f"B&B depth:   {sol.max_depth}")
    if sol.branching_factor is not None:
        print(
            f"Branch fac:  {sol.branching_factor:.4f}   "
            f"(effective: total_nodes^(1/depth); 2 = full binary tree)"
        )
    else:
        print("Branch fac:  n/a   (no branching — solved at root)")
    print(f"Solve time:  {sol.solve_time_s:.2f}s")
    if sol.status != "optimal":
        return

    print("\nStep durations:")
    for i in sorted(sol.duration):
        if i < len(inst.steps):
            label = inst.steps[i].research_tech or "(FINAL)"
        else:
            label = "?"
        print(f"   step {i:2d}  {label:30s} duration = {sol.duration[i]:.3f}s")

    print("\nNonzero recipe activity by step:")
    by_step: dict[int, list[tuple[str, str, float]]] = {}
    for (r, b, i), v in sol.x_real.items():
        by_step.setdefault(i, []).append((r, b, v))
    for (p, i), v in sol.x_pseudo.items():
        by_step.setdefault(i, []).append((p, "*pseudo", v))
    for (r, i), v in sol.x_hand.items():
        by_step.setdefault(i, []).append((r, "character", v))
    for i in sorted(by_step):
        if i < len(inst.steps):
            step_label = inst.steps[i].research_tech or "(FINAL)"
        else:
            step_label = "?"
        print(f"  step {i:2d} {step_label}:")
        for r, b, v in sorted(by_step[i], key=lambda t: -t[2]):
            print(f"     {v:12.4f}  {r}  ({b})")

    if sol.fuel_burn:
        print("\nFuel burn by burner-building × step:")
        by_step_fuel: dict[int, list[tuple[str, str, float]]] = {}
        for (fuel, b, i), v in sol.fuel_burn.items():
            by_step_fuel.setdefault(i, []).append((fuel, b, v))
        for i in sorted(by_step_fuel):
            step_label = (
                inst.steps[i].research_tech or "(FINAL)" if i < len(inst.steps) else "?"
            )
            print(f"  step {i:2d} {step_label}:")
            for fuel, b, v in sorted(by_step_fuel[i], key=lambda t: -t[2]):
                print(f"     {v:12.4f} {fuel:18s} → {b}")

    print("\nPer-step energy balance (MJ):")
    print(f"   {'step':>5} {'demand':>14} {'burn_supply':>14}")
    for i in sorted(sol.duration):
        print(
            f"   {i:>5d} "
            f"{sol.electric_demand.get(i, 0.0):14.4f} "
            f"{sol.electric_supply.get(i, 0.0):14.4f}"
        )

    n_tiers = len(inst.steps) + 1
    interesting = set(inst.final_floors) | set(inst.effective_initial_items)
    interesting &= inst.all_items(model) - inst.excluded_items
    print("\nFinal-tier counts (goal items + initial items):")
    for n in sorted(interesting):
        v = sol.item.get((n, n_tiers - 1), 0.0)
        floor = inst.final_floors.get(n)
        floor_s = f"   (floor: ≥ {floor:g})" if floor is not None else ""
        print(f"   {n:35s} {v:12.4f}{floor_s}")

    if inst.excluded_items:
        print("\nExcluded-item consumption (reported, not constrained):")
        for n in sorted(inst.excluded_items):
            print(f"   {n:35s} {sol.excluded_consumed.get(n, 0.0):12.4f}")
