"""Tests for the scenario-driven rocket-silo module hack.

The hack reads the modules/beacons a scenario declares in `items_produced`,
fills the silo's slots with productivity modules and the beacons with speed
modules (effects from the game data), and exposes effective speed / rocket-part
productivity / power factors the LP applies. All hermetic against the fixture
model (which carries the module + beacon prototypes); no SCIP, no Factorio.
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
from fplan.l2.solve import _scale_silo_productivity
from fplan.model import GameModel, build_game_data, load_model

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


def _scenario(**items: int) -> scn.Scenario:
    return scn.from_dict({"name": "t", "items_produced": items, "rocket_launches": 1})


# --------------------------------------------------------------------------- #
# compute_silo_modules
# --------------------------------------------------------------------------- #


def test_silo_modules_wr_prod3_loadout(model: GameModel) -> None:
    # 4× prod-3 in the silo + 40× speed-1 across 20 beacons (the WR TAS rig):
    #   speed = 4·(-0.15) + 40·0.20·0.5 = +3.40 → ×4.40
    #   productivity = 4·0.10 = +0.40 → ×1.40
    #   power = 250kW·(1+13.2) + 20·480kW = 13.15 MW
    s = _scenario(beacon=20, **{"speed-module": 40, "productivity-module-3": 4})
    r = l2_instance.compute_silo_modules(s, model, enabled=True)
    assert r.speed_mult == pytest.approx(4.40)
    assert r.productivity == pytest.approx(1.40)
    assert r.power_w == pytest.approx(13.15e6, rel=1e-3)
    assert r.note and "productivity-module-3" in r.note and "speed ×4.40" in r.note


def test_silo_modules_disabled_is_identity(model: GameModel) -> None:
    s = _scenario(beacon=20, **{"speed-module": 40, "productivity-module-3": 4})
    r = l2_instance.compute_silo_modules(s, model, enabled=False)
    assert (r.speed_mult, r.productivity, r.power_w, r.note) == (1.0, 1.0, None, None)


def test_silo_modules_none_declared_is_identity(model: GameModel) -> None:
    r = l2_instance.compute_silo_modules(
        _scenario(**{"iron-gear-wheel": 5}), model, True
    )
    assert r.speed_mult == 1.0 and r.productivity == 1.0 and r.note is None


def test_silo_modules_prod1_tier_reads_its_own_effect(model: GameModel) -> None:
    # The OLD hard-coded loadout (prod-1 ×4, 40 speed-1) → the old ×4.80 / ×1.16.
    s = _scenario(beacon=20, **{"speed-module": 40, "productivity-module": 4})
    r = l2_instance.compute_silo_modules(s, model, enabled=True)
    assert r.speed_mult == pytest.approx(4.80)
    assert r.productivity == pytest.approx(1.16)


def test_silo_modules_caps_at_slots_and_beacons(model: GameModel) -> None:
    # Over-declare: 8 prod modules but only 4 silo slots; 60 speed modules but
    # only 20·2 = 40 beacon slots. Effects cap at the physical capacity.
    s = _scenario(beacon=20, **{"speed-module": 60, "productivity-module-3": 8})
    r = l2_instance.compute_silo_modules(s, model, enabled=True)
    assert r.productivity == pytest.approx(1.40)  # 4 slots, not 8
    assert r.speed_mult == pytest.approx(4.40)  # 40 beacon slots, not 60


def test_silo_modules_no_beacons_only_silo(model: GameModel) -> None:
    # Prod modules but no beacons declared → only the silo's −0.60 speed penalty.
    s = _scenario(**{"productivity-module-3": 4})
    r = l2_instance.compute_silo_modules(s, model, enabled=True)
    assert r.speed_mult == pytest.approx(0.40)  # 1 + 4·(-0.15)
    assert r.productivity == pytest.approx(1.40)


# --------------------------------------------------------------------------- #
# build_instance threading + config toggle
# --------------------------------------------------------------------------- #


def _l1(tmp_path: Path) -> Path:
    p = tmp_path / "order.yaml"
    p.write_text(yaml.safe_dump({"method": "forward", "layers": [["automation"]]}))
    return p


def test_build_instance_threads_silo_factors(model: GameModel, tmp_path: Path) -> None:
    s = _scenario(beacon=20, **{"speed-module": 40, "productivity-module-3": 4})
    inst = l2_instance.build_instance(s, _l1(tmp_path), model)
    assert inst.silo_speed_mult == pytest.approx(4.40)
    assert inst.silo_productivity == pytest.approx(1.40)
    assert inst.silo_module_note is not None


def test_build_instance_config_toggle_off(model: GameModel, tmp_path: Path) -> None:
    s = _scenario(beacon=20, **{"speed-module": 40, "productivity-module-3": 4})
    cfg = replace(l2config.load_config(None), silo_modules_enabled=False)
    inst = l2_instance.build_instance(s, _l1(tmp_path), model, l2_config=cfg)
    assert inst.silo_speed_mult == 1.0
    assert inst.silo_productivity == 1.0
    assert inst.silo_module_note is None


# --------------------------------------------------------------------------- #
# _scale_silo_productivity (the LP output scaling)
# --------------------------------------------------------------------------- #


def test_scale_silo_productivity_scales_only_positive_output() -> None:
    net = {"rocket-part": [("rocket-part", 1.0), ("rocket-control-unit", -2.0)]}
    _scale_silo_productivity(net, 1.40)
    assert net["rocket-part"] == [
        ("rocket-part", pytest.approx(1.40)),
        ("rocket-control-unit", -2.0),
    ]


def test_scale_silo_productivity_identity_is_noop() -> None:
    net = {"rocket-part": [("rocket-part", 1.0)]}
    _scale_silo_productivity(net, 1.0)
    assert net["rocket-part"] == [("rocket-part", 1.0)]
