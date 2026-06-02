"""Tests for L2 visualization (`fplan.l2.viz`) and the `rates viz` CLI.

Viz is a pure consumer of a rates-shaped dict (YAML in → HTML string out) with
a best-effort, optional model load, so it runs fully in CI without SCIP or a
Factorio install. The browser-open helper is tested with `webbrowser` and the
platform detection mocked.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

import yaml
from typer.testing import CliRunner

from fplan import run as run_mod
from fplan.cli import app
from fplan.cli import rates as rates_cli
from fplan.l2 import viz

runner = CliRunner()

RATES = {
    "scenario": "steelaxe",
    "mode": "experimental",
    "l1_method": "forward",
    "initial_time_s": 10.0,
    "pseudo_recipes_version": "1.0.0",
    "solver": {"status": "optimal", "objective_s": 245.7, "gap": 0.0},
    "steps": [
        {
            "label": "automation",
            "duration_s": 100.0,
            "items": [
                {
                    "name": "iron-plate",
                    "production_rate_per_s": 2.0,
                    "consumption_rate_per_s": 1.0,
                    "count_start": 0.0,
                    "count_end": 100.0,
                },
                {
                    "name": "automation-science-pack",
                    "production_rate_per_s": 0.5,
                    "consumption_rate_per_s": 0.0,
                    "count_start": 0.0,
                    "count_end": 50.0,
                },
            ],
            "energy": {"electric_supply_mw": 1.0, "electric_demand_mw": 0.8},
            "capacity": [
                {
                    "building": "lab",
                    "saturated": True,
                    "utilization": 1.0,
                    "recipe_seconds_used": 90.0,
                    "capacity_seconds": 90.0,
                }
            ],
        }
    ],
}


# --- viz module ------------------------------------------------------------


def test_build_dataset_synthesizes_and_categorizes() -> None:
    ds = viz.build_dataset(RATES)  # no data_dir → best-effort skips the model
    assert ds["model_loaded"] is False
    assert viz.POWER_ITEM in ds["items_all"]  # synthesized from energy
    assert "automation-science-pack" in ds["items_all"]
    # Science packs visible by default.
    assert "automation-science-pack" in ds["visible_default"]


def test_render_html_default_three_panels() -> None:
    html = viz.render_html(viz.build_dataset(RATES))
    assert "<h1>L2 timeline</h1>" in html
    for cid in ("chart-prod", "chart-net", "chart-count"):
        assert f'id="{cid}"' in html
    assert '"stepFn"' in html and "Surplus count over time" in html


def test_render_html_parameterized_single_panel() -> None:
    # What a later flatten-viz does: compose with a 1-panel spec + custom heading.
    html = viz.render_html(
        viz.build_dataset(RATES),
        charts=[viz.DEFAULT_CHARTS[0]],
        heading="L2 rate flattening",
    )
    assert "<h1>L2 rate flattening</h1>" in html
    assert 'id="chart-prod"' in html
    assert 'id="chart-net"' not in html and 'id="chart-count"' not in html


def test_build_heatmap_html() -> None:
    hm = viz.build_heatmap_html(RATES)
    assert "capacity-saturation heatmap" in hm and "lab" in hm


def test_categorize() -> None:
    assert viz.categorize("iron-ore") == "Raw resources"
    assert viz.categorize("automation-science-pack") == "Science packs"
    assert viz.categorize(viz.POWER_ITEM) == "Power (MW)"
    assert viz.categorize("some-random-thing") == "Other"


# --- rates viz CLI ---------------------------------------------------------


def _make_run(tmp_path: Path, *, with_rates: bool = True) -> Path:
    d = run_mod.run_dir("r")
    d.mkdir(parents=True)
    (d / run_mod.MANIFEST_NAME).write_text("run: r\ninputs: {}\n")
    if with_rates:
        (d / "rates.yaml").write_text(yaml.safe_dump(RATES))
    return d


def test_viz_run_not_found(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "viz", "nope"])
    assert r.exit_code == 1 and "not found" in (r.stdout + (r.stderr or ""))


def test_viz_rates_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path, with_rates=False)
    r = runner.invoke(app, ["rates", "viz", "r"])
    assert r.exit_code == 1 and "rates file not found" in (r.stdout + (r.stderr or ""))
    assert "rates solve" in (r.stdout + (r.stderr or ""))


def test_viz_from_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "viz", "r", "--from", "gone.yaml"])
    assert r.exit_code == 1 and "rates file not found" in (r.stdout + (r.stderr or ""))


def test_viz_writes_both(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "viz", "r"])
    assert r.exit_code == 0
    assert (rd / "viz" / "rates-timeline.html").exists()
    assert (rd / "viz" / "rates-heatmap.html").exists()
    # Best-effort model: absent in CI → notice.
    assert "model not loaded" in r.stdout


def test_viz_no_heatmap(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "viz", "r", "--no-heatmap"])
    assert r.exit_code == 0
    assert (rd / "viz" / "rates-timeline.html").exists()
    assert not (rd / "viz" / "rates-heatmap.html").exists()


def test_viz_from_candidate_stem(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    cand = rd / "rates-search" / "seed-9.yaml"
    cand.parent.mkdir(parents=True)
    cand.write_text(yaml.safe_dump(RATES))
    r = runner.invoke(app, ["rates", "viz", "r", "--from", str(cand)])
    assert r.exit_code == 0
    # Output stem follows the input → no clobber of the promoted run's viz.
    assert (rd / "viz" / "seed-9-timeline.html").exists()
    assert not (rd / "viz" / "rates-timeline.html").exists()


def test_viz_dry_run(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "viz", "r", "--dry-run"])
    assert r.exit_code == 0 and "dry run" in r.stdout
    assert "rates-timeline.html" in r.stdout
    assert not (rd / "viz").exists()


def test_viz_open_invokes_helper(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    called: dict = {}
    monkeypatch.setattr(
        rates_cli, "_open_in_browser", lambda p: called.setdefault("p", p)
    )
    r = runner.invoke(app, ["rates", "viz", "r", "--open"])
    assert r.exit_code == 0
    assert called["p"].name == "rates-timeline.html"


# --- platform-aware open helper --------------------------------------------


def test_open_unrecognized_platform_skips(tmp_path, monkeypatch) -> None:
    from fplan import factorio

    monkeypatch.setattr(factorio, "current_platform", lambda: None)
    opened = {"n": 0}
    monkeypatch.setattr(
        webbrowser, "open", lambda *a, **k: opened.update(n=opened["n"] + 1)
    )
    out = _capture(lambda: rates_cli._open_in_browser(tmp_path / "x.html"))
    assert "unrecognized platform" in out
    assert opened["n"] == 0  # never attempted


def test_open_untested_platform_warns_and_attempts(tmp_path, monkeypatch) -> None:
    from fplan import factorio

    monkeypatch.setattr(factorio, "current_platform", lambda: "linux")
    monkeypatch.setattr(factorio, "is_untested", lambda p: True)
    seen: dict = {}
    monkeypatch.setattr(
        webbrowser, "open", lambda uri, *a, **k: seen.setdefault("uri", uri) or True
    )
    p = tmp_path / "x.html"
    p.write_text("x")
    out = _capture(lambda: rates_cli._open_in_browser(p))
    assert "untested on Linux" in out
    assert seen["uri"].startswith("file://")


def test_open_failure_falls_back(tmp_path, monkeypatch) -> None:
    from fplan import factorio

    monkeypatch.setattr(factorio, "current_platform", lambda: "darwin")
    monkeypatch.setattr(factorio, "is_untested", lambda p: False)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)  # could not open
    p = tmp_path / "x.html"
    p.write_text("x")
    out = _capture(lambda: rates_cli._open_in_browser(p))
    assert "could not open a browser" in out


def _capture(fn) -> str:
    """Run fn, returning what it echoed to stdout (typer.echo)."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


