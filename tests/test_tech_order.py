"""Tests for L1: goals (GoalState), the ordering/verify domain, and the
`tech-order build`/`verify` CLI plus the shared model-load / overwrite helpers.

Pure ordering/verify logic runs on synthetic tech graphs; goal-closure and the
CLI run against the model-layer raw fixture (no Factorio data in CI — the CLI
monkeypatches the model load to inject the fixture model).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from fplan import goals
from fplan import tech_order as ordering
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.model import GameModel, Technology, build_game_data, load_model

runner = CliRunner()

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


def _tech(
    name, prereqs=(), unlocks=(), ings=(), count=None, time=None, essential=False
):
    return Technology(
        name=name,
        prerequisites=list(prereqs),
        unlocks_recipes=list(unlocks),
        ingredients=[(n, c) for n, c in ings],
        count=count,
        time=time,
        essential=essential,
    )


def _diamond() -> dict[str, Technology]:
    # a → {b, c} → d
    return {
        "a": _tech("a"),
        "b": _tech("b", ["a"]),
        "c": _tech("c", ["a"]),
        "d": _tech("d", ["b", "c"]),
    }


def _gm(techs: dict[str, Technology]) -> GameModel:
    return GameModel(items={}, recipes={}, buildings={}, technologies=techs)


# --------------------------------------------------------------------------- #
# goals.py
# --------------------------------------------------------------------------- #


def test_goal_from_dict_and_as_dict_round_trip() -> None:
    g = goals.from_dict(
        {
            "name": "demo",
            "techs_researched": ["steel-axe"],
            "items_produced": {"beacon": 20},
            "rocket_launches": 1,
        }
    )
    assert g.techs_researched == ("steel-axe",)
    assert g.items_produced == (("beacon", 20.0),)
    assert g.rocket_launches == (("", 1.0),)
    d = g.as_dict()
    assert d["items_produced"] == {"beacon": 20.0}
    assert d["rocket_launches"] == 1.0  # bare-count form


def test_goal_counts_list_form_and_named_rocket() -> None:
    g = goals.from_dict(
        {"items_produced": [["a", 2], {"b": 3}], "rocket_launches": {"satellite": 2}}
    )
    assert g.items_produced == (("a", 2.0), ("b", 3.0))  # sorted by name
    assert g.as_dict()["rocket_launches"] == {"satellite": 2.0}


def test_goal_mixed_rocket_launches() -> None:
    g = goals.from_dict({"rocket_launches": [["", 1], ["satellite", 2]]})
    assert g.as_dict()["rocket_launches"] == {"_count": 1.0, "satellite": 2.0}


@pytest.mark.parametrize(
    "bad",
    [
        {"items_produced": True},  # bool rejected
        {"items_produced": 5},  # bare count not allowed here
        {"items_produced": [["only-one"]]},  # malformed entry
        {"items_produced": {5: 1}},  # non-string name
        {"items_produced": "a-string"},  # neither mapping nor list
        {"techs_researched": "not-a-list"},
    ],
)
def test_goal_validation_errors(bad) -> None:
    with pytest.raises(ValueError):
        goals.from_dict(bad)


def test_goal_load_and_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "g.yaml"
    p.write_text("name: x\ntechs_researched: [steel-axe]\n")
    assert goals.load(p).name == "x"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        goals.load(p)


def test_required_techs_uses_model(model: GameModel) -> None:
    # items_produced pulls in the item's unlocking tech; rocket_launches runs the
    # rocket-silo branch (empty here — the fixture has no rocket-silo recipe).
    g = goals.from_dict(
        {
            "techs_researched": ["plastics"],
            "items_produced": {"logistic-science-pack": 1},
            "rocket_launches": {"satellite": 1},  # named payload → item-unlock path
        }
    )
    req = goals.required_techs(g, model)
    assert "plastics" in req
    assert "logistic-science-pack" in req  # the item's unlocking tech


# --------------------------------------------------------------------------- #
# tech_order.py — closure + ordering methods
# --------------------------------------------------------------------------- #


def test_closure_and_unknown_leaf() -> None:
    techs = _diamond()
    assert ordering._closure(techs, {"d"}) == {"a", "b", "c", "d"}
    with pytest.raises(KeyError, match="unknown technology"):
        ordering._closure(techs, {"nope"})


def test_closure_keeps_unexpandable_prereq() -> None:
    # An unknown prereq is kept as a name but not expanded (real data has none).
    techs = {"x": _tech("x", ["ghost"])}  # ghost not in techs
    assert ordering._closure(techs, {"x"}) == {"x", "ghost"}


def test_forward_order() -> None:
    techs = _diamond()
    res = ordering.forward_order(techs, {"a", "b", "c", "d"}, goals.GoalState())
    assert res.layers == (("a",), ("b", "c"), ("d",))


def test_from_goal_order() -> None:
    techs = _diamond()
    res = ordering.from_goal_order(techs, {"a", "b", "c", "d"}, goals.GoalState())
    assert res.layers[0] == ("d",)  # goal asks first
    assert res.layers[-1] == ("a",)  # foundation last


def test_balanced_order() -> None:
    techs = _diamond()
    res = ordering.balanced_order(techs, {"a", "b", "c", "d"}, goals.GoalState())
    flat = [t for layer in res.layers for t in layer]
    assert flat.index("a") < flat.index("b") < flat.index("d")  # prereqs precede
    assert res.notes["critical_chain_zero_slack"]  # a and d are zero-slack


def test_required_set_via_model(model: GameModel) -> None:
    g = goals.from_dict({"techs_researched": ["plastics"]})
    req = ordering.required_set(model.technologies, g, model)
    assert "plastics" in req and "oil-processing" in req  # transitive prereq


# --------------------------------------------------------------------------- #
# tech_order.py — verify
# --------------------------------------------------------------------------- #


def test_verify_valid() -> None:
    techs = _diamond()
    res = ordering.verify_order(
        techs,
        _gm(techs),
        [["a"], ["b", "c"], ["d"]],
        goals.GoalState(techs_researched=("d",)),
    )
    assert res.ok and not res.errors


def test_verify_missing_and_order_violation() -> None:
    techs = _diamond()
    # d before its prereqs, and 'a' missing entirely.
    res = ordering.verify_order(
        techs, _gm(techs), [["d"], ["b", "c"]], goals.GoalState(techs_researched=("d",))
    )
    assert not res.ok
    assert any("missing required" in e for e in res.errors)
    assert any("not preceded by its prerequisite" in e for e in res.errors)


def test_verify_unknown_and_duplicate() -> None:
    techs = _diamond()
    res = ordering.verify_order(
        techs,
        _gm(techs),
        [["a", "a"], ["ghost"]],
        goals.GoalState(techs_researched=("a",)),
    )
    assert any("duplicate" in e for e in res.errors)
    assert any("unknown techs" in e for e in res.errors)


def test_verify_goal_references_unknown_tech() -> None:
    techs = _diamond()
    res = ordering.verify_order(
        techs, _gm(techs), [["a"]], goals.GoalState(techs_researched=("nonexistent",))
    )
    assert not res.ok
    assert any("goal references unknown tech" in e for e in res.errors)


def test_verify_extra_and_same_layer_warnings() -> None:
    techs = _diamond()
    techs["e"] = _tech("e")  # not required by goal d → extra (non-fatal)
    # a & b share layer 0 (b depends on a) → same-layer warning; linear order
    # a-before-b still holds, so the order is valid.
    res = ordering.verify_order(
        techs,
        _gm(techs),
        [["a", "b"], ["c"], ["d"], ["e"]],
        goals.GoalState(techs_researched=("d",)),
    )
    assert res.ok
    assert any("extra techs" in w for w in res.warnings)
    assert any("sharing a layer" in w for w in res.warnings)


# --------------------------------------------------------------------------- #
# tech_order.py — formatting + payload
# --------------------------------------------------------------------------- #


def test_format_tech_variants() -> None:
    assert "*" in ordering.format_tech(_tech("x", essential=True))
    assert "50 × (sci-pack" in ordering.format_tech(
        _tech("y", ings=[("sci-pack", 1)], count=50)
    )
    assert "—" in ordering.format_tech(_tech("z"))
    trig = Technology(
        name="t",
        research_trigger={"type": "craft-item", "item": "iron-plate", "count": 50},
    )
    assert "trigger: craft 50 iron-plate" in ordering.format_tech(trig)


def test_format_layers_and_payload() -> None:
    techs = _diamond()
    res = ordering.forward_order(techs, {"a", "b", "c", "d"}, goals.GoalState())
    goal = goals.GoalState(name="demo", techs_researched=("d",))
    text = ordering.format_layers(res, techs, goal, "forward")
    assert "Goal 'demo'" in text and "Layer 0" in text
    payload = ordering.build_payload(res, goal, "forward")
    assert payload["level"] == 1 and payload["method"] == "forward"
    assert payload["layers"] == [["a"], ["b", "c"], ["d"]]
    assert payload["goal"]["name"] == "demo"
    bal = ordering.balanced_order(techs, {"a", "b", "c", "d"}, goal)
    assert "notes" in ordering.build_payload(bal, goal, "balanced")  # notes included


# --------------------------------------------------------------------------- #
# shared CLI helpers
# --------------------------------------------------------------------------- #


def test_confirm_overwrite_or_exit(tmp_path: Path, monkeypatch) -> None:
    cli_main.confirm_overwrite_or_exit(tmp_path / "nope")  # absent → no-op, no raise
    target = tmp_path / "x.yaml"
    target.write_text("old")
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: False)
    with pytest.raises(typer.Exit) as exc:
        cli_main.confirm_overwrite_or_exit(target)
    assert exc.value.exit_code == 1


def test_load_model_or_exit_paths(tmp_path: Path, monkeypatch) -> None:
    import fplan.model as model_mod

    # No config → fatal.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        cli_main.load_model_or_exit(None)
    # data_dir exists but isn't Factorio → load raises FileNotFoundError → fatal.
    from fplan import config as cfg

    conf = tmp_path / "c.yaml"
    conf.write_text(cfg.render_config(str(tmp_path), None))
    with pytest.raises(typer.Exit):
        cli_main.load_model_or_exit(conf)
    # Happy path: stub the loader so no real data is needed.
    sentinel = object()
    monkeypatch.setattr(model_mod, "load_model", lambda *, data_dir: sentinel)
    assert cli_main.load_model_or_exit(conf) is sentinel


# --------------------------------------------------------------------------- #
# CLI: tech-order build / verify
# --------------------------------------------------------------------------- #


def _scenario(tmp_path: Path, techs: list[str]) -> Path:
    p = tmp_path / "scn.yaml"
    p.write_text("name: t\ntechs_researched: [" + ", ".join(techs) + "]\n")
    return p


@pytest.fixture
def use_fixture_model(monkeypatch, model: GameModel):
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: model)


def test_build_writes_order(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    result = runner.invoke(app, ["tech-order", "build", str(scn), "--out", "o.yaml"])
    assert result.exit_code == 0
    assert "Layer 0" in result.stdout
    import yaml

    doc = yaml.safe_load((tmp_path / "o.yaml").read_text())
    assert doc["level"] == 1 and "plastics" in [t for L in doc["layers"] for t in L]


def test_build_requires_out(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    assert runner.invoke(app, ["tech-order", "build", str(scn)]).exit_code == 2


def test_build_unknown_method(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    r = runner.invoke(
        app, ["tech-order", "build", str(scn), "--out", "o.yaml", "--method", "bogus"]
    )
    assert r.exit_code == 2


def test_build_scenario_not_found(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(
        app, ["tech-order", "build", str(tmp_path / "missing.yaml"), "--out", "o.yaml"]
    )
    assert r.exit_code == 1


def test_build_bad_scenario_yaml(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("- not\n- a mapping\n")
    r = runner.invoke(app, ["tech-order", "build", str(bad), "--out", "o.yaml"])
    assert r.exit_code == 1


def test_build_dry_run_writes_nothing(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    r = runner.invoke(
        app, ["tech-order", "build", str(scn), "--out", "o.yaml", "--dry-run"]
    )
    assert r.exit_code == 0 and "dry run" in r.stdout
    assert not (tmp_path / "o.yaml").exists()


def test_build_unknown_tech_is_fatal(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["no-such-tech"])
    r = runner.invoke(app, ["tech-order", "build", str(scn), "--out", "o.yaml"])
    assert r.exit_code == 1  # required_set raises KeyError → clean fatal


def test_verify_valid_and_invalid(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    runner.invoke(app, ["tech-order", "build", str(scn), "--out", "o.yaml"])
    ok = runner.invoke(app, ["tech-order", "verify", "o.yaml"])
    assert ok.exit_code == 0 and "VALID" in ok.stdout
    # Corrupt the order: drop a layer → missing required tech.
    import yaml

    doc = yaml.safe_load((tmp_path / "o.yaml").read_text())
    doc["layers"] = doc["layers"][1:]
    (tmp_path / "o.yaml").write_text(yaml.safe_dump(doc))
    bad = runner.invoke(app, ["tech-order", "verify", "o.yaml"])
    assert bad.exit_code == 1 and "INVALID" in bad.stdout


def test_verify_scenario_override(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    runner.invoke(app, ["tech-order", "build", str(scn), "--out", "o.yaml"])
    r = runner.invoke(app, ["tech-order", "verify", "o.yaml", "--scenario", str(scn)])
    assert r.exit_code == 0 and "--scenario" in r.stdout


def test_verify_no_layers(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty.yaml").write_text("level: 1\n")
    assert runner.invoke(app, ["tech-order", "verify", "empty.yaml"]).exit_code == 1


def test_verify_no_goal_no_scenario(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ng.yaml").write_text("layers:\n- [a]\n")
    r = runner.invoke(app, ["tech-order", "verify", "ng.yaml"])
    assert r.exit_code == 1 and "no embedded 'goal'" in (r.stdout + (r.stderr or ""))


def test_verify_reports_warnings(tmp_path, monkeypatch, use_fixture_model) -> None:
    # Verify a plastics order against a smaller goal → the plastics-only techs are
    # extras (non-fatal warning), still VALID.
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        [
            "tech-order",
            "build",
            str(_scenario(tmp_path, ["plastics"])),
            "--out",
            "o.yaml",
        ],
    )
    small = tmp_path / "small.yaml"
    small.write_text("techs_researched: [oil-processing]\n")
    r = runner.invoke(app, ["tech-order", "verify", "o.yaml", "--scenario", str(small)])
    assert r.exit_code == 0
    assert "WARNING" in r.stdout and "extra techs" in r.stdout


def test_verify_bad_yaml(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.yaml").write_text("layers: [unbalanced\n")
    assert runner.invoke(app, ["tech-order", "verify", "bad.yaml"]).exit_code == 1


def test_verify_scenario_not_found(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    runner.invoke(app, ["tech-order", "build", str(scn), "--out", "o.yaml"])
    r = runner.invoke(
        app,
        [
            "tech-order",
            "verify",
            "o.yaml",
            "--scenario",
            str(tmp_path / "missing.yaml"),
        ],
    )
    assert r.exit_code == 1


def test_build_write_failure_is_fatal(tmp_path, monkeypatch, use_fixture_model) -> None:
    # --out under a path whose parent is a file → mkdir/write raises OSError.
    monkeypatch.chdir(tmp_path)
    scn = _scenario(tmp_path, ["plastics"])
    afile = tmp_path / "afile"
    afile.write_text("x")
    r = runner.invoke(
        app, ["tech-order", "build", str(scn), "--out", str(afile / "sub.yaml")]
    )
    assert r.exit_code == 1
