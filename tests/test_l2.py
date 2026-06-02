"""Tests for the solver-neutral L2 layer: the tuning config, the scenario
contract, pseudo-recipes, the deployment overlay, and instance construction.

The SCIP solve itself (l2/solve.py) needs the full game model and a slow,
primal-coin-flip optimize, so it's a manual integration test (see the README),
not CI. Everything up to the solve runs against the model-layer raw fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fplan import scenario as scn
from fplan.l2 import config as l2config
from fplan.l2 import deployment, instance, pseudo_recipes
from fplan.model import GameModel, build_game_data, load_model

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_defaults_load() -> None:
    c = l2config.load_config()
    assert c.version == l2config.VERSION
    assert c.walking_speed_tps == 8.9 and c.burner_drill_cap == 50.0
    assert c.character_building == "assembling-machine-1"
    assert "pistol" in c.pruned_items and "wood" in c.fuel_excluded
    assert c.deployment_for("pumpjack").tile_footprint == 20.0
    # Unregistered building → empty pattern (no spatial cap).
    empty = c.deployment_for("nope")
    assert empty.tile_footprint == 0.0 and empty.infrastructure_items == {}


def test_config_deep_merge_override(tmp_path: Path) -> None:
    over = tmp_path / "tune.yaml"
    over.write_text(
        "caps: {burner_drill: 7.0}\ndeployment: {pumpjack: {tile_footprint: 9.0}}\n"
    )
    c = l2config.load_config(over)
    assert c.burner_drill_cap == 7.0  # overridden
    assert c.stone_furnace_cap == 200.0  # untouched default survives
    # Nested deep-merge: footprint overridden, infrastructure kept from default.
    p = c.deployment_for("pumpjack")
    assert p.tile_footprint == 9.0 and p.infrastructure_items["pipe"] == 10.0


def test_config_version_mismatch_warns(tmp_path: Path, capsys) -> None:
    over = tmp_path / "tune.yaml"
    over.write_text('version: "0.0.1"\ncaps: {burner_drill: 7.0}\n')
    l2config.load_config(over)
    assert "declares version" in capsys.readouterr().out


def test_config_invalid_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not a mapping\n")
    with pytest.raises(ValueError):
        l2config.load_config(bad)
    # A merged config missing a required section is a clean ValueError.
    partial = tmp_path / "p.yaml"
    partial.write_text("physics: null\n")
    # (deep-merge sets physics=None → _from_dict raises)
    with pytest.raises(ValueError):
        l2config.load_config(partial)


# --------------------------------------------------------------------------- #
# scenario
# --------------------------------------------------------------------------- #


def test_scenario_load_initial_and_checkpoints(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(
        "name: demo\n"
        "techs_researched: [steel-axe]\n"
        "items_produced: {beacon: 2}\n"
        "initial_state:\n"
        "  timestamp: '3:12'\n"
        "  items: {iron-plate: 5}\n"
        "  techs_researched: [automation]\n"
        "checkpoints:\n"
        "  - name: cp\n"
        "    trigger: {kind: before_recipe, recipe: rocket-part}\n"
        "    requires: {items: {rocket-silo: 1}}\n"
    )
    s = scn.load(p)
    assert s.name == "demo"
    assert s.initial.timestamp_s == 192.0
    assert dict(s.initial.items)["iron-plate"] == 5.0
    assert s.initial.techs_researched == ("automation",)
    assert s.goal.techs_researched == ("steel-axe",)
    assert len(s.checkpoints) == 1 and s.checkpoints[0].trigger.recipe == "rocket-part"


def test_scenario_timestamp_forms() -> None:
    assert scn._parse_timestamp(None, "t") == 0.0
    assert scn._parse_timestamp(90, "t") == 90.0
    assert scn._parse_timestamp("1:30", "t") == 90.0
    assert scn._parse_timestamp("45", "t") == 45.0
    for bad in (True, "1:2:3", [1]):
        with pytest.raises(ValueError):
            scn._parse_timestamp(bad, "t")


def test_scenario_validation_errors(tmp_path: Path) -> None:
    p = tmp_path / "s.yaml"
    p.write_text("- nope\n")
    with pytest.raises(ValueError):
        scn.load(p)
    with pytest.raises(ValueError):
        scn._initial_from_dict([1, 2], "initial_state")
    with pytest.raises(ValueError):
        scn._checkpoints_from_list([{"trigger": {"kind": "bogus"}}], "checkpoints")
    with pytest.raises(ValueError):  # before_recipe requires a recipe
        scn._checkpoints_from_list(
            [{"trigger": {"kind": "before_recipe"}}], "checkpoints"
        )


def test_scenario_as_dict_roundtrip() -> None:
    s = scn.from_dict(
        {"name": "x", "initial_state": {"items": {"a": 1}, "timestamp": 60}}
    )
    assert s.initial.as_dict()["timestamp_s"] == 60.0
    assert scn.InitialState().as_dict() == {}


# --------------------------------------------------------------------------- #
# pseudo_recipes
# --------------------------------------------------------------------------- #


def test_pseudo_recipes(model: GameModel) -> None:
    r = pseudo_recipes.for_research("automation", model, step_index=0)
    assert (
        r is not None
        and r.kind == "research"
        and r.capacity_per_building == (("lab", 1.0),)
    )
    launch = pseudo_recipes.for_launch("satellite", 1.0)
    assert launch.kind == "launch" and ("satellite", 1.0) in launch.ingredients
    bare = pseudo_recipes.for_launch("")
    assert bare.name == "launch/bare"
    burn = pseudo_recipes.for_burn("coal", model)
    assert burn is not None and burn.electric_output_j_per_cycle > 0
    assert pseudo_recipes.for_burn("wood", model) is None  # default FUEL_EXCLUDED
    # The exclusion set is config-threadable: excluding coal bars it too.
    assert pseudo_recipes.for_burn("coal", model, frozenset({"coal"})) is None
    # lookup resolves names back to recipes.
    assert pseudo_recipes.lookup("research/automation", model) is not None
    looked = pseudo_recipes.lookup("launch/bare", model)
    assert looked is not None and looked.kind == "launch"
    assert pseudo_recipes.lookup("not-a-pseudo", model) is None


# --------------------------------------------------------------------------- #
# deployment overlay (the stage-enrichment inversion)
# --------------------------------------------------------------------------- #


def test_deployed_facility_overlay_and_fallback(model: GameModel) -> None:
    cfg = l2config.load_config()
    am1 = model.buildings["assembling-machine-1"]
    f = deployment.deployed_facility(model, am1, cfg)
    assert f.tile_footprint == 15.0 and f.infrastructure_items["inserter"] == 2.0
    # Base factory stays deployment-free.
    assert model.make_facility(am1).infrastructure_items == {}
    # Unregistered building → bare prototype footprint, no infrastructure.
    acc = model.buildings["accumulator"]
    bare = deployment.deployed_facility(model, acc, cfg)
    assert bare.infrastructure_items == {}
    assert bare.tile_footprint == model.make_facility(acc).tile_footprint


# --------------------------------------------------------------------------- #
# instance construction
# --------------------------------------------------------------------------- #


def _l1(tmp_path: Path, layers: list[list[str]]) -> Path:
    p = tmp_path / "order.yaml"
    p.write_text(yaml.safe_dump({"method": "forward", "layers": layers}))
    return p


def _scenario(tmp_path: Path, **kw) -> scn.Scenario:
    return scn.from_dict({"name": "t", **kw})


def test_build_instance_basic(model: GameModel, tmp_path: Path) -> None:
    l1 = _l1(tmp_path, [["automation"], ["steel-processing"]])
    s = _scenario(tmp_path, initial_state={"items": {"iron-plate": 10}})
    inst = instance.build_instance(s, l1, model)
    # 2 tech steps + FINAL; character stand-in folded in; coal burn present.
    assert len(inst.steps) == 3
    assert inst.effective_initial_items["assembling-machine-1"] >= 2.0
    assert inst.cfg.version == l2config.VERSION
    assert "automation" in {st.research_tech for st in inst.steps}
    assert inst.all_items(model)  # non-empty


def test_build_instance_split_research(model: GameModel, tmp_path: Path) -> None:
    # `engine` is in split_research_techs → splits into engine-middle + engine.
    l1 = _l1(tmp_path, [["engine"]])
    inst = instance.build_instance(_scenario(tmp_path), l1, model)
    labels = [st.research_tech for st in inst.steps]
    assert "engine-middle" in labels and "engine" in labels


def test_build_instance_mode_weights(model: GameModel, tmp_path: Path) -> None:
    l1 = _l1(tmp_path, [["automation"]])
    s = _scenario(tmp_path)
    exp = instance.build_instance(s, l1, model, mode="experimental")
    assert exp.capacity_end_weight("assembling-machine-1") == 1.0
    assert exp.capacity_end_weight("electric-mining-drill") == 0.0  # raw extractor
    trap = instance.build_instance(s, l1, model, mode="trapezoidal")
    assert trap.capacity_end_weight("assembling-machine-1") == 0.5
    lb = instance.build_instance(s, l1, model, mode="lower-bound")
    assert lb.capacity_end_weight("anything") == 0.0
    assert "boiler" in lb.effective_initial_items  # lower-bound seed
    with pytest.raises(ValueError):
        instance.build_instance(s, l1, model, mode="bogus")


def test_build_instance_deployed_facility_method(
    model: GameModel, tmp_path: Path
) -> None:
    inst = instance.build_instance(
        _scenario(tmp_path), _l1(tmp_path, [["automation"]]), model
    )
    f = inst.deployed_facility(model, model.buildings["assembling-machine-1"])
    assert f.tile_footprint == 15.0


def test_build_instance_checkpoint_carves_step(
    model: GameModel, tmp_path: Path
) -> None:
    s = scn.from_dict(
        {
            "name": "t",
            "checkpoints": [
                {
                    "name": "cp",
                    "trigger": {"kind": "before_recipe", "recipe": "iron-gear-wheel"},
                    "requires": {"items": {"iron-gear-wheel": 3}},
                }
            ],
        }
    )
    inst = instance.build_instance(s, _l1(tmp_path, [["automation"]]), model)
    assert "carve/iron-gear-wheel" in [st.label for st in inst.steps]
    assert any("iron-gear-wheel" in st.forbidden_real_recipes for st in inst.steps)
    assert len(inst.checkpoints) == 1
    assert inst.checkpoints[0].items_floor["iron-gear-wheel"] == 3.0


def test_build_instance_checkpoint_dropped(model: GameModel, tmp_path: Path) -> None:
    # A checkpoint naming a recipe absent from the model is dropped with a warning.
    s = scn.from_dict(
        {
            "name": "t",
            "checkpoints": [
                {
                    "name": "cp",
                    "trigger": {"kind": "before_recipe", "recipe": "no-such-recipe"},
                    "requires": {},
                }
            ],
        }
    )
    inst = instance.build_instance(s, _l1(tmp_path, [["automation"]]), model)
    assert inst.checkpoints == ()
    assert any("checkpoint" in w for w in inst.warnings)


def test_build_instance_bad_l1(model: GameModel, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not_layers: 1\n")
    with pytest.raises(ValueError):
        instance.build_instance(_scenario(tmp_path), bad, model)


def test_load_map_data(tmp_path: Path) -> None:
    assert instance.load_map_data(None, 4.0).map_area == 0.0
    assert instance.load_map_data(tmp_path / "missing.yaml", 4.0).tile_pool == {}
    # Empty / non-mapping map file → empty MapData, not a traceback.
    (tmp_path / "empty.yaml").write_text("")
    assert instance.load_map_data(tmp_path / "empty.yaml", 4.0).map_area == 0.0
    p = tmp_path / "map.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "patches": [{"resource": "iron-ore", "tile_count": 100}],
                "map_gen_settings": {"width": 200, "height": 100},
                "oil_spots": [{}, {}],
                "water_patches": [{"tile_count": 64}],
                "tree_count": 50,
                "oil_clusters": [{"spot_count": 2, "total_yield_pct": 400}],
            }
        )
    )
    md = instance.load_map_data(p, 4.0)
    assert md.tile_pool["iron-ore"] == 100.0 and md.map_area == 20000.0
    assert md.oil_spot_count == 2 and md.wood_budget == 200.0
    assert md.water_pump_cap == 4.0 * 8.0 and md.oil_yield_multiplier == 2.0
    assert instance.load_tile_pool(p)["iron-ore"] == 100.0
