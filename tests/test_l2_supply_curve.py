"""Tests for the ore-patch supply curve and the patch-selection L2 input.

Three pure, hermetic surfaces (no SCIP, no Factorio):
  - `instance.apply_patch_selection` / `build_instance(patch_selection_path=...)`
    — the untrusted-file override of tile_pool / oil_spot_count.
  - `solve._spatial_dict` — the `spatial:` block single-sourced from the instance
    (so the viz reads the cap the LP enforced, no model reload).
  - `viz.build_supply_curve_dataset` / `render_supply_curve_html` — the demand
    series (weight-correct utilized + burner context) and the escaped HTML.
Plus the `rates add-selection` / `rates viz` CLI plumbing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fplan import run as run_mod
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.l2 import instance, solve, viz
from fplan.model import GameModel, build_game_data, load_model

runner = CliRunner()
MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"
DRILL = "electric-mining-drill"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


def _base_map() -> instance.MapData:
    return instance.MapData(
        tile_pool={"iron-ore": 5000.0, "copper-ore": 9000.0},
        map_area=1.0,
        oil_spot_count=34,
        water_pump_cap=0.0,
        wood_budget=0.0,
        oil_yield_multiplier=1.0,
    )


# --------------------------------------------------------------------------- #
# apply_patch_selection (untrusted file → tile_pool / oil_spot_count override)
# --------------------------------------------------------------------------- #


def test_apply_patch_selection_overrides(tmp_path: Path) -> None:
    f = tmp_path / "sel.yaml"
    f.write_text(
        "resources:\n"
        "  iron-ore: {unit: drills, total_tiles: 1234}\n"
        "  crude-oil: {unit: pumpjacks, spots: 11}\n"
    )
    warns: list[str] = []
    md = instance.apply_patch_selection(_base_map(), f, warns)
    assert md.tile_pool["iron-ore"] == 1234.0  # overridden
    assert md.tile_pool["copper-ore"] == 9000.0  # absent → full availability
    assert md.oil_spot_count == 11
    assert warns == []


def test_apply_patch_selection_none_is_identity() -> None:
    base = _base_map()
    assert instance.apply_patch_selection(base, None, []) is base


def test_apply_patch_selection_missing_warns(tmp_path: Path) -> None:
    warns: list[str] = []
    md = instance.apply_patch_selection(_base_map(), tmp_path / "nope.yaml", warns)
    assert md.tile_pool["iron-ore"] == 5000.0  # unchanged
    assert warns and "not found" in warns[0]


def test_apply_patch_selection_bad_numeric_skips(tmp_path: Path) -> None:
    f = tmp_path / "sel.yaml"
    f.write_text("resources:\n  iron-ore: {unit: drills, total_tiles: not-a-number}\n")
    warns: list[str] = []
    md = instance.apply_patch_selection(_base_map(), f, warns)
    assert md.tile_pool["iron-ore"] == 5000.0  # skipped, base kept
    assert warns and "non-numeric" in warns[0]


def test_apply_patch_selection_non_mapping_raises(tmp_path: Path) -> None:
    f = tmp_path / "sel.yaml"
    f.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        instance.apply_patch_selection(_base_map(), f, [])


def test_apply_patch_selection_non_finite_spots_skips(tmp_path: Path) -> None:
    # `.inf` is valid YAML → float inf; int(round(inf)) overflows. Untrusted
    # input must skip-with-warning, not escape as a raw traceback (invariant #1).
    f = tmp_path / "sel.yaml"
    f.write_text("resources:\n  crude-oil: {unit: pumpjacks, spots: .inf}\n")
    warns: list[str] = []
    md = instance.apply_patch_selection(_base_map(), f, warns)
    assert md.oil_spot_count == 34  # unchanged from base
    assert warns and "non-numeric" in warns[0]


def test_apply_patch_selection_no_resources_key_warns(tmp_path: Path) -> None:
    f = tmp_path / "sel.yaml"
    f.write_text("seed: 1\nscenario: x\n")  # well-formed, but no `resources:`
    warns: list[str] = []
    md = instance.apply_patch_selection(_base_map(), f, warns)
    assert md.tile_pool["iron-ore"] == 5000.0  # unchanged
    assert warns and "resources" in warns[0]


def test_apply_patch_selection_resources_not_mapping_raises(tmp_path: Path) -> None:
    f = tmp_path / "sel.yaml"
    f.write_text("resources:\n  - iron-ore\n  - copper-ore\n")  # a list, not a map
    with pytest.raises(ValueError, match="must be a mapping"):
        instance.apply_patch_selection(_base_map(), f, [])


# --------------------------------------------------------------------------- #
# build_instance(patch_selection_path=...) end-to-end into the instance
# --------------------------------------------------------------------------- #


def _l1(tmp_path: Path) -> Path:
    p = tmp_path / "order.yaml"
    p.write_text(yaml.safe_dump({"method": "forward", "layers": [["automation"]]}))
    return p


def _map_probe(tmp_path: Path) -> Path:
    p = tmp_path / "map.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "seed": 123,
                "map_gen_settings": {"width": 500, "height": 500},
                "patches": [
                    {
                        "resource": "iron-ore",
                        "tile_count": 5000,
                        "distance": 20.0,
                        "centroid_x": 10.0,
                        "centroid_y": 10.0,
                        "min_x": 0.0,
                        "max_x": 20.0,
                        "min_y": 0.0,
                        "max_y": 20.0,
                    }
                ],
                "oil_spots": [
                    {"x": 100, "y": 100, "amount": 300000, "distance": 141.0}
                ],
                "oil_clusters": [
                    {
                        "id": 0,
                        "centroid_x": 100.0,
                        "centroid_y": 100.0,
                        "spot_count": 1,
                        "total_amount": 300000,
                        "total_yield_pct": 100.0,
                        "distance": 141.0,
                    }
                ],
                "water_patches": [],
                "tree_count": 0,
            }
        )
    )
    return p


def test_build_instance_patch_selection(model: GameModel, tmp_path: Path) -> None:
    from fplan import scenario as scn

    s = scn.from_dict({"name": "t"})
    sel = tmp_path / "sel.yaml"
    sel.write_text("resources:\n  iron-ore: {unit: drills, total_tiles: 99}\n")
    inst = instance.build_instance(
        s,
        _l1(tmp_path),
        model,
        map_probe_path=_map_probe(tmp_path),
        patch_selection_path=sel,
    )
    assert inst.tile_pool["iron-ore"] == 99.0  # restricted from 5000


# --------------------------------------------------------------------------- #
# _spatial_dict — single-sourced from the instance + deployed footprint
# --------------------------------------------------------------------------- #


def test_spatial_dict_single_sources_the_cap(model: GameModel, tmp_path: Path) -> None:
    from fplan import scenario as scn

    inst = instance.build_instance(
        scn.from_dict({"name": "t"}),
        _l1(tmp_path),
        model,
        map_probe_path=_map_probe(tmp_path),
    )
    spatial = solve._spatial_dict(inst, model)
    assert spatial is not None
    fp = inst.deployed_facility(model, model.buildings[DRILL]).tile_footprint
    assert spatial["miners"][DRILL]["footprint"] == pytest.approx(fp)
    assert spatial["miners"][DRILL]["base_speed"] == pytest.approx(
        model.buildings[DRILL].base_speed
    )
    # drill_cap is exactly tile_pool / footprint — the LP's per-ore upper bound.
    assert spatial["resources"]["iron-ore"]["drill_cap"] == pytest.approx(5000.0 / fp)


def test_spatial_dict_none_without_map(model: GameModel, tmp_path: Path) -> None:
    from fplan import scenario as scn

    inst = instance.build_instance(scn.from_dict({"name": "t"}), _l1(tmp_path), model)
    assert solve._spatial_dict(inst, model) is None


# --------------------------------------------------------------------------- #
# build_supply_curve_dataset / render_supply_curve_html
# --------------------------------------------------------------------------- #


def _solved_l2(resource: str = "iron-ore") -> dict:
    return {
        "scenario": "demo",
        "mode": "experimental",
        "initial_time_s": 10.0,
        "spatial": {
            "miners": {DRILL: {"footprint": 11.375, "base_speed": 0.5}},
            "resources": {resource: {"tile_pool": 5000.0, "drill_cap": 439.6}},
            "oil_spot_count": 1,
            "map_area": 250000.0,
            "max_area_fraction": 0.6,
        },
        "steps": [
            {
                "duration_s": 120.0,
                "mining_assignment": [
                    {
                        "building": f"{DRILL}@{resource}",
                        "ore": resource,
                        "count_start": 0,
                        "count_end": 40,
                    }
                ],
                "burner_mining": [{"ore": resource, "drills_equiv": 2.0}],
                # recipe_seconds 1800 / (0.5 speed · 120 dur) = 30 utilized drills
                "capacity": [
                    {
                        "building": f"{DRILL}@{resource}",
                        "recipe_seconds_used": 1800.0,
                        "utilization": 0.75,
                    }
                ],
                "items": [{"name": "pumpjack", "count_end": 1}],
            }
        ],
    }


def _map_artifact() -> dict:
    return {
        "seed": 7,
        "patches": [
            {
                "resource": "iron-ore",
                "tile_count": 4828,
                "distance": 30.0,
                "centroid_x": 20.0,
                "centroid_y": 0.0,
                "min_x": 0,
                "max_x": 40,
                "min_y": -10,
                "max_y": 10,
            },
            {
                "resource": "iron-ore",
                "tile_count": 1000,
                "distance": 80.0,
                "centroid_x": -60.0,
                "centroid_y": 0.0,
                "min_x": -80,
                "max_x": -40,
                "min_y": -10,
                "max_y": 10,
            },
        ],
        "oil_clusters": [
            {
                "id": 0,
                "centroid_x": 100.0,
                "centroid_y": 100.0,
                "spot_count": 1,
                "distance": 141.0,
            }
        ],
        "oil_spots": [{"x": 100, "y": 100}],
        "water_patches": [],
    }


def test_supply_curve_dataset_weight_correct_and_burner() -> None:
    ds = viz.build_supply_curve_dataset(_solved_l2(), _map_artifact())
    assert ds is not None
    assert ds["has_footprint"] is True
    step = ds["series"]["iron-ore"]["steps"][0]
    assert step["built"] == 40.0
    # utilized = recipe_seconds / (base_speed · duration) — NOT a count_end divide.
    assert step["utilized"] == pytest.approx(30.0)
    assert step["burner"] == pytest.approx(2.0)
    assert ds["series"]["iron-ore"]["peak_demand_drills"] == 40.0
    # patch capacity = tile_count / footprint (the deployed footprint from spatial).
    iron = [p for p in ds["patches"] if p["resource"] == "iron-ore"]
    assert iron[0]["capacity"] == pytest.approx(round(4828 / 11.375, 1))


def test_supply_curve_dataset_none_without_patches() -> None:
    assert viz.build_supply_curve_dataset(_solved_l2(), {"patches": []}) is None


def test_supply_curve_dataset_no_spatial_block() -> None:
    l2 = _solved_l2()
    del l2["spatial"]  # a rates.yaml from before the spatial block
    ds = viz.build_supply_curve_dataset(l2, _map_artifact())
    assert ds is not None
    assert ds["has_footprint"] is False
    assert ds["patches"][0]["capacity"] is None  # can't convert tiles → drills


def test_supply_curve_render_substitutes_and_escapes() -> None:
    ds = viz.build_supply_curve_dataset(_solved_l2(), _map_artifact())
    assert ds is not None
    html = viz.render_supply_curve_html(ds)
    for token in ("__SC_DATA__", "__SC_VIEWBOX__", "__SC_TITLE__", "__JS_HELPERS__"):
        assert token not in html
    assert "<svg" in html and "Export YAML" in html


def test_shared_js_helpers_injected_into_both_views() -> None:
    """esc()/fmtAxisTime() are shared verbatim — both views inject the one copy,
    so the time axis + escaping can't drift apart."""
    sc_ds = viz.build_supply_curve_dataset(_solved_l2(), _map_artifact())
    assert sc_ds is not None
    sc = viz.render_supply_curve_html(sc_ds)
    tl = viz.render_html(
        viz.build_dataset(
            {
                "scenario": "s",
                "mode": "experimental",
                "l1_method": "f",
                "initial_time_s": 0.0,
                "solver": {"status": "optimal", "objective_s": 1.0},
                "steps": [{"label": "a", "duration_s": 60.0, "items": []}],
            }
        )
    )
    for html in (sc, tl):
        assert html.count("function fmtAxisTime(t)") == 1  # injected, not duplicated
        assert viz._JS_SHARED_HELPERS in html


