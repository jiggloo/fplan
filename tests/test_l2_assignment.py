"""Tests for facility assignment — committing mining drills (per ore), furnaces
(per output), and assemblers (per recipe) to a single job so L3 gets static
blocks.

Hermetic against the fixture model. Reachability is arranged by *seeding* the
buildings into the scenario's initial items (the fixture carries the building
prototypes but not their build recipes), and the consumable-`destroy` paths use
a synthetically-augmented recipe set — both avoid depending on a real solve.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from fplan import scenario as scn
from fplan.l2 import config as l2config
from fplan.l2 import instance as l2_instance
from fplan.l2 import solve as l2_solve
from fplan.model import GameModel, build_game_data, load_model
from fplan.model.game import Recipe, Stack

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"

# Buildings seeded into initial items so they're reachable without build recipes.
_SEED = {
    "electric-mining-drill": 20.0,
    "burner-mining-drill": 20.0,
    "stone-furnace": 20.0,
    "steel-furnace": 20.0,
    "assembling-machine-1": 20.0,
    "assembling-machine-2": 20.0,
}
# Layers that make the smelting / crafting recipes available in some step.
_LAYERS = [
    ["automation"],
    ["electronics"],
    ["steel-processing"],
    ["logistic-science-pack"],
    ["advanced-material-processing"],
    ["automation-2"],
]


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


def _l1(tmp_path: Path, layers: list[list[str]] | None = None) -> Path:
    p = tmp_path / "order.yaml"
    p.write_text(yaml.safe_dump({"method": "forward", "layers": layers or _LAYERS}))
    return p


def _scenario(seed: dict | None = None) -> scn.Scenario:
    return scn.from_dict(
        {
            "name": "t",
            "items_produced": {"logistic-science-pack": 3},
            "initial_state": {"items": dict(seed if seed is not None else _SEED)},
        }
    )


def _inst(model, tmp_path, **kw):
    return l2_instance.build_instance(_scenario(), _l1(tmp_path), model, **kw)


def _con_names(m) -> set[str]:
    return {c.name for c in m.getConss()}


def _player_time_mentions(m, prefix: str) -> bool:
    """Whether any player_time constraint references a variable whose name starts
    with `prefix` (the model is unsolved, so vars carry their original names)."""
    for c in m.getConss():
        if not c.name.startswith("player_time_"):
            continue
        for v in m.getValsLinear(c):
            if str(v).startswith(prefix) or str(v).startswith("t_" + prefix):
                return True
    return False


def _consume_recipe(name: str, building: str) -> Recipe:
    """A recipe that consumes `building` as an ingredient (an upgrade/teardown),
    inert in the LP (never enabled) but visible to the data-driven consumer scan."""
    return Recipe(
        name=name,
        kind="crafting",
        category="crafting",
        time_seconds=1.0,
        ingredients=[Stack(name=building, amount=1.0)],
        outputs=[],
        enabled_at_start=False,
        unlocking_techs=[],
    )


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_assignment_defaults() -> None:
    c = l2config.load_config(None)
    assert c.mining_assignment_buildings == (
        "electric-mining-drill",
        "burner-mining-drill",
    )
    assert c.smelting_assignment_buildings == ("stone-furnace", "steel-furnace")
    assert c.crafting_assignment_enabled is True
    assert c.crafting_assignment_buildings == (
        "assembling-machine-1",
        "assembling-machine-2",
    )
    assert c.crafting_split_science_packs is True
    assert "inserter" in c.crafting_split_items
    assert c.crafting_unassign_cost_s == 1.0


def test_config_assignment_yaml_override(tmp_path: Path) -> None:
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "assignment:\n"
        "  mining: {buildings: [electric-mining-drill]}\n"
        "  smelting: {buildings: []}\n"
        "  crafting:\n"
        "    enabled: false\n"
        "    unassign_cost_s: 5\n"
        "    split_items: [inserter]\n"
    )
    c = l2config.load_config(f)
    assert c.mining_assignment_buildings == ("electric-mining-drill",)
    assert c.smelting_assignment_buildings == ()  # empty list disables the class
    assert c.crafting_assignment_enabled is False
    assert c.crafting_unassign_cost_s == 5.0
    assert c.crafting_split_items == frozenset({"inserter"})


def test_config_assignment_scalar_raises(tmp_path: Path) -> None:
    # A bare scalar where a mapping is expected → clean error, not a crash.
    f = tmp_path / "cfg.yaml"
    f.write_text("assignment: true\n")
    with pytest.raises(ValueError, match="invalid L2 config"):
        l2config.load_config(f)


def test_config_assignment_crafting_scalar_raises(tmp_path: Path) -> None:
    f = tmp_path / "cfg.yaml"
    f.write_text("assignment:\n  crafting: false\n")
    with pytest.raises(ValueError, match="invalid L2 config"):
        l2config.load_config(f)


# --------------------------------------------------------------------------- #
# resolve_assignment + spec
# --------------------------------------------------------------------------- #


def test_resolve_filters_to_reachable(model: GameModel) -> None:
    cfg = l2config.load_config(None)
    reachable = frozenset({"electric-mining-drill", "steel-furnace"})
    a = l2_instance.resolve_assignment(model, cfg, reachable, True)
    assert a.mining_buildings == ("electric-mining-drill",)  # burner not reachable
    assert a.smelting_buildings == ("steel-furnace",)  # stone not reachable
    assert a.crafting is None  # neither assembler reachable
    assert a.active is True


def test_resolve_crafting_needs_player_time(model: GameModel) -> None:
    cfg = l2config.load_config(None)
    reachable = frozenset(_SEED)
    a = l2_instance.resolve_assignment(model, cfg, reachable, player_time_enabled=False)
    # Mining/smelting are unconditional capacity structure; crafting's switch
    # cost is meaningless without player-time, so it's withheld.
    assert a.mining_buildings and a.smelting_buildings
    assert a.crafting is None


def test_resolve_crafting_disabled(model: GameModel) -> None:
    cfg = replace(l2config.load_config(None), crafting_assignment_enabled=False)
    a = l2_instance.resolve_assignment(model, cfg, frozenset(_SEED), True)
    assert a.crafting is None and a.mining_buildings


def test_resolve_all_off_is_inactive(model: GameModel) -> None:
    cfg = replace(
        l2config.load_config(None),
        mining_assignment_buildings=(),
        smelting_assignment_buildings=(),
        crafting_assignment_enabled=False,
    )
    a = l2_instance.resolve_assignment(model, cfg, frozenset(_SEED), True)
    assert a.active is False and a.note is None
    assert a.split_capacity_buildings == frozenset()


def test_split_capacity_buildings_property(model: GameModel) -> None:
    a = l2_instance.resolve_assignment(
        model, l2config.load_config(None), frozenset(_SEED), True
    )
    assert a.split_capacity_buildings == frozenset(_SEED)


def test_crafting_spec_splits_output(model: GameModel) -> None:
    a = l2_instance.resolve_assignment(
        model, l2config.load_config(None), frozenset(_SEED), True
    )
    assert a.crafting is not None
    assert a.crafting.splits_output("automation-science-pack")  # pack pattern
    assert a.crafting.splits_output("inserter")  # in split_items
    assert not a.crafting.splits_output("iron-plate")  # pooled
    assert a.crafting.assign_cost_s == pytest.approx(1.0 / 60.0)  # one game tick


# --------------------------------------------------------------------------- #
# data-driven consumer / split helpers (solve)
# --------------------------------------------------------------------------- #


def test_building_consumers_data_driven(model: GameModel) -> None:
    # Bare fixture: building recipes are stubs, so nothing is consumed.
    assert l2_solve._building_consumers(model, "assembling-machine-1") == []
    # Inject a recipe consuming AM1 → detected from the recipe ingredients.
    recs = dict(model.recipes)
    recs["am2-build"] = _consume_recipe("am2-build", "assembling-machine-1")
    aug = replace(model, recipes=recs)
    assert l2_solve._building_consumers(aug, "assembling-machine-1") == [
        ("am2-build", 1.0)
    ]


def test_recipe_is_split(model: GameModel) -> None:
    spec = l2_instance.resolve_assignment(
        model, l2config.load_config(None), frozenset(_SEED), True
    ).crafting
    assert spec is not None
    assert l2_solve._recipe_is_split(model, spec, "automation-science-pack")
    assert l2_solve._recipe_is_split(model, spec, "inserter")
    assert not l2_solve._recipe_is_split(model, spec, "iron-plate")


# --------------------------------------------------------------------------- #
# LP construction: the three classes are wired per building
# --------------------------------------------------------------------------- #


def test_mining_assignment_per_building(model: GameModel, tmp_path: Path) -> None:
    inst = _inst(model, tmp_path)
    m, handles = l2_solve.build_lp(inst, model)
    drills = {b for (b, _o, _t) in handles["drill_assign"]}
    assert {"electric-mining-drill", "burner-mining-drill"} <= drills
    names = _con_names(m)
    assert any(n.startswith("cap_drill_electric-mining-drill_") for n in names)
    assert any(n.startswith("drill_total_burner-mining-drill_") for n in names)
    assert any(n.startswith("drill_mono_electric-mining-drill_") for n in names)


def test_smelting_assignment_per_building(model: GameModel, tmp_path: Path) -> None:
    inst = _inst(model, tmp_path)
    m, handles = l2_solve.build_lp(inst, model)
    furnaces = {b for (b, _o, _t) in handles["furnace_assign"]}
    assert {"stone-furnace", "steel-furnace"} <= furnaces
    names = _con_names(m)
    assert any(n.startswith("cap_furnace_steel-furnace_") for n in names)
    assert any(n.startswith("furnace_total_stone-furnace_") for n in names)


def test_burner_furnace_coupling(model: GameModel, tmp_path: Path) -> None:
    inst = _inst(model, tmp_path)
    m, _ = l2_solve.build_lp(inst, model)
    # A burner-drill@ore needs a stone-furnace@plate on the smelted product.
    assert any(n.startswith("burner_furnace_couple_") for n in _con_names(m))


def test_crafting_assignment_buckets_and_player_time(
    model: GameModel, tmp_path: Path
) -> None:
    inst = _inst(model, tmp_path)
    m, handles = l2_solve.build_lp(inst, model)
    asm = {b for (b, _r, _t) in handles["assembler_assign"]}
    assert "assembling-machine-1" in asm
    names = _con_names(m)
    assert any(n.startswith("asm_link_assembling-machine-1_") for n in names)
    assert any(n.startswith("cap_asm_assembling-machine-1_") for n in names)
    assert any(n.startswith("asm_bal_assembling-machine-1_") for n in names)
    # The assemblers are removed from the generic pooled capacity.
    assert not any(n.startswith("cap_assembling-machine-1_") for n in names)
    # The transition vars exist and feed the serial player-time budget.
    var_names = {v.name for v in m.getVars()}
    assert any(n.startswith("asm_assign_assembling-machine-1_") for n in var_names)
    assert _player_time_mentions(m, "asm_assign_")


def test_assignment_disabled_keeps_pooled(model: GameModel, tmp_path: Path) -> None:
    cfg = replace(
        l2config.load_config(None),
        mining_assignment_buildings=(),
        smelting_assignment_buildings=(),
        crafting_assignment_enabled=False,
    )
    inst = _inst(model, tmp_path, l2_config=cfg)
    m, handles = l2_solve.build_lp(inst, model)
    assert not handles["drill_assign"]
    assert not handles["furnace_assign"]
    assert not handles["assembler_assign"]
    names = _con_names(m)
    # Assemblers now carry the generic pooled capacity instead.
    assert any(n.startswith("cap_assembling-machine-1_") for n in names)
    assert not any(n.startswith("asm_link") for n in names)


def test_crafting_gated_on_player_time(model: GameModel, tmp_path: Path) -> None:
    inst = _inst(model, tmp_path, player_time_enabled=False)
    _m, handles = l2_solve.build_lp(inst, model)
    # Crafting assignment withheld, but mining/smelting (no player-time cost) stay.
    assert not handles["assembler_assign"]
    assert handles["drill_assign"] and handles["furnace_assign"]


def test_consumable_furnace_gets_destroy(model: GameModel, tmp_path: Path) -> None:
    # Inject a recipe consuming stone-furnace → its bucket may shrink by the
    # consumption (the destroy drain); steel-furnace (no consumer) stays strict.
    recs = dict(model.recipes)
    recs["eat-stone-furnace"] = _consume_recipe("eat-stone-furnace", "stone-furnace")
    aug = replace(model, recipes=recs)
    inst = _inst(aug, tmp_path)
    m, _ = l2_solve.build_lp(inst, aug)
    names = _con_names(m)
    assert any(n.startswith("furnace_destroy_cap_stone-furnace_") for n in names)
    # steel-furnace is never consumed → no destroy cap, strict monotonicity only.
    assert not any(n.startswith("furnace_destroy_cap_steel-furnace_") for n in names)


def test_assembler_gets_destroy_when_consumed(model: GameModel, tmp_path: Path) -> None:
    recs = dict(model.recipes)
    recs["am2-build"] = _consume_recipe("am2-build", "assembling-machine-1")
    aug = replace(model, recipes=recs)
    inst = _inst(aug, tmp_path)
    m, _ = l2_solve.build_lp(inst, aug)
    names = _con_names(m)
    assert any(n.startswith("asm_destroy_cap_assembling-machine-1_") for n in names)


# --------------------------------------------------------------------------- #
# emission (hand-built Solution → per-step records)
# --------------------------------------------------------------------------- #


def _solution(model, **overrides):
    from fplan.l2.solve import Solution

    base = dict(
        status="optimal",
        objective=1.0,
        x_real={},
        x_pseudo={},
        x_hand={},
        item={},
        duration={0: 10.0},
        drill_assign={},
        furnace_assign={},
        excluded_consumed={},
        fuel_burn={},
        electric_demand={},
        electric_supply={},
        n_vars=0,
        n_constrs=0,
    )
    base.update(overrides)
    return Solution(**base)


def test_emits_per_building_mining_smelting_records(
    model: GameModel, tmp_path: Path
) -> None:
    inst = _inst(model, tmp_path)
    sol = _solution(
        model,
        drill_assign={("burner-mining-drill", "iron-ore", 0): 4.0},
        furnace_assign={("steel-furnace", "iron-plate", 0): 3.0},
    )
    recs = l2_solve._per_step_records(inst, sol, model)
    mining = recs[0].get("mining_assignment", [])
    smelt = recs[0].get("smelting_assignment", [])
    assert any(r["building"] == "burner-mining-drill@iron-ore" for r in mining)
    assert any(r["building"] == "steel-furnace@iron-plate" for r in smelt)


def test_emits_assembler_records_repurpose_penalized(
    model: GameModel, tmp_path: Path
) -> None:
    inst = _inst(model, tmp_path)
    sol = _solution(
        model,
        assembler_assign={("assembling-machine-1", "automation-science-pack", 0): 5.0},
    )
    recs = l2_solve._per_step_records(inst, sol, model)
    asm = recs[0].get("assembler_assignment", [])
    assert asm and asm[0]["repurpose_penalized"] is True
    assert asm[0]["building"] == "assembling-machine-1@automation-science-pack"


# --------------------------------------------------------------------------- #
# note
# --------------------------------------------------------------------------- #


def test_assignment_note_lists_active_classes(model: GameModel, tmp_path: Path) -> None:
    inst = _inst(model, tmp_path)
    note = inst.assignment.note
    assert note and "mining per-ore" in note and "crafting per-recipe" in note


# --------------------------------------------------------------------------- #
# assembler retirement (drop AM1 vars after a tech)
# --------------------------------------------------------------------------- #


def test_config_retire_after_default() -> None:
    c = l2config.load_config(None)
    assert c.crafting_retire_after == {"assembling-machine-1": "low-density-structure"}


def test_config_retire_after_yaml_override(tmp_path: Path) -> None:
    # Like the other dict configs (deployment, seeds), retire_after deep-merges
    # per building: a user can change a building's tech or add one. Neutralize a
    # building by pointing it at a tech it never researches.
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "assignment:\n"
        "  crafting:\n"
        "    retire_after:\n"
        "      assembling-machine-1: rocket-silo\n"
        "      assembling-machine-2: ''\n"
    )
    c = l2config.load_config(f)
    assert c.crafting_retire_after["assembling-machine-1"] == "rocket-silo"
    assert c.crafting_retire_after["assembling-machine-2"] == ""


def test_resolve_retire_after_filtered_to_reachable(model: GameModel) -> None:
    cfg = l2config.load_config(None)
    # AM1 reachable → kept; AM1 absent → dropped.
    a = l2_instance.resolve_assignment(
        model, cfg, frozenset({"assembling-machine-1"}), True
    )
    assert a.retire_after == {"assembling-machine-1": "low-density-structure"}
    a2 = l2_instance.resolve_assignment(model, cfg, frozenset({"lab"}), True)
    assert a2.retire_after == {}


def test_lp_retires_am1_after_tech(model: GameModel, tmp_path: Path) -> None:
    # Reach low-density-structure so retirement fires; seed AM1 so it's reachable.
    layers = _LAYERS + [["advanced-material-processing-2"], ["low-density-structure"]]
    inst = l2_instance.build_instance(_scenario(), _l1(tmp_path, layers), model)
    lds = next(
        s.index for s in inst.steps if s.research_tech == "low-density-structure"
    )
    _m, handles = l2_solve.build_lp(inst, model)
    am1_steps = {i for (_r, b, i) in handles["x_real"] if b == "assembling-machine-1"}
    # AM1 used before/at the LDS research step, gone once LDS is researched.
    assert any(i <= lds for i in am1_steps)
    assert not any(i > lds for i in am1_steps), "AM1 vars must be dropped post-LDS"
    # AM2 still runs after LDS (it takes over).
    am2_post = {
        i for (_r, b, i) in handles["x_real"] if b == "assembling-machine-2" and i > lds
    }
    assert am2_post


def test_resolve_retire_empty_tech_disables(model: GameModel) -> None:
    # An empty tech is the explicit "never retire" escape hatch — the entry is
    # dropped, so the building is never pruned.
    cfg = replace(
        l2config.load_config(None),
        crafting_retire_after={"assembling-machine-1": ""},
    )
    a = l2_instance.resolve_assignment(
        model, cfg, frozenset({"assembling-machine-1"}), True
    )
    assert a.retire_after == {}


@pytest.mark.parametrize("bad", ["-1.0", ".inf", ".nan"])
def test_config_unassign_cost_invalid_raises(tmp_path: Path, bad: str) -> None:
    # A negative/non-finite unassign cost would flip the repurpose incentive or
    # wreck the LP — reject it cleanly (invariant #1), never reach the solver.
    f = tmp_path / "cfg.yaml"
    f.write_text(f"assignment:\n  crafting:\n    unassign_cost_s: {bad}\n")
    with pytest.raises(ValueError, match="invalid L2 config"):
        l2config.load_config(f)


def test_lp_no_retirement_when_disabled(model: GameModel, tmp_path: Path) -> None:
    cfg = replace(l2config.load_config(None), crafting_retire_after={})
    layers = _LAYERS + [["advanced-material-processing-2"], ["low-density-structure"]]
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, layers), model, l2_config=cfg
    )
    lds = next(
        s.index for s in inst.steps if s.research_tech == "low-density-structure"
    )
    _m, handles = l2_solve.build_lp(inst, model)
    am1_post = {
        i for (_r, b, i) in handles["x_real"] if b == "assembling-machine-1" and i > lds
    }
    assert am1_post, "with retirement off, AM1 keeps running after LDS"
