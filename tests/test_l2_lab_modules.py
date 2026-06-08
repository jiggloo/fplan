"""Tests for the lab productivity-module variant (the "productive lab" research
loadout). The variant fills a lab's module slots with a productivity module and
offers the LP a second, slower research pool that delivers bonus research per
cycle — so a tech finishes on fewer science packs. Effects, slot count, and the
power factor all come from the game data; how many labs run productive is the
solver's choice, with the modules reserved as infrastructure.

Hermetic against the fixture model (which carries the lab `module_slots`, the
prod-1 module effects, and producible `lab` + `productivity-module` recipes).
The pure helper (`compute_lab_modules`), the config, the instance threading, and
the LP *construction* are exercised here; the slow SCIP solve stays a manual
integration check (see docs/integration_tests.md).
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
from fplan.l2 import solve as l2_solve
from fplan.model import GameModel, build_game_data, load_model
from fplan.model.game import ModuleEffect

runner = CliRunner()

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


# A tech order whose last research step (`automation-2`) starts after the
# `productivity-module` recipe is unlocked, so the prod-lab variant is available
# there but not for the earlier (pre-module) research steps.
_POST_MODULE_LAYERS = [
    ["automation"],
    ["electronics"],
    ["advanced-electronics"],
    ["modules"],
    ["productivity-module"],
    ["automation-2"],
]


def _l1(tmp_path: Path, layers: list[list[str]]) -> Path:
    p = tmp_path / "order.yaml"
    p.write_text(yaml.safe_dump({"method": "forward", "layers": layers}))
    return p


def _scenario() -> scn.Scenario:
    return scn.from_dict(
        {"name": "t", "items_produced": {"automation-science-pack": 5}}
    )


# --------------------------------------------------------------------------- #
# compute_lab_modules
# --------------------------------------------------------------------------- #


def test_lab_modules_prod1_loadout(model: GameModel) -> None:
    # 2× prod-1 (the lab's 2 slots): productivity +2·0.04 = +0.08; speed
    # 1 + 2·(-0.05) = 0.90; power (1 + 2·0.40)/0.90 = 1.80/0.90 = 2.0.
    r = l2_instance.compute_lab_modules(model, True, "productivity-module")
    assert r.prod_bonus == pytest.approx(0.08)
    assert r.speed_frac == pytest.approx(0.90)
    assert r.power_factor == pytest.approx(2.0)
    assert r.module_item == "productivity-module" and r.modules_per == 2
    assert r.note and "productivity-module" in r.note and "×0.90" in r.note


def test_lab_modules_disabled_is_identity(model: GameModel) -> None:
    r = l2_instance.compute_lab_modules(model, False, "productivity-module")
    assert (r.prod_bonus, r.speed_frac, r.power_factor) == (0.0, 1.0, 1.0)
    assert r.module_item is None and r.modules_per == 0 and r.note is None


def test_lab_modules_non_productivity_module_is_skipped(model: GameModel) -> None:
    # A speed module has no productivity effect → no variant, with a warning.
    warns: list[str] = []
    r = l2_instance.compute_lab_modules(model, True, "speed-module", warns)
    assert r.module_item is None and r.note is None
    assert any("not a productivity module" in w for w in warns)


def test_lab_modules_unknown_module_is_skipped(model: GameModel) -> None:
    warns: list[str] = []
    r = l2_instance.compute_lab_modules(model, True, "no-such-module", warns)
    assert r.module_item is None
    assert any("not a productivity module" in w for w in warns)


def test_lab_modules_non_finite_effect_is_clean(model: GameModel) -> None:
    # A degenerate module effect (inf productivity) must skip-with-warning, never
    # feed the LP a non-finite coefficient (invariant #1).
    effects = dict(model.module_effects)
    effects["productivity-module"] = ModuleEffect(
        speed=-0.05, productivity=float("inf"), consumption=0.4
    )
    bad_model = replace(model, module_effects=effects)
    warns: list[str] = []
    r = l2_instance.compute_lab_modules(bad_model, True, "productivity-module", warns)
    assert r.module_item is None and r.note is None
    assert any("non-finite" in w for w in warns)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_lab_modules_defaults() -> None:
    c = l2config.load_config(None)
    assert c.lab_modules_enabled is True
    assert c.lab_modules_item == "productivity-module"


def test_config_lab_modules_yaml_toggle(tmp_path: Path) -> None:
    f = tmp_path / "cfg.yaml"
    f.write_text("lab_modules:\n  enabled: false\n  module: productivity-module-3\n")
    c = l2config.load_config(f)
    assert c.lab_modules_enabled is False
    assert c.lab_modules_item == "productivity-module-3"


def test_config_lab_modules_scalar_raises(tmp_path: Path) -> None:
    # A bare scalar where a mapping is expected → clean error, not a crash, and
    # (unlike a naive `or {}`) `lab_modules: false` does not silently re-enable.
    f = tmp_path / "cfg.yaml"
    f.write_text("lab_modules: false\n")
    with pytest.raises(ValueError, match="invalid L2 config"):
        l2config.load_config(f)


# --------------------------------------------------------------------------- #
# build_instance threading
# --------------------------------------------------------------------------- #


def test_build_instance_threads_lab_factors(model: GameModel, tmp_path: Path) -> None:
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, [["automation"]]), model
    )
    assert inst.lab_module_item == "productivity-module"
    assert inst.lab_prod_bonus == pytest.approx(0.08)
    assert inst.lab_speed_frac == pytest.approx(0.90)
    assert inst.lab_power_factor == pytest.approx(2.0)
    assert inst.lab_modules_per == 2
    assert inst.lab_module_note is not None


def test_build_instance_config_toggle_off(model: GameModel, tmp_path: Path) -> None:
    cfg = replace(l2config.load_config(None), lab_modules_enabled=False)
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, [["automation"]]), model, l2_config=cfg
    )
    assert inst.lab_module_item is None
    assert inst.lab_prod_bonus == 0.0 and inst.lab_speed_frac == 1.0
    assert inst.lab_module_note is None


# --------------------------------------------------------------------------- #
# LP construction: the prod-pool split is wired only where the module is unlocked
# --------------------------------------------------------------------------- #


def test_build_lp_adds_prod_pool_after_module_unlock(
    model: GameModel, tmp_path: Path
) -> None:
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, _POST_MODULE_LAYERS), model
    )
    m, handles = l2_solve.build_lp(inst, model)
    # Only the automation-2 research step (after productivity-module unlocks) gets
    # the variant; the four earlier research steps stay bare-only.
    prod_steps = sorted(i for _name, i in handles["res_prod"])
    research_step = next(
        st.index for st in inst.steps if st.research_tech == "automation-2"
    )
    assert prod_steps == [research_step]
    assert sorted(handles["lab_prod"]) == [research_step]
    names = {c.name for c in m.getConss()}
    assert any(n.startswith(f"cap_lab_bare_{research_step}") for n in names)
    assert any(n.startswith(f"cap_lab_prod_{research_step}") for n in names)
    assert any(n.startswith(f"lab_module_infra_{research_step}") for n in names)


def test_build_lp_no_prod_pool_when_disabled(model: GameModel, tmp_path: Path) -> None:
    cfg = replace(l2config.load_config(None), lab_modules_enabled=False)
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, _POST_MODULE_LAYERS), model, l2_config=cfg
    )
    m, handles = l2_solve.build_lp(inst, model)
    assert handles["res_prod"] == {} and handles["lab_prod"] == {}
    names = {c.name for c in m.getConss()}
    assert not any("cap_lab_prod" in n or "lab_module_infra" in n for n in names)


def test_build_lp_no_prod_pool_before_module_unlock(
    model: GameModel, tmp_path: Path
) -> None:
    # A scenario that never unlocks productivity-module → no productive labs,
    # even with the variant enabled (the per-step unlock gate holds).
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, [["automation"], ["steel-processing"]]), model
    )
    _m, handles = l2_solve.build_lp(inst, model)
    assert handles["res_prod"] == {}


def test_prod_pool_draws_science_per_cycle(model: GameModel, tmp_path: Path) -> None:
    # A productive-lab cycle must consume science packs at the SAME per-cycle rate
    # as a bare cycle (prod modules add output, not cheaper inputs). If res_prod
    # were omitted from the flow balance the LP could deliver research for free.
    # Assert that in every science-pack flow balance at the step, the res_prod var
    # carries the same coefficient as the bare research var (x_pseudo) — i.e. both
    # draw the pack identically. (getValsLinear reports the normalized LHS
    # coefficient, positive for a consumed item, so we compare, not sign-check.)
    inst = l2_instance.build_instance(
        _scenario(), _l1(tmp_path, _POST_MODULE_LAYERS), model
    )
    m, handles = l2_solve.build_lp(inst, model)
    step = next(st.index for st in inst.steps if st.research_tech == "automation-2")
    res_name = handles["res_prod"][("research/automation-2", step)].name
    bare_name = handles["x_pseudo"][("research/automation-2", step)].name

    checked = 0
    for cons in m.getConss():
        if cons.getConshdlrName() != "linear" or not cons.name.startswith("flow_"):
            continue
        if cons.name.rsplit("_", 1)[-1] != str(step):
            continue
        coefs = {
            getattr(var, "name", str(var)): coef
            for var, coef in m.getValsLinear(cons).items()
        }
        if bare_name not in coefs:  # not a pack this research consumes
            continue
        # Same nonzero draw as the bare cycle — never free.
        assert coefs.get(res_name) == pytest.approx(coefs[bare_name])
        assert coefs[bare_name] != 0.0
        checked += 1
    # automation-2 consumes 2 science packs → both balances checked.
    assert checked >= 1, "no science-pack flow balance referenced the research"


# --------------------------------------------------------------------------- #
# CLI: the ⚙ lab-modules note is printed when the variant is configured
# --------------------------------------------------------------------------- #


def test_solve_prints_lab_module_note(tmp_path: Path, monkeypatch, model) -> None:
    import types

    monkeypatch.chdir(tmp_path)
    (tmp_path / "scn.yaml").write_text(
        "name: t\nitems_produced: {automation-science-pack: 5}\nrocket_launches: 1\n"
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
    from fplan.l2 import solve as l2_solve_mod

    def _fake_solve(inst, model, **kw):
        return (
            types.SimpleNamespace(
                objective=12.0, status="optimal", solve_time_s=1.0, seed=None
            ),
            None,
            None,
        )

    monkeypatch.setattr(l2_solve_mod, "solve", _fake_solve)
    monkeypatch.setattr(l2_solve_mod, "write_solution", lambda *a, **k: None)
    r = runner.invoke(app, ["rates", "solve", "r", "--seed", "1"])
    assert r.exit_code == 0, r.output
    assert "lab modules:" in r.output and "×0.90" in r.output