def test_supply_curve_render_neutralizes_script_injection() -> None:
    mp = _map_artifact()
    mp["patches"][0]["resource"] = "</script><img src=x onerror=alert(1)>"
    ds = viz.build_supply_curve_dataset(_solved_l2(), mp)
    assert ds is not None
    html = viz.render_supply_curve_html(ds)
    # The embedded JSON must not let the injected </script> close the element.
    assert "</script><img" not in html


# --------------------------------------------------------------------------- #
# CLI: rates add-selection + rates viz supply curve
# --------------------------------------------------------------------------- #


def _make_run(tmp_path: Path, name: str = "r", *, map_text: str | None = None) -> Path:
    (tmp_path / "scn.yaml").write_text("name: t\n")
    (tmp_path / "order.yaml").write_text(
        yaml.safe_dump({"method": "forward", "layers": [["automation"]]})
    )
    (tmp_path / "map.yaml").write_text(
        map_text if map_text is not None else yaml.safe_dump(_map_artifact())
    )
    run_mod.save(
        run_mod.run_dir(name),
        run_mod.Manifest.new(
            name,
            scenario="scn.yaml",
            tech_order="order.yaml",
            map_path="map.yaml",
            created="t0",
        ),
    )
    return run_mod.run_dir(name)


def test_add_selection_binds_and_removes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    (tmp_path / "sel.yaml").write_text("resources: {}\n")
    res = runner.invoke(app, ["rates", "add-selection", "r", "sel.yaml"])
    assert res.exit_code == 0, res.output
    assert run_mod.load(rd).inputs["patch-selection"]["path"] == "sel.yaml"
    res = runner.invoke(app, ["rates", "add-selection", "r", "--remove"])
    assert res.exit_code == 0
    assert "patch-selection" not in run_mod.load(rd).inputs


