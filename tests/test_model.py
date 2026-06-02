"""Tests for the model layer (`fplan.model`).

The cleaning pipeline is exercised against a small *captured* raw-prototype
fixture (`fixtures/model_raw_subset.json`) run through the real cleaning, plus
targeted unit tests for the individual helpers. The live Lua load
(`fplan.model.data._load_raw` / `load`) needs a Factorio install and is
integration-only — see `python -m fplan.model` and the README Testing section.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplan.model import build_game_data, load_model
from fplan.model import data as md
from fplan.model import game as mg
from fplan.model.game import GameModel, Recipe, Stack

FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    raw = json.loads(FIXTURE.read_text())
    return load_model(raw=build_game_data(raw))


# --------------------------------------------------------------------------- #
# Fixture cleaning — the real pipeline on captured raw data
# --------------------------------------------------------------------------- #


def test_fixture_model_shape(model: GameModel) -> None:
    assert len(model.items) == 21
    assert len(model.recipes) == 20
    assert len(model.buildings) == 18
    assert len(model.technologies) == 38


def test_fixture_mining_and_pumping(model: GameModel) -> None:
    assert model.recipes["mine/iron-ore"].kind == "mining"
    assert "pump/offshore-pump" in model.recipes
    # Uranium mining carries its sulfuric-acid fluid ingredient.
    uranium = model.recipes["mine/uranium-ore"]
    assert any(
        s.kind == "fluid" and s.name == "sulfuric-acid" for s in uranium.ingredients
    )


def test_fixture_rocket_silo_module_hack(model: GameModel) -> None:
    assert round(model.buildings["rocket-silo"].base_speed, 2) == 4.8
    rp = model.recipes["rocket-part"]
    assert rp.outputs and rp.outputs[0].amount == pytest.approx(1.16, rel=1e-3)


def test_fixture_fuel_and_footprint(model: GameModel) -> None:
    assert model.items["coal"].fuel_value_j == 4_000_000.0
    assert model.buildings["stone-furnace"].fuel_categories == ("chemical",)
    assert model.buildings["stone-furnace"].base_tile_footprint > 0


def test_fixture_normal_expensive_merged(model: GameModel) -> None:
    # steel-plate had a normal/expensive split; the cleaned recipe uses normal.
    steel = model.recipes["steel-plate"]
    assert steel.ingredients and steel.time_seconds > 0


def test_fixture_multi_output_fluid(model: GameModel) -> None:
    aop = model.recipes["advanced-oil-processing"]
    assert len(aop.outputs) >= 2
    assert any(s.kind == "fluid" for s in aop.outputs)


def test_fixture_container_inventory(model: GameModel) -> None:
    assert model.items["iron-chest"].inventory_size is not None


# --------------------------------------------------------------------------- #
# GameModel cross-reference + materialization
# --------------------------------------------------------------------------- #


def test_cross_references(model: GameModel) -> None:
    assert any(r.name == "iron-plate" for r in model.recipes_producing("iron-plate"))
    assert model.recipes_consuming("iron-plate")  # gear wheel, steel, ...
    furnace_recipe = model.recipes["iron-plate"]
    assert any(b.kind == "furnace" for b in model.buildings_for(furnace_recipe))


def test_unlocking_and_availability(model: GameModel) -> None:
    # Start-enabled item → no unlocking techs.
    assert model.unlocking_techs_for("iron-plate") == set()
    # Tech-gated item → its unlocking tech.
    gated = model.unlocking_techs_for("logistic-science-pack")
    assert "logistic-science-pack" in gated
    assert not any(
        r.name == "logistic-science-pack" for r in model.available_recipes(set())
    )
    assert any(
        r.name == "logistic-science-pack"
        for r in model.available_recipes({"logistic-science-pack"})
    )


def test_make_facility_and_run(model: GameModel) -> None:
    furnace = model.buildings["stone-furnace"]
    fac = model.make_facility(furnace)
    assert fac.building == "stone-furnace"
    assert fac.tile_footprint == furnace.base_tile_footprint
    assert fac.infrastructure_items == {}
    assert hash(fac) == hash((fac.building, fac.modules))  # dict field excluded
    run = model.run(model.recipes["iron-plate"], fac)
    assert run.outputs_per_sec  # produces iron-plate per second
    # A recipe whose category the facility can't host raises.
    pump = model.recipes["pump/offshore-pump"]
    with pytest.raises(ValueError, match="cannot run recipe"):
        model.run(pump, fac)


def test_make_facility_modules_unsupported(model: GameModel) -> None:
    with pytest.raises(NotImplementedError):
        model.make_facility(
            model.buildings["stone-furnace"], modules=("speed-module-1",)
        )


# --------------------------------------------------------------------------- #
# total_recipe_seconds (synthetic models)
# --------------------------------------------------------------------------- #


def _model(*recipes: Recipe) -> GameModel:
    return GameModel(
        items={}, recipes={r.name: r for r in recipes}, buildings={}, technologies={}
    )


def _recipe(name, ings, outs, t=1.0) -> Recipe:
    return Recipe(
        name=name,
        kind="crafting",
        category="crafting",
        time_seconds=t,
        ingredients=[Stack(n, a) for n, a in ings],
        outputs=[Stack(n, a) for n, a in outs],
        enabled_at_start=True,
        unlocking_techs=[],
    )


def test_total_recipe_seconds_chain() -> None:
    m = _model(
        _recipe("a", [], [("A", 1)], t=1.0),
        _recipe("b", [("A", 2)], [("B", 1)], t=2.0),
    )
    assert m.total_recipe_seconds("A") == 1.0
    assert m.total_recipe_seconds("B") == 4.0  # 2 + 2*1
    assert m.total_recipe_seconds("missing") == 0.0


def test_total_recipe_seconds_cheapest_and_multioutput() -> None:
    m = _model(
        _recipe("cheap", [], [("X", 1)], t=1.0),
        _recipe("exp", [], [("X", 1)], t=5.0),
    )
    assert m.total_recipe_seconds("X") == 1.0
    m2 = _model(_recipe("mo", [], [("P", 1), ("Q", 1)], t=4.0))
    assert m2.total_recipe_seconds("P") == 2.0  # 4 / (1+1)


def test_total_recipe_seconds_cycle_skipped() -> None:
    # A self-referential recipe (Kovarex-style) is skipped; with no other
    # producer the item costs 0, and a non-cyclic alternative is chosen over it.
    only_cyclic = _model(_recipe("sl", [("A", 1)], [("A", 1)]))
    assert only_cyclic.total_recipe_seconds("A") == 0.0
    with_alt = _model(
        _recipe("sl", [("A", 1)], [("A", 1)]),
        _recipe("direct", [], [("A", 1)], t=2.0),
    )
    assert with_alt.total_recipe_seconds("A") == 2.0  # cyclic skipped, direct chosen


def test_total_recipe_seconds_zero_output_skipped() -> None:
    m = _model(_recipe("z", [], [("Z", 0.0)]))
    assert m.total_recipe_seconds("Z") == 0.0


def test_load_model_requires_input() -> None:
    with pytest.raises(ValueError, match="requires either"):
        load_model()


def test_main_reports_missing_prototypes_cleanly(tmp_path, monkeypatch, capsys) -> None:
    # A configured data_dir that exists but isn't a Factorio data dir reaches the
    # loader (require_data_dir only checks existence) → FileNotFoundError. The
    # entrypoint must surface it as a clean error, not a leaked traceback.
    from fplan import config as cfg
    from fplan.model import __main__ as model_main

    monkeypatch.chdir(tmp_path)
    (tmp_path / cfg.DEFAULT_CONFIG_NAME).write_text(
        cfg.render_config(str(tmp_path), None)  # data_dir exists, no base/prototypes
    )
    assert model_main.main() == 1
    assert "error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# game.py helpers
# --------------------------------------------------------------------------- #


def test_parse_energy() -> None:
    assert mg.parse_energy(None) is None
    assert mg.parse_energy(90000) == 90000.0
    assert mg.parse_energy("90kW") == 90000.0
    assert mg.parse_energy("4MJ") == 4e6
    assert mg.parse_energy("1GW") == 1e9
    assert mg.parse_energy("2TJ") == 2e12
    assert mg.parse_energy("5W") == 5.0
    assert mg.parse_energy("garbage") is None


def test_as_list() -> None:
    assert mg._as_list(None) == []
    assert mg._as_list([1, 2]) == [1, 2]
    assert mg._as_list({"b": 2, "a": 1}) == [1, 2]  # sorted keys
    assert set(mg._as_list({1: "x", "a": "y"})) == {"x", "y"}  # unsortable → values
    assert mg._as_list(5) == [5]


def test_merge_normal() -> None:
    assert mg._merge_normal({"x": 1}) == {"x": 1}
    merged = mg._merge_normal(
        {"x": 1, "normal": {"ingredients": ["a"]}, "expensive": {"ingredients": ["b"]}}
    )
    assert merged["ingredients"] == ["a"]
    assert "normal" not in merged and "expensive" not in merged


def test_expected_amount() -> None:
    assert mg._expected_amount({"amount": 5}) == 5.0
    assert mg._expected_amount({"amount_min": 2, "amount_max": 4}) == 3.0
    assert mg._expected_amount({}) == 1.0
    assert mg._expected_amount({"amount": 10, "probability": 0.7}) == pytest.approx(7.0)
    assert mg._expected_amount(["x", 3]) == 3.0
    assert mg._expected_amount(["x"]) == 1.0
    assert mg._expected_amount("scalar") == 1.0


def test_stack_helpers() -> None:
    assert mg._stack_from_ingredient(["iron", 2]).amount == 2.0
    fluid_in = mg._stack_from_ingredient(
        {"name": "water", "amount": 50, "type": "fluid"}
    )
    assert fluid_in.kind == "fluid" and fluid_in.amount == 50.0
    assert mg._stack_from_result(["x", 3]).amount == 3.0
    fluid_out = mg._stack_from_result({"name": "gas", "amount": 10, "type": "fluid"})
    assert fluid_out.kind == "fluid" and fluid_out.amount == 10.0


def test_recipe_outputs_none() -> None:
    assert mg._recipe_outputs_from_raw({}) == []


def test_fuel_categories() -> None:
    assert mg._fuel_categories({}) == ()
    assert mg._fuel_categories({"fuel_category": "chemical"}) == ("chemical",)
    assert mg._fuel_categories({"fuel_categories": ["chemical", "nuclear"]}) == (
        "chemical",
        "nuclear",
    )


def test_tile_footprint() -> None:
    assert mg._tile_footprint({}) == 0.0
    assert mg._tile_footprint({"selection_box": [[-1.5, -1.5], [1.5, 1.5]]}) == 9.0
    assert mg._tile_footprint({"collision_box": [[-0.4, -0.4], [0.4, 0.4]]}) == 1.0
    assert mg._tile_footprint({"selection_box": "bad"}) == 0.0  # malformed → 0


def test_rocket_silo_hack_noop_without_silo() -> None:
    # No silo / no rocket-part recipe: the post-process must be a no-op.
    m = load_model(raw=build_game_data({"item": {"x": {"name": "x", "type": "item"}}}))
    assert "rocket-silo" not in m.buildings


def test_mining_skips_resource_without_minable() -> None:
    m = load_model(
        raw=build_game_data(
            {"resource": {"barren": {"name": "barren", "type": "resource"}}}
        )
    )
    assert "mine/barren" not in m.recipes


def test_crafting_recipe_enabled_none_defaults_true() -> None:
    m = load_model(
        raw=build_game_data(
            {"recipe": {"r": {"name": "r", "enabled": None, "result": "x"}}}
        )
    )
    assert m.recipes["r"].enabled_at_start is True


def test_container_inventory_size_attached_and_skipped() -> None:
    raw = {
        "item": {
            "box": {"name": "box", "type": "item"},
            "box2": {"name": "box2", "type": "item"},
        },
        "container": {
            "box": {"name": "box", "inventory_size": 48},  # attached
            "box2": {"name": "box2"},  # in items but no inventory_size → skipped
            "ghost": {"name": "ghost", "inventory_size": 10},  # not an item → skipped
        },
    }
    m = load_model(raw=build_game_data(raw))
    assert m.items["box"].inventory_size == 48
    assert m.items["box2"].inventory_size is None
    assert "ghost" not in m.items


def test_build_drill_offshore_and_buildings_for(model: GameModel) -> None:
    # Building extraction covered the full kind matrix via the fixture; spot-check
    # a couple of the derived primitives.
    pump = model.buildings["offshore-pump"]
    assert pump.kind == "offshore-pump" and pump.pumped_fluid_per_sec > 0
    drill = model.buildings["electric-mining-drill"]
    assert drill.kind == "mining-drill" and drill.base_power_w > 0


# --------------------------------------------------------------------------- #
# data.py (parsing) — the pure pieces; the Lua load is integration-only
# --------------------------------------------------------------------------- #


def test_build_game_data_parses_techs() -> None:
    gd = build_game_data(json.loads(FIXTURE.read_text()))
    assert "rocket-silo" in gd.technologies
    assert gd.technologies["rocket-silo"].prerequisites  # deep tech has prereqs
    assert any(t.ingredients for t in gd.technologies.values())  # science-pack costs
    assert any(t.unlocks_recipes for t in gd.technologies.values())


def test_parse_tech_list_shapes() -> None:
    t = md._parse_tech(
        {
            "name": "t",
            "prerequisites": ["a", "b"],
            "effects": [
                {"type": "unlock-recipe", "recipe": "r"},
                {"type": "laboratory-speed", "modifier": 0.3},
            ],
            "unit": {
                "count": 10,
                "time": 30,
                "ingredients": [["automation-science-pack", 2]],
            },
            "essential": True,
        }
    )
    assert sorted(t.prerequisites) == ["a", "b"]
    assert t.unlocks_recipes == ["r"]
    assert t.lab_speed_bonus == pytest.approx(0.3)
    assert t.ingredients == [("automation-science-pack", 2)]
    assert t.count == 10 and t.time == 30 and t.essential


def test_parse_tech_dict_shapes() -> None:
    # Mapping-shaped prerequisites/effects/ingredients (the alternate Lua form).
    t = md._parse_tech(
        {
            "name": "t",
            "prerequisites": {"1": "a", "2": "b"},
            "effects": {"1": {"type": "unlock-recipe", "recipe": "r"}},
            "unit": {
                "ingredients": {"1": {"name": "automation-science-pack", "amount": 4}}
            },
        }
    )
    assert sorted(t.prerequisites) == ["a", "b"]
    assert t.unlocks_recipes == ["r"]
    assert t.ingredients == [("automation-science-pack", 4)]


@pytest.mark.parametrize(
    "trigger, expected",
    [
        (None, None),
        ({}, None),
        (
            {"type": "craft-item", "item": "iron-plate", "count": 50},
            "craft 50 iron-plate",
        ),
        ({"type": "craft-fluid", "fluid": "water", "count": 5}, "craft 5 water"),
        ({"type": "mine-entity", "entity": "big-rock"}, "mine big-rock"),
        ({"type": "build-entity", "entity": "lab"}, "build lab"),
        ({"type": "build-entity", "entity": {"name": "lab"}}, "build lab"),
        ({"type": "capture-spawner"}, "capture a spawner"),
        (
            {"type": "send-item-to-orbit", "item": "satellite"},
            "send satellite to orbit",
        ),
        ({"type": "create-space-platform"}, "create a space platform"),
        ({"type": "mystery", "foo": "bar"}, "mystery(foo=bar)"),
        ({"type": "mystery"}, "mystery"),
    ],
)
def test_format_research_trigger(trigger, expected) -> None:
    assert md.format_research_trigger(trigger) == expected
