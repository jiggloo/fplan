"""Tests for the L2 facility-area feature's data layer: the solve's emitted
reference maps (`facilities` / `recipe_outputs`) and the area-split report
(`rates post`). The interactive view itself (compute_area_series /
build_area_dataset / render_area_html) is covered in test_l2_viz.py.

Hermetic against the fixture model. The footprint/speed map is built from a real
L2 instance (so the deployed footprint is exercised); the steps fed to the
emission helpers are synthetic, avoiding a real solve.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fplan import run as run_mod
from fplan import scenario as scn
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.l2 import flatten as l2_flatten
from fplan.l2 import instance as l2_instance
from fplan.l2 import solve as l2_solve
from fplan.model import GameModel, build_game_data, load_model

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"
runner = CliRunner()


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


@pytest.fixture
def use_fixture_model(monkeypatch, model):
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: model)


def _inst(model: GameModel, tmp_path: Path) -> l2_instance.L2Instance:
    l1 = tmp_path / "order.yaml"
    l1.write_text(yaml.safe_dump({"method": "forward", "layers": [["automation"]]}))
    sc = scn.from_dict(
        {
            "name": "t",
            "items_produced": {"iron-plate": 1},
            "initial_state": {"items": {"stone-furnace": 5.0}},
        }
    )
    return l2_instance.build_instance(sc, l1, model)


# --- facilities map (_facilities_dict) -------------------------------------


def test_facilities_dict_emits_deployed_footprint_and_speed(model, tmp_path) -> None:
    inst = _inst(model, tmp_path)
    steps = [
        {
            "activity": [
                {"recipe": "iron-plate", "building": "stone-furnace"},
                # pseudo / non-building rows must be skipped, not crash
                {"recipe": "research/automation", "building": "lab (productive)"},
                {"recipe": "x", "building": "character"},
            ],
            "mining_assignment": [
                {"building": "electric-mining-drill@iron-ore", "ore": "iron-ore"},
            ],
            "items": [
                {"name": "stone-furnace", "count_end": 3.0},
                {"name": "iron-plate", "count_end": 50.0},  # an item, not a building
            ],
        }
    ]
    fac = l2_solve._facilities_dict(inst, model, steps)

    # Buildings from activity + assignment base are present; the @target is dropped.
    assert "stone-furnace" in fac and "electric-mining-drill" in fac
    # Deployed footprint (infra-inclusive) exceeds the bare prototype footprint.
    bare = model.make_facility(model.buildings["stone-furnace"]).tile_footprint
    assert fac["stone-furnace"]["footprint"] >= bare > 0
    sf = model.buildings["stone-furnace"]
    assert fac["stone-furnace"]["base_speed"] == sf.base_speed
    # Items that aren't buildings, and pseudo / character rows, carry no entry.
    assert "iron-plate" not in fac
    assert "character" not in fac and "lab (productive)" not in fac


# --- recipe→output map (_recipe_outputs_dict) ------------------------------


def test_recipe_outputs_dict_principal_output_skips_pseudo(model) -> None:
    steps = [
        {
            "activity": [
                {"recipe": "iron-plate", "building": "stone-furnace"},
                {"recipe": "research/automation", "building": "lab"},  # no item output
            ],
            "assembler_assignment": [
                {"recipe": "iron-gear-wheel", "building": "assembling-machine-1@x"},
            ],
        }
    ]
    ro = l2_solve._recipe_outputs_dict(model, steps)
    assert ro["iron-plate"] == "iron-plate"
    assert ro["iron-gear-wheel"] == "iron-gear-wheel"
    # A pseudo recipe (not in model.recipes) is omitted rather than guessed.
    assert "research/automation" not in ro


# --- base-area split (compute_area_split / format_area_split) --------------

_FACILITIES = {
    "electric-mining-drill": {"footprint": 10.0, "base_speed": 0.5},
    "boiler": {"footprint": 12.0, "base_speed": 1.0},
}


def test_compute_area_split_penalized_flexible_static(model) -> None:
    steps = [
        {
            "label": "s0",
            "items": [
                {"name": "electric-mining-drill", "count_end": 5.0},
                {"name": "boiler", "count_end": 2.0},
                {"name": "iron-plate", "count_end": 99.0},  # not a building → ignored
            ],
            # 3 of the 5 drills are committed (penalized); 2 remain flexible.
            "mining_assignment": [
                {"building": "electric-mining-drill@iron-ore", "count_end": 3.0},
            ],
        }
    ]
    area = l2_flatten.compute_area_split(steps, _FACILITIES, model)
    a = area[0]
    # drill (mining-drill kind = flexible): penalized 3·10=30, remainder 2·10=20.
    assert a["penalized"] == 30.0
    assert a["flexible"] == 20.0
    # boiler (static kind), uncommitted: 2·12=24 static.
    assert a["static"] == 24.0
    assert a["total"] == 74.0
    per = a["per_building"]
    assert per["electric-mining-drill"]["remainder_class"] == "flexible"
    assert per["boiler"]["remainder_class"] == "static"
    assert "iron-plate" not in per


def test_compute_area_split_empty_without_facilities(model) -> None:
    # A pre-emission rates.yaml carries no facilities → every footprint is 0 →
    # no per-building area recorded.
    steps = [{"label": "s0", "items": [{"name": "boiler", "count_end": 2.0}]}]
    area = l2_flatten.compute_area_split(steps, {}, model)
    assert area[0]["total"] == 0.0 and area[0]["per_building"] == {}


def test_format_area_split_lines_and_empty(model) -> None:
    steps = [
        {
            "label": "s0",
            "items": [{"name": "electric-mining-drill", "count_end": 5.0}],
            "mining_assignment": [
                {"building": "electric-mining-drill@iron-ore", "count_end": 3.0}
            ],
        }
    ]
    area = l2_flatten.compute_area_split(steps, _FACILITIES, model)
    lines = l2_flatten.format_area_split(area)
    text = "\n".join(lines)
    assert "penalized" in text and "peak-area step" in text
    assert "repurposable committed" in text  # drill remainder is flexible
    # An empty split prints nothing.
    assert l2_flatten.format_area_split([]) == []


# --- rates post wiring ------------------------------------------------------

_POST_RATES_WITH_AREA = {
    "scenario": "t",
    "mode": "lower-bound",
    "l1_method": "forward",
    "initial_time_s": 0.0,
    "facilities": {"electric-mining-drill": {"footprint": 10.0, "base_speed": 0.5}},
    "recipe_outputs": {"iron-ore": "iron-ore"},
    "steps": [
        {
            "label": "s0",
            "duration_s": 10.0,
            "items": [
                {
                    "name": "iron-ore",
                    "produced": 100.0,
                    "production_rate_per_s": 10.0,
                    "consumption_rate_per_s": 0.0,
                    "consumed": 0.0,
                    "count_start": 0.0,
                    "count_end": 100.0,
                },
                {"name": "electric-mining-drill", "count_end": 4.0},
            ],
            "mining_assignment": [
                {
                    "building": "electric-mining-drill@iron-ore",
                    "ore": "iron-ore",
                    "count_start": 0.0,
                    "count_end": 3.0,
                }
            ],
        },
    ],
}


def test_post_echoes_area_split(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    run_mod.run_dir("r").mkdir(parents=True)
    rd = run_mod.run_dir("r")
    (rd / run_mod.MANIFEST_NAME).write_text("run: r\ninputs: {}\n")
    (rd / "rates.yaml").write_text(yaml.safe_dump(_POST_RATES_WITH_AREA))
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz", "--force"])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")
    # The base-area split table is echoed after the flatten summary.
    assert "repurpose-penalized vs flexible base area" in r.stdout
    assert "peak-area step" in r.stdout


def test_post_notes_missing_facilities(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    # A rates.yaml predating the facilities: block → a one-line note, no table.
    monkeypatch.chdir(tmp_path)
    run_mod.run_dir("r").mkdir(parents=True)
    rd = run_mod.run_dir("r")
    (rd / run_mod.MANIFEST_NAME).write_text("run: r\ninputs: {}\n")
    legacy = {k: v for k, v in _POST_RATES_WITH_AREA.items() if k != "facilities"}
    (rd / "rates.yaml").write_text(yaml.safe_dump(legacy))
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz", "--force"])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")
    assert "predates the facilities: block" in r.stdout
    assert "repurpose-penalized vs flexible base area" not in r.stdout