# --- richer dataset / heatmap branches -------------------------------------

RICH = {
    "scenario": "rich",
    "mode": "experimental",
    "l1_method": "forward",
    "initial_time_s": 0.0,
    "steps": [
        {
            "label": "s0",
            "duration_s": 50.0,
            "items": [
                {
                    "name": "iron-plate",
                    "production_rate_per_s": 1.0,
                    "consumption_rate_per_s": 0.0,
                    "count_start": 0.0,
                    "count_end": 50.0,
                },
            ],
            "energy": {
                "electric_supply_mw": 2.0,
                "character_credit_mw": 0.5,
                "electric_demand_mw": 1.5,
            },
            "mining_assignment": [
                {
                    "building": "electric-mining-drill@iron-ore",
                    "count_start": 0.0,
                    "count_end": 4.0,
                },
            ],
            "smelting_assignment": [
                {
                    "building": "steel-furnace@iron-plate",
                    "count_start": 2.0,
                    "count_end": 1.0,
                },  # removal → cons branch
            ],
            "activity": [
                {
                    "building": "assembling-machine-1",
                    "recipe": "iron-gear-wheel",
                    "cycles": 10.0,
                    "recipe_sec_used": 5.0,
                },
            ],
            "player_time": {
                "movement_s": 5.0,
                "placement_s": 3.0,
                "wood_cutting_s": 1.0,
                "idle_s": 41.0,
            },
            "capacity": [
                {"building": "lab", "saturated": True, "utilization": 1.0},
                {
                    "building": "chemical-plant",
                    "saturated": False,
                    "utilization": 0.4,
                },  # slack cell
            ],
        },
        {
            "label": "s1",
            "duration_s": 30.0,
            "items": [],
            "capacity": [
                {"building": "lab", "saturated": True, "utilization": 1.0},
                # chemical-plant ABSENT this step → blank cell
            ],
        },
    ],
}


