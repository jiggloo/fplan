"""Tests for L2 rate-flattening (`fplan.l2.flatten`).

The flattening math is solver-neutral and model-light: ``tube``/``chord`` need
no model, and the unmet-input report / ``mrp`` explosion use the cleaned-model
raw fixture (the same one the L2 tests use). The heavy bits (a real solve) are
exercised in the manual integration steps, not here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from fplan.l2 import flatten as F
from fplan.model import GameModel, build_game_data, load_model

MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


# A two-step world: 100 widgets built in step 0, all consumed in step 1. The
# original makes them all up front (rate 10 then 0); flattening spreads them.
SYNTH = {
    "scenario": "t",
    "mode": "lower-bound",
    "initial_time_s": 0.0,
    "steps": [
        {
            "label": "s0",
            "duration_s": 10.0,
            "items": [
                {
                    "name": "widget",
                    "produced": 100.0,
                    "production_rate_per_s": 10.0,
                    "consumption_rate_per_s": 0.0,
                    "consumed": 0.0,
                    "count_start": 0.0,
                    "count_end": 100.0,
                }
            ],
        },
        {
            "label": "s1",
            "duration_s": 10.0,
            "items": [
                {
                    "name": "widget",
                    "produced": 0.0,
                    "production_rate_per_s": 0.0,
                    "consumption_rate_per_s": 10.0,
                    "consumed": 100.0,
                    "count_start": 100.0,
                    "count_end": 0.0,
                }
            ],
        },
    ],
}


# --- taut string -----------------------------------------------------------
def test_taut_string_endpoints_and_corridor() -> None:
    X = [0.0, 1.0, 2.0]
    BOT = [0.0, 0.0, 2.0]
    TOP = [0.0, 1.0, 2.0]
    path = F.taut_string(X, BOT, TOP, 0.0, 2.0)
    assert path[0] == (0.0, 0.0) and path[-1] == (2.0, 2.0)
    # Every gate value stays within [BOT, TOP].
    px = [p[0] for p in path]
    py = [p[1] for p in path]
    for k, x in enumerate(X):
        y = F._interp(px, py, x)
        assert BOT[k] - 1e-6 <= y <= TOP[k] + 1e-6


def test_taut_string_single_gate() -> None:
    assert F.taut_string([5.0], [0.0], [0.0], 3.0, 3.0) == [(5.0, 3.0)]


# --- segments --------------------------------------------------------------
def test_segments_counts_distinct_runs() -> None:
    assert F._segments([1.0, 1.0, 2.0, 2.0, 1.0]) == 3
    assert F._segments([5.0, 5.0, 5.0]) == 1
    assert F._segments([]) == 0


# --- flatten_item ----------------------------------------------------------
@pytest.mark.parametrize("method", ["tube", "chord"])
def test_flatten_item_conserves_area_and_smooths(method: str) -> None:
    traces = F.build_traces(SYNTH["steps"])
    t = [0.0, 10.0, 20.0]
    res = F.flatten_item(traces["widget"], t, method, F.EPS_ZERO)
    # Area conserved: total flattened units == original total (100).
    flat_total = sum(res.flat_rate[k] * (t[k + 1] - t[k]) for k in range(2))
    assert flat_total == pytest.approx(100.0)
    # Smoothed from 2 segments (10, 0) to 1 (5, 5).
    assert res.orig_segments == 2
    assert res.revisits == 1
    assert res.flat_rate == pytest.approx([5.0, 5.0])
    assert res.self_stockouts == 0


def test_flatten_item_zero_production_passthrough() -> None:
    tr = F.ItemTrace("x", 2)  # never observed → all zero
    res = F.flatten_item(tr, [0.0, 1.0, 2.0], "tube", F.EPS_ZERO)
    assert res.total_units == 0.0
    assert res.flat_rate == [0.0, 0.0]


# --- orchestrator + post yaml ---------------------------------------------
@pytest.mark.parametrize("method", F.METHODS)
def test_flatten_orchestrator(method: str, model: GameModel) -> None:
    res = F.flatten(SYNTH, method=method, model=model)
    assert res.method == method
    assert res.t == [0.0, 10.0, 20.0]
    assert "widget" in res.flats
    s = res.summary()
    assert set(s) == {
        "items_scored",
        "revisits",
        "orig_segments",
        "revisits_saved",
        "self_stockouts",
        "deficit_lines",
    }


def test_flatten_unknown_method(model: GameModel) -> None:
    with pytest.raises(ValueError, match="unknown method"):
        F.flatten(SYNTH, method="nope", model=model)


def test_build_post_yaml_shape_and_immutability(model: GameModel) -> None:
    original = copy.deepcopy(SYNTH)
    res = F.flatten(SYNTH, method="tube", model=model)
    post = F.build_post_yaml(SYNTH, res, source_ref="rates.yaml")

    # Source dict is untouched (deep copy).
    assert SYNTH == original

    # Production characteristics rewritten to the flattened schedule.
    it0 = post["steps"][0]["items"][0]
    assert it0["production_rate_per_s"] == pytest.approx(5.0)
    assert it0["produced"] == pytest.approx(50.0)
    # Consumption / inventory pass through unchanged.
    assert post["steps"][1]["items"][0]["consumption_rate_per_s"] == 10.0
    assert it0["count_end"] == 100.0

    block = post["post"]
    assert block["method"] == "tube"
    assert block["source"] == "rates.yaml"
    assert block["schema"] == "provisional-rates-mirror"
    assert block["summary"]["items_scored"] == 1
    assert "widget" in block["per_item"]
    assert block["per_item"]["widget"]["revisits"] == 1
    assert isinstance(block["deficits"], list)


def test_post_yaml_is_yaml_roundtrippable(model: GameModel) -> None:
    res = F.flatten(SYNTH, method="chord", model=model)
    post = F.build_post_yaml(SYNTH, res, source_ref="rates.yaml")
    reloaded = yaml.safe_load(yaml.safe_dump(post, sort_keys=False))
    assert reloaded["post"]["method"] == "chord"


# --- documented method invariants (hermetic; example rates.yaml are gitignored
# artifacts absent in CI, so these use a hand-built banking case instead) ------


# Three steps, one item, with interior surplus banking: the requirement floor R
# bulges *above* the straight 0→total chord, so the chord (which ignores the
# tube) dips below R at both interior boundaries — a self-stockout — while the
# tube hugs the floor and never does.
#   P   = [0, 100, 160, 200]   inv = [0, 30, 20, 0]   R = P-inv = [0, 70, 140, 200]
#   chord (deadlines = endpoints only) = [0, 66.7, 133.3, 200]  →  < R at k1,k2
def _banking_step(label, produced, consumed, cs, ce):
    return {
        "label": label,
        "duration_s": 10.0,
        "items": [
            {
                "name": "gear",
                "produced": produced,
                "production_rate_per_s": produced / 10.0,
                "consumption_rate_per_s": consumed / 10.0,
                "consumed": consumed,
                "count_start": cs,
                "count_end": ce,
            }
        ],
    }


BANKING = {
    "scenario": "bank",
    "mode": "lower-bound",
    "initial_time_s": 0.0,
    "steps": [
        _banking_step("s0", 100.0, 70.0, 0.0, 30.0),
        _banking_step("s1", 60.0, 70.0, 30.0, 20.0),
        _banking_step("s2", 40.0, 60.0, 20.0, 0.0),
    ],
}


def test_tube_never_self_stockouts(model: GameModel) -> None:
    res = F.flatten(BANKING, method="tube", model=model)
    assert res.summary()["self_stockouts"] == 0


def test_area_conserved_multi_step(model: GameModel) -> None:
    res = F.flatten(BANKING, method="tube", model=model)
    for fl in res.flats.values():
        flat_total = sum(
            fl.flat_rate[k] * (res.t[k + 1] - res.t[k])
            for k in range(len(fl.flat_rate))
        )
        assert flat_total == pytest.approx(fl.total_units, abs=1e-3)


def test_chord_self_stockouts_where_tube_does_not(model: GameModel) -> None:
    # The cautionary baseline: chord ignores the tube, so on surplus banking it
    # dips below the requirement floor where tube doesn't.
    chord = F.flatten(BANKING, method="chord", model=model).summary()
    tube = F.flatten(BANKING, method="tube", model=model).summary()
    assert chord["self_stockouts"] > 0
    assert tube["self_stockouts"] == 0
    # chord collapses to fewer/equal segments than the tube (at that cost).
    assert chord["revisits"] <= tube["revisits"]
