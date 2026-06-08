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
from typer.testing import CliRunner

from fplan import run as run_mod
from fplan import scenario as scn
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.l2 import config as l2config
from fplan.l2 import instance as l2_instance
from fplan.l2.solve import _scale_silo_productivity
from fplan.model import GameModel, build_game_data, load_model

runner = CliRunner()

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


# --------------------------------------------------------------------------- #
# Untrusted / degenerate input (invariant #1: never a raw traceback)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -5.0])
def test_silo_modules_non_finite_count_is_clean(model: GameModel, bad: float) -> None:
    # A degenerate hand-edited count (inf/NaN/negative) must skip-with-warning,
    # never crash int() with an OverflowError/ValueError.
    s = _scenario(beacon=bad, **{"speed-module": 40, "productivity-module-3": 4})
    warns: list[str] = []
    r = l2_instance.compute_silo_modules(s, model, enabled=True, warnings=warns)
    assert r.note is not None  # the silo prod modules still apply; beacons skipped
    assert any("non-finite" in w for w in warns)


def test_silo_modules_huge_count_overflow_is_clean(model: GameModel) -> None:
    # A huge-but-finite beacon count (1e308) passes the finite check but
    # overflows power_w to inf when multiplied; the final finiteness guard
    # returns identity-with-warning rather than feeding the LP a non-finite
    # coefficient.
    s = _scenario(beacon=1e308, **{"speed-module": 40, "productivity-module-3": 4})
    warns: list[str] = []
    r = l2_instance.compute_silo_modules(s, model, enabled=True, warnings=warns)
    assert r.power_w is None and r.note is None and r.speed_mult == 1.0
    assert any("non-finite factors" in w for w in warns)


def test_silo_modules_mixed_tier_prefers_strongest(model: GameModel) -> None:
    # 4 prod-1 + 4 prod-3 declared, only 4 silo slots → the stronger prod-3 fills
    # them (productivity 1.40, not the prod-1 1.16).
    s = _scenario(**{"productivity-module": 4, "productivity-module-3": 4})
    r = l2_instance.compute_silo_modules(s, model, enabled=True)
    assert r.productivity == pytest.approx(1.40)


# --------------------------------------------------------------------------- #
# Bundled default-victory loadout + config YAML round-trip
# --------------------------------------------------------------------------- #


def test_default_victory_ships_prod3_loadout() -> None:
    s = scn.load(Path("examples/scenarios/default-victory.yaml"))
    produced = dict(s.goal.items_produced)
    assert produced.get("productivity-module-3") == 4
    assert produced.get("speed-module") == 40 and produced.get("beacon") == 20
    # the before_recipe(rocket-part) checkpoint requires the same module set
    reqs = [dict(c.requires.items) for c in s.checkpoints]
    cp = next(r for r in reqs if r.get("rocket-silo"))
    assert cp.get("productivity-module-3") == 4
    assert "productivity-module" not in cp  # not the old prod-1


def test_config_silo_modules_yaml_toggle(tmp_path: Path) -> None:
    f = tmp_path / "cfg.yaml"
    f.write_text("silo_modules:\n  enabled: false\n")
    assert l2config.load_config(f).silo_modules_enabled is False
    # an absent block defaults to enabled (deep-merged over the packaged default)
    assert l2config.load_config(None).silo_modules_enabled is True


def test_config_silo_modules_scalar_raises(tmp_path: Path) -> None:
    # A malformed scalar where a mapping is expected → clean error, not a crash.
    f = tmp_path / "cfg.yaml"
    f.write_text("silo_modules: oops\n")
    with pytest.raises(ValueError, match="invalid L2 config"):
        l2config.load_config(f)


# --------------------------------------------------------------------------- #
# CLI: the ⚙ silo-modules note is printed when the hack fires
# --------------------------------------------------------------------------- #


def test_solve_prints_silo_module_note(tmp_path: Path, monkeypatch, model) -> None:
    import types

    monkeypatch.chdir(tmp_path)
    (tmp_path / "scn.yaml").write_text(
        "name: t\nitems_produced: {beacon: 20, speed-module: 40, "
        "productivity-module-3: 4}\nrocket_launches: 1\n"
    )
    (tmp_path / "order.yaml").write_text(
        yaml.safe_dump({"method": "forward", "layers": [["automation"]]})
    )
    (tmp_path / "map.yaml").write_text("patches: []\n")
    run_mod.save(
        run_mod.run_dir("r"),
        run_mod.Manifest.new(
            "r",
            scenario="scn.yaml",
            tech_order="order.yaml",
            map_path="map.yaml",
            created="t0",
        ),
    )
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: model)
    from fplan.l2 import solve as l2_solve

    def _fake_solve(inst, model, **kw):
        return (
            types.SimpleNamespace(
                objective=245.0, status="optimal", solve_time_s=1.0, seed=None
            ),
            None,
            None,
        )

    monkeypatch.setattr(l2_solve, "solve", _fake_solve)
    monkeypatch.setattr(l2_solve, "write_solution", lambda *a, **k: None)
    r = runner.invoke(app, ["rates", "solve", "r", "--seed", "1"])
    assert r.exit_code == 0, r.output
    assert "rocket-silo modules:" in r.output and "speed ×4.40" in r.output