def test_build_dataset_rich_fields() -> None:
    ds = viz.build_dataset(RICH)
    items = set(ds["items_all"])
    assert "electric-mining-drill@iron-ore" in items  # mining split
    assert "steel-furnace@iron-plate" in items  # smelting split
    assert f"{viz.PLAYER_TIME_PREFIX}movement" in items  # player-time breakdown
    # player-time items are confined to the production chart.
    assert f"{viz.PLAYER_TIME_PREFIX}idle" in ds["player_time_items"]
    # mining/smelting splits categorize as production facilities.
    assert viz.categorize("electric-mining-drill@iron-ore") == "Production facilities"
    # renders without error across the richer data.
    assert "<h1>L2 timeline</h1>" in viz.render_html(ds)


def test_heatmap_slack_absent_and_multistep() -> None:
    hm = viz.build_heatmap_html(RICH)
    # Both capacity-constrained buildings appear as rows; two step columns.
    assert "lab" in hm and "chemical-plant" in hm
    assert "s0" in hm and "s1" in hm


# --- WF-MAR round-1 fixes: injection, stable colors, error paths, coverage ---

INJECT = {
    "scenario": "</script><img src=x onerror=alert(1)>",
    "mode": "m",
    "l1_method": "f",
    "initial_time_s": 0.0,
    "steps": [
        {
            "label": "</span><b>x</b>",
            "duration_s": 10.0,
            "items": [
                {
                    "name": "</script><svg onload=alert(2)>",
                    "production_rate_per_s": 1.0,
                    "consumption_rate_per_s": 0.0,
                    "count_start": 0.0,
                    "count_end": 1.0,
                }
            ],
            "capacity": [
                {
                    "building": '"></th><script>alert(4)</script>',
                    "saturated": True,
                    "utilization": 1.0,
                }
            ],
        }
    ],
    "solver": {"objective_s": 1.0, "seed": "1<script>alert(5)</script>"},
}


def test_timeline_no_script_breakout() -> None:
    html = viz.render_html(viz.build_dataset(INJECT))
    # Payloads must not survive as live markup; the dataset JSON is </-escaped.
    assert "</script><img" not in html
    assert "</script><svg" not in html
    assert "onerror=alert(1)" not in html or "&lt;/script&gt;" in html
    assert "<\\/script>" in html  # script-safe JSON in the <script> block


def test_heatmap_escapes_building_label_seed() -> None:
    hm = viz.build_heatmap_html(INJECT)
    assert "<script>alert(4)" not in hm  # building name escaped
    assert "<script>alert(5)" not in hm  # seed escaped
    assert "onerror=alert(3)" not in hm
    assert "&lt;script&gt;" in hm  # escaped form present


def test_color_for_item_is_stable() -> None:
    # Deterministic across processes (hashlib, not the salted builtin hash()).
    assert viz.color_for_item("iron-plate") == "hsl(234, 55%, 41%)"


def test_facility_breakdown_when_model_loaded(monkeypatch) -> None:
    # Inject fake model maps so the model-loaded branch + facilities math run
    # without a Factorio install.
    monkeypatch.setattr(
        viz,
        "_load_model_maps",
        lambda data_dir=None: (
            {"assembling-machine-1": 1.0},
            {"iron-gear-wheel": [("iron-gear-wheel", 1.0)]},
            True,
        ),
    )
    ds = viz.build_dataset(RICH)
    assert ds["model_loaded"] is True
    detail = ds["steps"][0]["prod_detail"]["iron-gear-wheel"]
    assert detail[0]["recipe"] == "iron-gear-wheel"
    # facilities = recipe_sec_used / (base_speed · duration) = 5 / (1·50) = 0.1
    assert abs(detail[0]["facilities"] - 0.1) < 1e-9


def test_viz_malformed_yaml_is_clean_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run(tmp_path, with_rates=False)
    # An item missing "name" → build_dataset KeyError → clean exit 1, no traceback.
    (rd / "rates.yaml").write_text(
        yaml.safe_dump({"steps": [{"duration_s": 1.0, "items": [{"prod": 1}]}]})
    )
    r = runner.invoke(app, ["rates", "viz", "r"])
    assert r.exit_code == 1 and "malformed rates YAML" in (r.stdout + (r.stderr or ""))
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_viz_bad_run_name_is_usage_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "viz", "../evil"])
    assert r.exit_code == 2
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_open_success_is_quiet(tmp_path, monkeypatch) -> None:
    from fplan import factorio

    monkeypatch.setattr(factorio, "current_platform", lambda: "darwin")
    monkeypatch.setattr(factorio, "is_untested", lambda p: False)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)
    p = tmp_path / "x.html"
    p.write_text("x")
    out = _capture(lambda: rates_cli._open_in_browser(p))
    assert out.strip() == ""  # happy path emits nothing


def test_viz_open_dry_run_does_not_open(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    opened = {"n": 0}
    monkeypatch.setattr(rates_cli, "_open_in_browser", lambda p: opened.update(n=1))
    r = runner.invoke(app, ["rates", "viz", "r", "--open", "--dry-run"])
    assert r.exit_code == 0 and opened["n"] == 0  # dry-run returns before opening