def test_add_selection_usage_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    # FILE required unless --remove.
    res = runner.invoke(app, ["rates", "add-selection", "r"])
    assert res.exit_code == 2
    # missing file → exit 1.
    res = runner.invoke(app, ["rates", "add-selection", "r", "nope.yaml"])
    assert res.exit_code == 1


def test_viz_emits_supply_curve(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    (rd / "rates.yaml").write_text(yaml.safe_dump(_solved_l2()))
    # No model needed for viz; force the best-effort load to a no-op.
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: None)
    res = runner.invoke(app, ["rates", "viz", "r", "--no-heatmap"])
    assert res.exit_code == 0, res.output
    assert (rd / "viz" / "rates-supply-curve.html").exists()


def test_viz_notes_missing_map(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    (tmp_path / "map.yaml").unlink()  # bound but gone
    (rd / "rates.yaml").write_text(yaml.safe_dump(_solved_l2()))
    res = runner.invoke(app, ["rates", "viz", "r", "--no-heatmap"])
    assert res.exit_code == 0, res.output
    assert "supply-curve skipped" in res.output
    assert (rd / "viz" / "rates-timeline.html").exists()  # timeline still renders


def test_viz_no_supply_curve_flag(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    (rd / "rates.yaml").write_text(yaml.safe_dump(_solved_l2()))
    res = runner.invoke(app, ["rates", "viz", "r", "--no-heatmap", "--no-supply-curve"])
    assert res.exit_code == 0, res.output
    assert not (rd / "viz" / "rates-supply-curve.html").exists()


def test_viz_notes_pre_spatial_rates(tmp_path: Path, monkeypatch) -> None:
    """A rates.yaml from before the spatial: block still renders the supply curve,
    with a note that capacities are omitted until a re-solve."""
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    l2 = _solved_l2()
    del l2["spatial"]
    (rd / "rates.yaml").write_text(yaml.safe_dump(l2))
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: None)
    res = runner.invoke(app, ["rates", "viz", "r", "--no-heatmap"])
    assert res.exit_code == 0, res.output
    assert "predates the spatial: block" in res.output
    assert (rd / "viz" / "rates-supply-curve.html").exists()
