"""L2 visualization — interactive timeline + capacity-saturation heatmap.

Pure consumer of a run's ``rates.yaml`` (the L2 solver output); the CLI
(``fplan rates viz``) handles run resolution, file output, and ``--open``.

``render_html`` emits a self-contained interactive HTML file with stacked
time-series panels sharing one zoomable x-axis. The default timeline has three:
  1. Raw production rate over time (items/s)
  2. Net production rate over time (production - consumption, items/s)
  3. Surplus count over time (running stockpile)

Per-item lines are rendered as step functions (panels 1 + 2) or linear-connect
(panel 3), because L2's underlying model is piecewise-constant within a step.
A tree-grouped legend toggles per-item visibility (science packs +
electric-mining-drill visible by default).

``build_heatmap_html`` emits the companion **capacity-saturation heatmap** from
the per-step ``capacity`` field: rows = capacity-constrained buildings, columns
= steps in tech order, a cell black when that building is saturated (on the
critical surface, relevant to t_FINAL — L2→L3 handoff Theme 2).

The renderer is deliberately **parameterized** (chart spec + heading/title/meta
as arguments, with ``build_dataset`` / ``categorize`` / ``color_for_item`` /
``default_meta_parts`` as reusable functions) so a later flatten-viz can compose it with
a different chart spec + an augmented dataset, rather than string-surgery on the
template. See ``DEFAULT_CHARTS``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from html import escape
from pathlib import Path

# Template placeholders, substituted in one non-rescanning pass (see render_html).
_PLACEHOLDER_RE = re.compile(
    r"__(?:HEADING|CHART_PANES|CHARTS_JSON|TITLE|META|DATA_JSON|JS_HELPERS)__"
)

# JS helpers shared verbatim by every L2 view (timeline + supply curve), injected
# via the __JS_HELPERS__ placeholder. Keeping them in one place means escaping
# and axis-tick formatting stay identical across views — change here, both move.
_JS_SHARED_HELPERS = r"""// HTML-escape DATA-derived strings before any innerHTML interpolation — names
// come from the rates YAML / map artifact, untrusted in the DOM.
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]); }
// Compact axis-tick label: drop the seconds on minute-aligned ticks ("2m" not
// "2m0.0s") and the minutes below 1 min ("30s"), so whole-minute ticks don't
// overlap. Shared so every time axis reads the same.
function fmtAxisTime(t) {
  const m = Math.floor(t / 60);
  const s = Math.round((t - m * 60) * 10) / 10;
  if (s === 0) return `${m}m`;
  if (m === 0) return `${s}s`;
  return `${m}m${s}s`;
}"""

# -- Item categorization -------------------------------------------------

RAW_RESOURCES = {
    "iron-ore",
    "copper-ore",
    "coal",
    "stone",
    "uranium-ore",
    "water",
    "crude-oil",
    "wood",
}
SMELTED_PLATES = {"iron-plate", "copper-plate", "steel-plate"}
PRODUCTION_FACILITIES = {
    "assembling-machine-1",
    "assembling-machine-2",
    "assembling-machine-3",
    "electric-furnace",
    "steel-furnace",
    "stone-furnace",
    "oil-refinery",
    "chemical-plant",
    "electric-mining-drill",
    "burner-mining-drill",
    "pumpjack",
    "offshore-pump",
    "boiler",
    "steam-engine",
    "lab",
    "rocket-silo",
}

# Synthetic item name used for the per-step MW supply/demand line.
POWER_ITEM = "_power-mw"

CATEGORY_ORDER = [
    "Raw resources",
    "Smelted plates",
    "Science packs",
    "Power (MW)",
    "Player time (s)",
    "Production facilities",
    "Other",
]

# Prefix for the synthetic per-step player-time breakdown items. These
# carry SECONDS (not a rate) in their `prod` field and are shoehorned into
# the production-rate chart only (see PLAYER_TIME_CHART in the JS).
PLAYER_TIME_PREFIX = "player-time:"


def categorize(name: str) -> str:
    if name == POWER_ITEM:
        return "Power (MW)"
    if name in RAW_RESOURCES:
        return "Raw resources"
    if name in SMELTED_PLATES:
        return "Smelted plates"
    if name.endswith("-science-pack"):
        return "Science packs"
    if name.startswith(PLAYER_TIME_PREFIX):
        return "Player time (s)"
    if name in PRODUCTION_FACILITIES:
        return "Production facilities"
    # Per-ore drill split, e.g. "electric-mining-drill@iron-ore".
    if "@" in name and name.split("@", 1)[0] in PRODUCTION_FACILITIES:
        return "Production facilities"
    return "Other"


def _stable_hash(s: str) -> int:
    """A process-independent hash. Python's builtin ``hash`` is salted per run
    (``PYTHONHASHSEED``), which would re-color every item on each invocation and
    defeat comparing two viz outputs (the promoted run vs a ``--from`` candidate)."""
    return int(hashlib.md5(s.encode()).hexdigest(), 16)


def color_for_item(name: str) -> str:
    """Stable per-item HSL color from a hash, with mild S/L jitter to
    spread visually-similar hues. Stable across runs (see ``_stable_hash``)."""
    h = _stable_hash(name + "_hue") % 360
    s = 55 + (_stable_hash(name + "_sat") % 25)  # 55-80
    L = 38 + (_stable_hash(name + "_lum") % 18)  # 38-56
    return f"hsl({h}, {s}%, {L}%)"


# -- Dataset extraction --------------------------------------------------


def _load_model_maps(data_dir: Path | None = None):
    """Load the game model to derive per-recipe facility counts.

    Returns (building_speed, recipe_outputs, ok). Failure is non-fatal:
    the viz still renders from pure YAML, just without the facility
    breakdown (so it stays runnable without a Factorio install — pass no
    ``data_dir`` and this returns empty maps). The model is the source of
    `base_speed` (facility count = recipe-seconds / (base_speed · duration))
    and of recipe→output mapping (so we can answer "which facility produces
    item X").
    """
    try:
        from fplan.model import load_model

        m = load_model(data_dir=data_dir)
    except Exception:
        return {}, {}, False
    building_speed = {
        n: float(b.base_speed)
        for n, b in m.buildings.items()
        if getattr(b, "base_speed", None)
    }
    recipe_outputs = {
        rn: [(o.name, float(o.amount)) for o in r.outputs]
        for rn, r in m.recipes.items()
    }
    return building_speed, recipe_outputs, True


def build_dataset(l2: dict, *, data_dir: Path | None = None) -> dict:
    """Walk L2's per-step `items` list (which already carries
    production_rate_per_s, consumption_rate_per_s, count_start, count_end
    per item per step) and synthesize a power-MW item from `energy`.

    ``data_dir`` (optional) enables the best-effort facility-count breakdown;
    without it the dataset still builds from the YAML alone.
    """
    steps_yaml = l2.get("steps", [])
    step_records = []
    # The initial state may sit at a nonzero in-game time (e.g. 3:12 for
    # default-victory, the hand-crafted seed base). Start the cumulative
    # clock there so every step t0/t1 — and hence the chart x-axis and the
    # left-column step times — read as absolute in-game time, not relative.
    initial_time_s = float(l2.get("initial_time_s", 0.0) or 0.0)
    clock = initial_time_s  # running absolute in-game time, advanced per step

    building_speed, recipe_outputs, model_loaded = _load_model_maps(data_dir)

    # Per-item totals for default-visibility ranking.
    total_flow: defaultdict[str, float] = defaultdict(float)

    for i, s in enumerate(steps_yaml):
        duration = float(s.get("duration_s", 0.0)) or 1e-9
        start_t = clock
        end_t = clock + duration
        clock = end_t

        rates: dict[str, dict] = {}
        for it in s.get("items", []):
            name = it["name"]
            p = float(it.get("production_rate_per_s") or 0.0)
            c = float(it.get("consumption_rate_per_s") or 0.0)
            cs = float(it.get("count_start") or 0.0)
            ce = float(it.get("count_end") or 0.0)
            rates[name] = {"prod": p, "cons": c, "count_start": cs, "count_end": ce}
            total_flow[name] += (abs(p) + abs(c)) * duration

        # Per-ore electric-drill split: the LP assigns drills to a specific
        # ore (a drill on iron can't switch to copper), emitted as
        # `mining_assignment`. Surface each as a synthetic item (e.g.
        # "electric-mining-drill@iron-ore") so the split is visible
        # alongside the aggregate drill. prod/cons mirror the aggregate
        # drill's build-rate semantics — the per-step count delta over
        # duration — so the production / net-rate charts show drills being
        # added (positive) or removed, not a flat zero. The surplus-count
        # chart reads count_start/count_end directly.
        for ma in s.get("mining_assignment", []) or []:
            name = ma.get("building")
            if not name:
                continue
            cs = float(ma.get("count_start") or 0.0)
            ce = float(ma.get("count_end") or 0.0)
            delta = ce - cs
            prod = max(0.0, delta) / duration
            cons = max(0.0, -delta) / duration
            rates[name] = {
                "prod": prod,
                "cons": cons,
                "count_start": cs,
                "count_end": ce,
            }
            total_flow[name] += (abs(prod) + abs(cons)) * duration

        # Per-output steel-furnace split: same treatment as the drill split
        # above (a furnace committed to iron-plate can't switch to copper),
        # emitted as `smelting_assignment` and surfaced as synthetic items
        # like "steel-furnace@iron-plate".
        for sa in s.get("smelting_assignment", []) or []:
            name = sa.get("building")
            if not name:
                continue
            cs = float(sa.get("count_start") or 0.0)
            ce = float(sa.get("count_end") or 0.0)
            delta = ce - cs
            prod = max(0.0, delta) / duration
            cons = max(0.0, -delta) / duration
            rates[name] = {
                "prod": prod,
                "cons": cons,
                "count_start": cs,
                "count_end": ce,
            }
            total_flow[name] += (abs(prod) + abs(cons)) * duration

        # Per-item production-facility breakdown: which recipe(s) produce
        # each item this step, on which building, and the fractional number
        # of facilities running it. facilities = recipe-seconds-of-work /
        # (base_speed · duration) — verified to sum back to the stored
        # building counts (count_end for crafting, count_start for raw
        # extraction). Keyed by OUTPUT item so the UI can answer "what makes
        # this item, with how many machines". Pseudo-recipes (research/,
        # power/, launch) aren't in recipe_outputs and are skipped.
        prod_detail: dict[str, list] = defaultdict(list)
        for a in s.get("activity", []) or []:
            building = a.get("building")
            recipe = a.get("recipe")
            cycles = float(a.get("cycles") or 0.0)
            rsec = float(a.get("recipe_sec_used") or cycles)
            sp = building_speed.get(building) or building_speed.get(
                (building or "").split("@", 1)[0]
            )
            facilities = (rsec / (sp * duration)) if sp else None
            for item_name, amt in recipe_outputs.get(recipe, []):
                prod_detail[item_name].append(
                    {
                        "recipe": recipe,
                        "building": building,
                        "facilities": facilities,
                        "item_rate": cycles * amt / duration,
                    }
                )

        # Power: synthetic item. Supply (production), demand (consumption).
        # (character_credit was removed when the player became a power-free
        # hand-craft facility; older YAMLs simply lack the key → 0.0.)
        e = s.get("energy", {}) or {}
        supply = float(e.get("electric_supply_mw") or 0.0)
        demand = float(e.get("electric_demand_mw") or 0.0)
        rates[POWER_ITEM] = {
            "prod": supply,
            "cons": demand,
            "count_start": 0.0,
            "count_end": 0.0,
        }
        total_flow[POWER_ITEM] += (supply + demand) * duration

        # Player-time breakdown: synthetic seconds-valued items, one per
        # component (movement / placement / wood-cutting / idle). The value
        # is carried in `prod` so it plots in the production-rate chart; the
        # JS restricts these items to that chart only (mixing seconds into
        # the net-rate / surplus-count panels would be meaningless). idle =
        # duration − total player time the constraint consumed.
        pt = s.get("player_time") or {}
        if pt:
            pt_components = {
                f"{PLAYER_TIME_PREFIX}movement": pt.get("movement_s", 0.0),
                f"{PLAYER_TIME_PREFIX}placement": pt.get("placement_s", 0.0),
                f"{PLAYER_TIME_PREFIX}wood-cutting": pt.get("wood_cutting_s", 0.0),
                f"{PLAYER_TIME_PREFIX}idle": pt.get("idle_s", 0.0),
            }
            for name, secs in pt_components.items():
                rates[name] = {
                    "prod": float(secs),
                    "cons": 0.0,
                    "count_start": 0.0,
                    "count_end": 0.0,
                }
                total_flow[name] += abs(float(secs))

        # Hand-crafting breakdown: the recipes the character made this step
        # (the x_hand activity, `building: character`), alphabetical, with
        # cycle counts. Powers the top-bar "Hand-crafting" panel. Counts that
        # would render as "0.00" (< 0.005, the panel's 2-decimal precision) are
        # dropped as dust rather than listed as zero.
        handcraft = sorted(
            (
                {
                    "recipe": a.get("recipe"),
                    "count": float(a.get("cycles") or 0.0),
                }
                for a in (s.get("activity") or [])
                if a.get("building") == "character"
                and float(a.get("cycles") or 0.0) >= 0.005
            ),
            key=lambda h: h["recipe"] or "",
        )

        step_records.append(
            {
                "i": i,
                "label": s.get("label", f"step-{i}"),
                "duration": duration,
                "t0": start_t,
                "t1": end_t,
                "rates": rates,
                "prod_detail": dict(prod_detail),
                "handcraft": handcraft,
            }
        )

    # All items seen anywhere.
    items_all = sorted(total_flow.keys(), key=lambda n: -total_flow[n])

    # Group by category. Within each subsection sort alphabetically, EXCEPT
    # Science packs — those keep flow-rank order (the research-curve reading
    # order, which `items_all` already provides). `items_all` itself stays
    # flow-ranked for Top-10 / chart iteration; this only reorders the legend.
    by_cat: dict[str, list[str]] = defaultdict(list)
    for it in items_all:
        by_cat[categorize(it)].append(it)
    for cat, items in by_cat.items():
        if cat != "Science packs":
            items.sort()
    categories = [{"name": cat, "items": by_cat.get(cat, [])} for cat in CATEGORY_ORDER]

    # Colors (stable across runs).
    colors = {it: color_for_item(it) for it in items_all}

    # Default visibility: all science packs + electric-mining-drill.
    # Science-pack curves surface the smoothing the tech-research ORDER
    # needs (the L2→L1 feedback signal); electric-mining-drill count is
    # the resource-extraction-saturation proxy for whether the run is
    # physically achievable. Everything else stays one click away in the
    # legend but starts hidden so these two signals aren't drowned out.
    visible_default = set(by_cat.get("Science packs", []))
    if "electric-mining-drill" in total_flow:
        visible_default.add("electric-mining-drill")

    # Tech anchors (one per step start).
    tech_anchors = [
        {"label": s["label"], "time": s["t0"], "i": s["i"]} for s in step_records
    ]

    return {
        "scenario": l2.get("scenario", "unknown"),
        "mode": l2.get("mode", "unknown"),
        "l1_method": l2.get("l1_method", "unknown"),
        "pseudo_recipes_version": l2.get("pseudo_recipes_version"),
        "solver": l2.get("solver", {}),
        "initial_time_s": initial_time_s,
        "total_time": clock,
        "steps": step_records,
        "items_all": items_all,
        "categories": categories,
        "colors": colors,
        # Items confined to the production-rate chart (seconds-valued
        # player-time breakdown); the JS skips them in the other panels.
        "player_time_items": [
            it for it in items_all if it.startswith(PLAYER_TIME_PREFIX)
        ],
        "visible_default": sorted(visible_default),
        "tech_anchors": tech_anchors,
        "model_loaded": model_loaded,
    }


# -- HTML rendering ------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>L2 timeline — __TITLE__</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 13px; color: #222; }
  body { display: flex; flex-direction: column; height: 100vh; }
  header { padding: 6px 12px; background: #1f2937; color: #fff; display: flex; align-items: baseline; gap: 12px; flex: 0 0 auto; }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  header .meta { font-size: 11px; color: #9ca3af; }
  #main { flex: 1; display: flex; min-height: 0; }
  #nav { width: 180px; flex: 0 0 180px; border-right: 1px solid #e5e7eb; overflow-y: auto; padding: 6px 4px; }
  #nav h3 { font-size: 11px; text-transform: uppercase; color: #6b7280; margin: 6px 4px 4px; letter-spacing: 0.04em; }
  #nav .tech-row { padding: 2px 6px; cursor: pointer; border-radius: 3px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #nav .tech-row:hover { background: #f3f4f6; }
  #nav .tech-row.selected { background: #dbeafe; font-weight: 600; }
  #nav .tech-row .t { color: #6b7280; margin-right: 4px; font-variant-numeric: tabular-nums; font-size: 10px; }

  #center { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  #charts { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .chart-pane { flex: 1; display: flex; flex-direction: column; min-height: 0; border-bottom: 1px solid #e5e7eb; }
  .chart-title { font-size: 11px; padding: 3px 8px; background: #f9fafb; color: #374151; border-bottom: 1px solid #e5e7eb; flex: 0 0 auto; font-weight: 500; }
  .chart-svg-wrap { flex: 1; position: relative; min-height: 0; }
  .chart-svg { position: absolute; inset: 0; width: 100%; height: 100%; cursor: crosshair; user-select: none; }

  #details { flex: 0 0 auto; max-height: 25vh; overflow-y: auto; border-top: 2px solid #d1d5db; padding: 4px 8px; background: #fafafa; }
  #details h3 { font-size: 11px; text-transform: uppercase; color: #6b7280; margin: 4px 0; }
  #details table { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
  #details th, #details td { padding: 2px 6px; text-align: right; border-bottom: 1px solid #f3f4f6; }
  #details th { background: #f3f4f6; position: sticky; top: 0; }
  #details td:first-child, #details th:first-child { text-align: left; }
  #details .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
  #details td.item-cell { cursor: pointer; }
  #details td.item-cell:hover { text-decoration: underline; }

  #cell-popup { position: absolute; display: none; z-index: 20; background: #fff; border: 1px solid #cbd5e1; border-radius: 5px; box-shadow: 0 4px 16px rgba(0,0,0,0.18); font-size: 11px; font-variant-numeric: tabular-nums; max-width: 360px; overflow: hidden; }
  #cell-popup .cp-head { display: flex; align-items: center; gap: 6px; padding: 5px 8px; background: #1f2937; color: #fff; font-weight: 600; }
  #cell-popup .cp-head .swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
  #cell-popup .cp-head .cp-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #cell-popup .cp-x { cursor: pointer; font-size: 15px; line-height: 1; padding: 0 2px; color: #cbd5e1; }
  #cell-popup .cp-x:hover { color: #fff; }
  #cell-popup .cp-sub { padding: 4px 8px; color: #6b7280; border-bottom: 1px solid #f3f4f6; }
  #cell-popup .cp-empty { padding: 8px; color: #6b7280; }
  #cell-popup table.cp-table { border-collapse: collapse; width: 100%; }
  #cell-popup table.cp-table th, #cell-popup table.cp-table td { padding: 3px 8px; text-align: right; border-bottom: 1px solid #f3f4f6; white-space: nowrap; }
  #cell-popup table.cp-table th { background: #f3f4f6; color: #374151; }
  #cell-popup table.cp-table td:first-child, #cell-popup table.cp-table th:first-child,
  #cell-popup table.cp-table td:nth-child(2), #cell-popup table.cp-table th:nth-child(2) { text-align: left; }
  #cell-popup tr.cp-total td { font-weight: 600; border-top: 1px solid #d1d5db; }
  .hdr-btn { font-size: 11px; padding: 2px 9px; cursor: pointer; background: #374151; color: #e5e7eb; border: 1px solid #4b5563; border-radius: 4px; }
  .hdr-btn:hover { background: #4b5563; color: #fff; }
  .hdr-btn.active { background: #2563eb; border-color: #2563eb; color: #fff; }
  #handcraft-panel { position: absolute; top: 38px; right: 10px; bottom: 10px; width: 300px; z-index: 30; background: #fff; border: 1px solid #cbd5e1; border-radius: 6px; box-shadow: 0 6px 24px rgba(0,0,0,0.22); display: flex; flex-direction: column; overflow: hidden; }
  #handcraft-panel .hp-head { display: flex; align-items: center; gap: 6px; padding: 6px 9px; background: #1f2937; color: #fff; font-weight: 600; font-size: 12px; flex: 0 0 auto; }
  #handcraft-panel .hp-title { flex: 1; }
  #handcraft-panel .hp-x { cursor: pointer; font-size: 16px; line-height: 1; padding: 0 2px; color: #cbd5e1; }
  #handcraft-panel .hp-x:hover { color: #fff; }
  #handcraft-panel .hp-body { overflow-y: auto; padding: 4px 0; font-size: 11px; font-variant-numeric: tabular-nums; }
  #handcraft-panel .hp-step { padding: 4px 9px 2px; font-weight: 600; color: #374151; background: #f3f4f6; border-top: 1px solid #e5e7eb; display: flex; gap: 6px; }
  #handcraft-panel .hp-step .t { color: #6b7280; font-weight: 400; }
  #handcraft-panel .hp-recipe { padding: 1px 9px 1px 18px; display: flex; justify-content: space-between; gap: 8px; }
  #handcraft-panel .hp-recipe .n { color: #6b7280; }
  #handcraft-panel .hp-none { padding: 1px 9px 1px 18px; color: #9ca3af; font-style: italic; }

  #legend { width: 240px; flex: 0 0 240px; border-left: 1px solid #e5e7eb; overflow-y: auto; padding: 6px 4px; }
  #legend .ctrl-row { padding: 4px 6px; display: flex; gap: 6px; font-size: 11px; }
  #legend .ctrl-row button { font-size: 11px; padding: 1px 6px; cursor: pointer; }
  #legend .cat { margin-bottom: 4px; }
  #legend .cat-header { padding: 3px 4px; font-size: 11px; font-weight: 600; color: #374151; cursor: pointer; background: #f3f4f6; display: flex; align-items: center; gap: 4px; user-select: none; }
  #legend .cat-header .caret { transition: transform 0.1s; font-size: 9px; }
  #legend .cat-header.collapsed .caret { transform: rotate(-90deg); }
  #legend .cat-items { padding: 2px 0; }
  #legend .cat-items.hidden { display: none; }
  #legend .item-row { padding: 2px 6px 2px 18px; display: flex; align-items: center; gap: 6px; cursor: pointer; font-size: 12px; white-space: nowrap; }
  #legend .item-row:hover { background: #f9fafb; }
  #legend .item-row input { margin: 0; }
  #legend .item-row .swatch { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
  #legend .item-row .name { flex: 1; overflow: hidden; text-overflow: ellipsis; }

  .axis-line { stroke: #9ca3af; stroke-width: 1; fill: none; }
  .axis-text { font-size: 10px; fill: #4b5563; }
  .step-bound { stroke: #d1d5db; stroke-width: 1; stroke-dasharray: 2,3; }
  .step-label { font-size: 9px; fill: #6b7280; }
  .selected-bound { stroke: #2563eb; stroke-width: 1.5; stroke-dasharray: none; }
  .item-line { fill: none; stroke-width: 1.5; }
  .item-line:hover { stroke-width: 3; }

  #tooltip { position: absolute; pointer-events: none; background: rgba(31, 41, 55, 0.95); color: #fff; padding: 4px 8px; border-radius: 3px; font-size: 11px; font-variant-numeric: tabular-nums; display: none; z-index: 10; max-width: 280px; }
</style>
</head>
<body>
<header>
  <h1>__HEADING__</h1>
  <div class="meta">__META__</div>
  <div style="flex: 1"></div>
  <button id="handcraft-btn" class="hdr-btn" title="Per-step hand-crafting (character)">Hand-crafting</button>
  <div class="meta" id="zoom-info"></div>
</header>
<div id="main">
  <div id="nav">
    <h3>Tech / step</h3>
    <div id="tech-list"></div>
  </div>
  <div id="center">
    <div id="charts">
__CHART_PANES__
    </div>
    <div id="details">
      <h3 id="details-title">Click a chart to select a step</h3>
      <table id="details-table"></table>
    </div>
  </div>
  <div id="legend">
    <div class="ctrl-row">
      <button id="legend-all">All</button>
      <button id="legend-none">None</button>
      <button id="legend-top10">Top 10</button>
    </div>
    <div id="legend-tree"></div>
  </div>
</div>
<div id="tooltip"></div>
<div id="cell-popup"></div>
<div id="handcraft-panel" style="display:none">
  <div class="hp-head">
    <span class="hp-title">Hand-crafting by step (character)</span>
    <span class="hp-x" id="handcraft-x" title="close">×</span>
  </div>
  <div class="hp-body" id="handcraft-body"></div>
</div>
<script>
const DATA = __DATA_JSON__;
__JS_HELPERS__
const MARGIN = { top: 6, right: 10, bottom: 22, left: 60 };

// Per-chart spec: which value to plot, what label. Injected by render_html
// from the Python chart spec so a 1-panel view (flatten) reuses this template
// without editing it.
const CHARTS = __CHARTS_JSON__;

// Player-time breakdown items carry seconds (not a rate) and only make
// sense in the production-rate chart. Confine them there.
const PLAYER_TIME_CHART = "chart-prod";
const playerTimeItems = new Set(DATA.player_time_items || []);
function inChart(item, specId) {
  return specId === PLAYER_TIME_CHART || !playerTimeItems.has(item);
}

// Visibility state.
const visible = new Set(DATA.visible_default);
let selectedStepIdx = null;
// Times are absolute in-game seconds: the domain starts at the initial
// state's timestamp (default-victory: 192s), not 0.
let xMin = DATA.initial_time_s, xMax = DATA.total_time;
const X_INITIAL = { min: DATA.initial_time_s, max: DATA.total_time };

// --- helpers ---
function valueAt(step, item, key) {
  const r = step.rates[item];
  if (!r) return null;
  if (key === "prod") return r.prod;
  if (key === "cons") return r.cons;
  if (key === "net")  return r.prod - r.cons;
  if (key === "count_start") return r.count_start;
  if (key === "count_end")   return r.count_end;
  return null;
}

function fmt(v) {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "0";
  const abs = Math.abs(v);
  // Very large values (e.g. the unconstrained water slack ~1e9) stay in
  // exponential so they don't print as a 10-digit wall. Everything else
  // rounds to 2 decimals, so near-zero dust (2.72e-6) reads as "0.00"
  // instead of distracting scientific notation.
  if (abs >= 1000) return v.toExponential(2);
  const r = v.toFixed(2);
  return r === "-0.00" ? "0.00" : r;  // avoid signed-zero from tiny negatives
}

function fmtTime(t) {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}m${s.toFixed(1)}s`;
}

// --- legend tree ---
function buildLegend() {
  const root = document.getElementById("legend-tree");
  root.innerHTML = "";
  for (const cat of DATA.categories) {
    if (cat.items.length === 0) continue;
    const catEl = document.createElement("div");
    catEl.className = "cat";
    const hdr = document.createElement("div");
    hdr.className = "cat-header";
    hdr.innerHTML = `<span class="caret">▼</span><span class="cat-name">${esc(cat.name)}</span><span style="flex:1"></span><span class="cat-count" style="color:#9ca3af;font-weight:normal">${cat.items.length}</span>`;
    const list = document.createElement("div");
    list.className = "cat-items";
    hdr.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      hdr.classList.toggle("collapsed");
      list.classList.toggle("hidden");
    });
    for (const item of cat.items) {
      const row = document.createElement("label");
      row.className = "item-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = visible.has(item);
      cb.addEventListener("change", () => {
        if (cb.checked) visible.add(item); else visible.delete(item);
        renderAllCharts();
        renderDetails();
      });
      const sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = DATA.colors[item];
      const nm = document.createElement("span");
      nm.className = "name";
      // Flatten view annotates each item with its revisit count (↻N).
      nm.textContent = (DATA.revisits && DATA.revisits[item] != null)
        ? item + "  ↻" + DATA.revisits[item] : item;
      row.appendChild(cb);
      row.appendChild(sw);
      row.appendChild(nm);
      list.appendChild(row);
    }
    catEl.appendChild(hdr);
    catEl.appendChild(list);
    root.appendChild(catEl);
  }
}

function setAllVisibility(predicate) {
  visible.clear();
  for (const item of DATA.items_all) {
    if (predicate(item)) visible.add(item);
  }
  buildLegend();
  renderAllCharts();
  renderDetails();
}

// --- tech nav ---
function buildNav() {
  const root = document.getElementById("tech-list");
  root.innerHTML = "";
  for (const tech of DATA.tech_anchors) {
    const row = document.createElement("div");
    row.className = "tech-row";
    row.dataset.step = tech.i;
    row.title = `${tech.label} @ ${fmtTime(tech.time)}`;
    row.innerHTML = `<span class="t">${fmtTime(tech.time)}</span>${esc(tech.label)}`;
    row.addEventListener("click", () => {
      // Center viewport on this step.
      const step = DATA.steps[tech.i];
      const center = (step.t0 + step.t1) / 2;
      const span = xMax - xMin;
      xMin = Math.max(DATA.initial_time_s, center - span / 2);
      xMax = Math.min(DATA.total_time, xMin + span);
      selectedStepIdx = tech.i;
      renderAllCharts();
      renderDetails();
    });
    root.appendChild(row);
  }
}

// --- charts ---
function chartMetrics(svgEl) {
  const r = svgEl.getBoundingClientRect();
  const w = Math.max(200, r.width);
  const h = Math.max(80, r.height);
  return {
    w, h,
    plotX0: MARGIN.left,
    plotY0: MARGIN.top,
    plotW: w - MARGIN.left - MARGIN.right,
    plotH: h - MARGIN.top - MARGIN.bottom,
  };
}

function dataYRange(spec) {
  // Compute y-range across visible items + current x-range.
  let yMin = 0, yMax = 0;
  for (const item of visible) {
    if (!inChart(item, spec.id)) continue;
    for (const step of DATA.steps) {
      if (step.t1 < xMin || step.t0 > xMax) continue;
      let v;
      if (spec.key === "count") {
        const a = valueAt(step, item, "count_start") ?? 0;
        const b = valueAt(step, item, "count_end") ?? 0;
        yMin = Math.min(yMin, a, b);
        yMax = Math.max(yMax, a, b);
      } else {
        v = (spec.key === "prod") ? (step.rates[item]?.prod ?? 0)
            : (spec.key === "net") ? ((step.rates[item]?.prod ?? 0) - (step.rates[item]?.cons ?? 0))
            : 0;
        yMin = Math.min(yMin, v);
        yMax = Math.max(yMax, v);
        // A diff chart (flatten view) overlays a second series per item;
        // size the y-range to both so neither curve clips.
        if (spec.overlayKey) {
          const ov = step.rates[item]?.[spec.overlayKey];
          if (ov !== null && ov !== undefined) {
            yMin = Math.min(yMin, ov);
            yMax = Math.max(yMax, ov);
          }
        }
      }
    }
  }
  if (yMin === 0 && yMax === 0) { yMin = 0; yMax = 1; }
  // Floor the positive max at 1.0: a chart whose visible peak is ~1e-5
  // would otherwise zoom into noise. Larger peaks size normally.
  yMax = Math.max(yMax, 1.0);
  // Pad 5% above and 5% below; keep zero in range if possible.
  const range = yMax - yMin;
  if (range === 0) { yMax += 1; }
  return { yMin: yMin - 0.05 * (yMax - yMin), yMax: yMax + 0.05 * (yMax - yMin) };
}

function niceTicks(min, max, target = 5) {
  const range = max - min;
  if (range <= 0) return [min, max];
  const rough = range / target;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  let step;
  if (norm < 1.5) step = mag;
  else if (norm < 3.5) step = 2 * mag;
  else if (norm < 7.5) step = 5 * mag;
  else step = 10 * mag;
  const ticks = [];
  const start = Math.ceil(min / step) * step;
  for (let v = start; v <= max + 1e-9; v += step) ticks.push(v);
  return ticks;
}

function renderChart(spec) {
  const svg = document.getElementById(spec.id);
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const m = chartMetrics(svg);
  svg.setAttribute("viewBox", `0 0 ${m.w} ${m.h}`);

  const { yMin, yMax } = dataYRange(spec);
  const xScale = (t) => m.plotX0 + ((t - xMin) / (xMax - xMin)) * m.plotW;
  const yScale = (v) => m.plotY0 + m.plotH - ((v - yMin) / (yMax - yMin)) * m.plotH;

  const ns = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(ns, "g");

  // Clip
  const clipId = `clip-${spec.id}`;
  const defs = document.createElementNS(ns, "defs");
  const clip = document.createElementNS(ns, "clipPath");
  clip.setAttribute("id", clipId);
  const cr = document.createElementNS(ns, "rect");
  cr.setAttribute("x", m.plotX0); cr.setAttribute("y", m.plotY0);
  cr.setAttribute("width", m.plotW); cr.setAttribute("height", m.plotH);
  clip.appendChild(cr); defs.appendChild(clip); g.appendChild(defs);

  // Y-axis ticks + grid
  for (const v of niceTicks(yMin, yMax, 5)) {
    const y = yScale(v);
    const grid = document.createElementNS(ns, "line");
    grid.setAttribute("x1", m.plotX0); grid.setAttribute("x2", m.plotX0 + m.plotW);
    grid.setAttribute("y1", y); grid.setAttribute("y2", y);
    grid.setAttribute("stroke", "#f3f4f6"); grid.setAttribute("stroke-width", 1);
    g.appendChild(grid);
    const lbl = document.createElementNS(ns, "text");
    lbl.setAttribute("class", "axis-text");
    lbl.setAttribute("x", m.plotX0 - 4); lbl.setAttribute("y", y + 3);
    lbl.setAttribute("text-anchor", "end");
    lbl.textContent = fmt(v);
    g.appendChild(lbl);
  }

  // Zero line (if y-range crosses zero)
  if (yMin < 0 && yMax > 0) {
    const z = yScale(0);
    const zero = document.createElementNS(ns, "line");
    zero.setAttribute("x1", m.plotX0); zero.setAttribute("x2", m.plotX0 + m.plotW);
    zero.setAttribute("y1", z); zero.setAttribute("y2", z);
    zero.setAttribute("stroke", "#9ca3af"); zero.setAttribute("stroke-width", 1);
    g.appendChild(zero);
  }

  // Step boundaries (dotted vertical lines + labels at bottom)
  const showLabels = (xMax - xMin) / m.plotW < 0.8;  // density-gated label rendering
  for (const step of DATA.steps) {
    if (step.t0 < xMin - 0.1 || step.t0 > xMax + 0.1) continue;
    const x = xScale(step.t0);
    const line = document.createElementNS(ns, "line");
    line.setAttribute("class", step.i === selectedStepIdx ? "selected-bound" : "step-bound");
    line.setAttribute("x1", x); line.setAttribute("x2", x);
    line.setAttribute("y1", m.plotY0); line.setAttribute("y2", m.plotY0 + m.plotH);
    g.appendChild(line);
    if (showLabels) {
      const lbl = document.createElementNS(ns, "text");
      lbl.setAttribute("class", "step-label");
      lbl.setAttribute("x", x + 2);
      lbl.setAttribute("y", m.plotY0 + m.plotH + 12);
      lbl.setAttribute("transform", `rotate(0, ${x + 2}, ${m.plotY0 + m.plotH + 12})`);
      lbl.textContent = step.label;
      g.appendChild(lbl);
    }
  }
  // Final boundary
  {
    const lastT = DATA.total_time;
    if (lastT >= xMin && lastT <= xMax) {
      const x = xScale(lastT);
      const line = document.createElementNS(ns, "line");
      line.setAttribute("class", "step-bound");
      line.setAttribute("x1", x); line.setAttribute("x2", x);
      line.setAttribute("y1", m.plotY0); line.setAttribute("y2", m.plotY0 + m.plotH);
      g.appendChild(line);
    }
  }

  // Plot area frame
  const frame = document.createElementNS(ns, "rect");
  frame.setAttribute("x", m.plotX0); frame.setAttribute("y", m.plotY0);
  frame.setAttribute("width", m.plotW); frame.setAttribute("height", m.plotH);
  frame.setAttribute("fill", "none"); frame.setAttribute("class", "axis-line");
  g.appendChild(frame);

  // X-axis ticks (minute marks if range > 60s, else 10s)
  const xRange = xMax - xMin;
  const tickStep = xRange > 600 ? 120 : xRange > 120 ? 60 : xRange > 30 ? 10 : 5;
  for (let t = Math.ceil(xMin / tickStep) * tickStep; t <= xMax; t += tickStep) {
    const x = xScale(t);
    const tk = document.createElementNS(ns, "line");
    tk.setAttribute("x1", x); tk.setAttribute("x2", x);
    tk.setAttribute("y1", m.plotY0 + m.plotH); tk.setAttribute("y2", m.plotY0 + m.plotH + 3);
    tk.setAttribute("class", "axis-line");
    g.appendChild(tk);
    const lbl = document.createElementNS(ns, "text");
    lbl.setAttribute("class", "axis-text");
    lbl.setAttribute("x", x); lbl.setAttribute("y", m.plotY0 + m.plotH + 14);
    lbl.setAttribute("text-anchor", "middle");
    lbl.textContent = fmtAxisTime(t);
    g.appendChild(lbl);
  }

  // Item lines.
  const linesGroup = document.createElementNS(ns, "g");
  linesGroup.setAttribute("clip-path", `url(#${clipId})`);
  for (const item of visible) {
    if (!inChart(item, spec.id)) continue;
    // Overlay series (flatten diff): a faint step function drawn first so
    // the solid primary line paints on top of it.
    if (spec.overlayKey) {
      let od = "", st = false;
      for (const step of DATA.steps) {
        const ov = step.rates[item]?.[spec.overlayKey];
        if (ov === null || ov === undefined) continue;
        od += (st ? "L" : "M") + xScale(step.t0).toFixed(2) + "," + yScale(ov).toFixed(2) + " ";
        od += "L" + xScale(step.t1).toFixed(2) + "," + yScale(ov).toFixed(2) + " ";
        st = true;
      }
      if (od) {
        const op = document.createElementNS(ns, "path");
        op.setAttribute("class", "item-line");
        op.setAttribute("d", od);
        op.setAttribute("stroke", DATA.colors[item]);
        op.setAttribute("stroke-width", "1");
        op.setAttribute("opacity", "0.30");
        linesGroup.appendChild(op);
      }
    }
    const pts = [];
    if (spec.key === "count") {
      // Linear-connect through (t0, count_start), (t1, count_end) per step.
      for (const step of DATA.steps) {
        const cs = step.rates[item]?.count_start ?? null;
        const ce = step.rates[item]?.count_end ?? null;
        if (cs === null || ce === null) continue;
        pts.push([step.t0, cs]);
        pts.push([step.t1, ce]);
      }
    } else {
      // Step function: flat through each step at its rate value.
      for (const step of DATA.steps) {
        let v;
        if (spec.key === "prod") v = step.rates[item]?.prod ?? null;
        else v = (step.rates[item]?.prod ?? 0) - (step.rates[item]?.cons ?? 0);
        if (v === null) continue;
        pts.push([step.t0, v]);
        pts.push([step.t1, v]);
      }
    }
    if (pts.length === 0) continue;
    let d = "";
    for (let i = 0; i < pts.length; i++) {
      const [t, v] = pts[i];
      d += (i === 0 ? "M" : "L") + xScale(t).toFixed(2) + "," + yScale(v).toFixed(2) + " ";
    }
    const path = document.createElementNS(ns, "path");
    path.setAttribute("class", "item-line");
    path.setAttribute("d", d);
    path.setAttribute("stroke", DATA.colors[item]);
    if (spec.overlayKey) path.setAttribute("stroke-width", "2");
    linesGroup.appendChild(path);
  }
  g.appendChild(linesGroup);

  svg.appendChild(g);
}

function renderAllCharts() {
  for (const spec of CHARTS) renderChart(spec);
  const fullSpan = DATA.total_time - DATA.initial_time_s;
  document.getElementById("zoom-info").textContent =
    `${fmtTime(xMin)} → ${fmtTime(xMax)} (zoom ${((fullSpan / (xMax - xMin)) * 100).toFixed(0)}%)`;
}

// Reflect the selected step in the left tech/step sidebar, and scroll it
// into view. Called on every selection change (chart click or nav click)
// so highlighting stays in sync in both directions.
function highlightNav() {
  const rows = document.querySelectorAll("#tech-list .tech-row");
  rows.forEach(r => r.classList.toggle(
    "selected", Number(r.dataset.step) === selectedStepIdx));
  if (selectedStepIdx !== null) {
    const sel = document.querySelector(
      `#tech-list .tech-row[data-step="${selectedStepIdx}"]`);
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }
}

// --- bottom panel: unmet-input table (flatten diff view) ---
// Replaces the per-step detail table when DATA.view === "flatten". Lists every
// (step, consuming recipe, input item) where the flattened plan's running-total
// production has fallen behind the raw requirement (buffer-aware). All
// data-derived text is esc()-escaped; the only un-escaped interpolations are
// server-generated color strings (DATA.colors / the grey fallback).
function renderDeficits(t, title) {
  const defs = DATA.deficits || [];
  title.textContent = `Unmet inputs — ${defs.length} lines `
    + `(running-total produced < required, buffer-aware · method ${esc(DATA.flatten_method)})`;
  const head = document.createElement("thead");
  head.innerHTML = "<tr><th>time</th>"
    + "<th style='text-align:left'>step</th>"
    + "<th style='text-align:left'>consuming recipe</th>"
    + "<th style='text-align:left'>input item</th>"
    + "<th>short (units)</th><th>short (time)</th>"
    + "<th>made (total)</th><th>required (total)</th></tr>";
  t.appendChild(head);
  const body = document.createElement("tbody");
  if (defs.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" style="text-align:center;color:#10b981;padding:8px">`
      + `No unmet inputs — running-total production meets the raw requirement everywhere.</td>`;
    body.appendChild(tr);
  }
  for (const d of defs) {
    const tr = document.createElement("tr");
    if (d.step === selectedStepIdx) tr.style.background = "#dbeafe";
    const rsw = DATA.colors[d.recipe] || "#9ca3af";
    const isw = DATA.colors[d.input] || "#9ca3af";
    tr.innerHTML = `<td>${fmtTime(d.time)}</td>`
      + `<td style="text-align:left">${esc(d.step)} ${esc(d.label)}</td>`
      + `<td style="text-align:left"><span class="swatch" style="background:${rsw}"></span>${esc(d.recipe)}</td>`
      + `<td style="text-align:left"><span class="swatch" style="background:${isw}"></span>${esc(d.input)}</td>`
      + `<td>${fmt(d.short)}</td>`
      + `<td>${d.short_time == null ? "—" : fmtTime(d.short_time)}</td>`
      + `<td>${fmt(d.made)}</td><td>${fmt(d.required)}</td>`;
    body.appendChild(tr);
  }
  t.appendChild(body);
}

// --- details table ---
function renderDetails() {
  highlightNav();
  const t = document.getElementById("details-table");
  const title = document.getElementById("details-title");
  t.innerHTML = "";
  if (DATA.view === "flatten") { renderDeficits(t, title); return; }
  if (selectedStepIdx === null) {
    title.textContent = "Click a chart to select a step";
    return;
  }
  const step = DATA.steps[selectedStepIdx];
  title.textContent = `Step ${step.i}: ${step.label}   |   ${fmtTime(step.t0)} → ${fmtTime(step.t1)}   |   duration ${step.duration.toFixed(2)}s`;
  const head = document.createElement("thead");
  head.innerHTML = "<tr><th>item</th><th>prod /s</th><th>cons /s</th><th>net /s</th><th>count start</th><th>count end</th><th>Δ count</th></tr>";
  t.appendChild(head);
  const body = document.createElement("tbody");
  // Sorted by |net|*duration descending so the most-active items rise.
  const itemsHere = [...visible].filter(it => step.rates[it] !== undefined);
  itemsHere.sort((a, b) => {
    const na = (step.rates[a].prod - step.rates[a].cons) * step.duration;
    const nb = (step.rates[b].prod - step.rates[b].cons) * step.duration;
    return Math.abs(nb) - Math.abs(na);
  });
  for (const it of itemsHere) {
    const r = step.rates[it];
    const net = r.prod - r.cons;
    const dc = r.count_end - r.count_start;
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="item-cell" data-item="${esc(it)}" title="click for production-facility breakdown"><span class="swatch" style="background:${DATA.colors[it]}"></span>${esc(it)}</td>
      <td>${fmt(r.prod)}</td><td>${fmt(r.cons)}</td><td>${fmt(net)}</td>
      <td>${fmt(r.count_start)}</td><td>${fmt(r.count_end)}</td><td>${fmt(dc)}</td>`;
    body.appendChild(tr);
  }
  t.appendChild(body);
}

// --- top-bar hand-crafting panel: nested step → recipe list ---
function renderHandcraft() {
  const body = document.getElementById("handcraft-body");
  let html = "";
  for (const step of DATA.steps) {
    const hc = step.handcraft || [];
    html += `<div class="hp-step"><span class="t">${fmtTime(step.t0)}</span>`
      + `<span>${esc(step.label)}</span></div>`;
    if (hc.length === 0) {
      html += `<div class="hp-none">— none —</div>`;
      continue;
    }
    for (const h of hc) {
      html += `<div class="hp-recipe"><span>${esc(h.recipe)}</span>`
        + `<span class="n">${fmt(h.count)}</span></div>`;
    }
  }
  body.innerHTML = html;
}

function toggleHandcraft(force) {
  const panel = document.getElementById("handcraft-panel");
  const btn = document.getElementById("handcraft-btn");
  const show = (force !== undefined) ? force : (panel.style.display === "none");
  if (show) renderHandcraft();
  panel.style.display = show ? "flex" : "none";
  btn.classList.toggle("active", show);
}

// --- inline cell popup: production-facility breakdown for one item ---
function closeCellPopup() {
  document.getElementById("cell-popup").style.display = "none";
}

function showCellPopup(item, ev) {
  if (selectedStepIdx === null) return;
  const step = DATA.steps[selectedStepIdx];
  const detail = (step.prod_detail && step.prod_detail[item]) || [];
  const pop = document.getElementById("cell-popup");
  const sw = DATA.colors[item] || "#9ca3af";
  let html = `<div class="cp-head"><span class="swatch" style="background:${sw}"></span>`
    + `<span class="cp-title">${esc(item)}</span><span class="cp-x" title="close">×</span></div>`
    + `<div class="cp-sub">step ${step.i}: ${esc(step.label)} — produced by</div>`;
  if (detail.length === 0) {
    html += DATA.model_loaded
      ? `<div class="cp-empty">not produced this step</div>`
      : `<div class="cp-empty">facility data unavailable (game model not loaded)</div>`;
  } else {
    html += `<table class="cp-table"><thead><tr>`
      + `<th>recipe</th><th>building</th><th>#facilities</th><th>rate /s</th></tr></thead><tbody>`;
    let totF = 0, totR = 0; let anyF = false;
    for (const d of detail) {
      const f = d.facilities;
      if (f != null) { totF += f; anyF = true; }
      totR += (d.item_rate || 0);
      const fcell = (f == null) ? "—" : f.toFixed(3);
      html += `<tr><td>${esc(d.recipe)}</td><td>${esc(d.building)}</td>`
        + `<td>${fcell}</td><td>${fmt(d.item_rate)}</td></tr>`;
    }
    if (detail.length > 1) {
      html += `<tr class="cp-total"><td>total</td><td></td>`
        + `<td>${anyF ? totF.toFixed(3) : "—"}</td><td>${fmt(totR)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }
  pop.innerHTML = html;
  pop.style.display = "block";
  // Position near the click, clamped to the viewport.
  const pad = 12;
  const rect = pop.getBoundingClientRect();
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth) x = Math.max(4, window.innerWidth - rect.width - pad);
  if (y + rect.height > window.innerHeight) y = Math.max(4, window.innerHeight - rect.height - pad);
  pop.style.left = x + "px";
  pop.style.top = y + "px";
  const x_btn = pop.querySelector(".cp-x");
  if (x_btn) x_btn.addEventListener("click", closeCellPopup);
}

// --- interactions ---
function setupZoomPan(svgEl) {
  let panning = false;
  let panStartX, xMinStart, xMaxStart;
  svgEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = svgEl.getBoundingClientRect();
    const xPx = e.clientX - r.left;
    const m = chartMetrics(svgEl);
    if (xPx < m.plotX0 || xPx > m.plotX0 + m.plotW) return;
    const tAtCursor = xMin + ((xPx - m.plotX0) / m.plotW) * (xMax - xMin);
    const factor = Math.pow(1.2, -Math.sign(e.deltaY));
    const span = (xMax - xMin) / factor;
    const minSpan = 0.5;
    if (span < minSpan) return;
    xMin = Math.max(DATA.initial_time_s, tAtCursor - ((tAtCursor - xMin) / (xMax - xMin)) * span);
    xMax = Math.min(DATA.total_time, xMin + span);
    if (xMax === DATA.total_time) xMin = Math.max(DATA.initial_time_s, xMax - span);
    renderAllCharts();
  }, { passive: false });
  svgEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    panning = true;
    panStartX = e.clientX;
    xMinStart = xMin; xMaxStart = xMax;
    svgEl.style.cursor = "grabbing";
  });
  window.addEventListener("mousemove", (e) => {
    if (!panning) return;
    const r = svgEl.getBoundingClientRect();
    const m = chartMetrics(svgEl);
    const dxPx = e.clientX - panStartX;
    const span = xMaxStart - xMinStart;
    const dx = -(dxPx / m.plotW) * span;
    let newMin = xMinStart + dx;
    let newMax = xMaxStart + dx;
    if (newMin < DATA.initial_time_s) { newMax += (DATA.initial_time_s - newMin); newMin = DATA.initial_time_s; }
    if (newMax > DATA.total_time) { newMin -= (newMax - DATA.total_time); newMax = DATA.total_time; }
    xMin = newMin; xMax = newMax;
    renderAllCharts();
  });
  window.addEventListener("mouseup", () => {
    if (panning) { panning = false; svgEl.style.cursor = "crosshair"; }
  });

  svgEl.addEventListener("click", (e) => {
    if (Math.abs(e.clientX - (panStartX ?? -1e9)) > 4) return;  // dragged
    const r = svgEl.getBoundingClientRect();
    const xPx = e.clientX - r.left;
    const m = chartMetrics(svgEl);
    if (xPx < m.plotX0 || xPx > m.plotX0 + m.plotW) return;
    const tClick = xMin + ((xPx - m.plotX0) / m.plotW) * (xMax - xMin);
    // Find step containing tClick.
    let found = null;
    for (const step of DATA.steps) {
      if (tClick >= step.t0 && tClick < step.t1) { found = step.i; break; }
    }
    if (found === null) {
      // Last step edge
      if (tClick >= DATA.total_time) found = DATA.steps[DATA.steps.length - 1].i;
    }
    if (found !== null) {
      selectedStepIdx = found;
      renderAllCharts();
      renderDetails();
    }
  });

  // Tooltip on hover.
  svgEl.addEventListener("mousemove", (e) => {
    if (panning) return;
    const r = svgEl.getBoundingClientRect();
    const xPx = e.clientX - r.left;
    const m = chartMetrics(svgEl);
    const tt = document.getElementById("tooltip");
    if (xPx < m.plotX0 || xPx > m.plotX0 + m.plotW) {
      tt.style.display = "none"; return;
    }
    const tCursor = xMin + ((xPx - m.plotX0) / m.plotW) * (xMax - xMin);
    let step = null;
    for (const s of DATA.steps) if (tCursor >= s.t0 && tCursor < s.t1) { step = s; break; }
    if (!step) { tt.style.display = "none"; return; }
    const spec = CHARTS.find(c => c.id === svgEl.id);
    const items = [...visible].filter(it => step.rates[it] !== undefined);
    items.sort((a, b) => {
      const va = spec.key === "count" ? step.rates[a].count_end :
                 spec.key === "prod"  ? step.rates[a].prod :
                                        step.rates[a].prod - step.rates[a].cons;
      const vb = spec.key === "count" ? step.rates[b].count_end :
                 spec.key === "prod"  ? step.rates[b].prod :
                                        step.rates[b].prod - step.rates[b].cons;
      return Math.abs(vb) - Math.abs(va);
    });
    let html = `<b>step ${step.i}: ${esc(step.label)}</b> @ ${fmtTime(tCursor)}<br>`;
    for (const it of items.slice(0, 6)) {
      const r = step.rates[it];
      let v = spec.key === "count" ? r.count_end : spec.key === "prod" ? r.prod : (r.prod - r.cons);
      html += `<span style="display:inline-block;width:8px;height:8px;background:${DATA.colors[it]};margin-right:4px"></span>${esc(it)}: ${fmt(v)}<br>`;
    }
    tt.innerHTML = html;
    tt.style.display = "block";
    tt.style.left = (e.clientX + 12) + "px";
    tt.style.top = (e.clientY + 12) + "px";
  });
  svgEl.addEventListener("mouseleave", () => {
    document.getElementById("tooltip").style.display = "none";
  });
}

// --- bootstrap ---
window.addEventListener("DOMContentLoaded", () => {
  buildLegend();
  buildNav();
  for (const spec of CHARTS) {
    const svg = document.getElementById(spec.id);
    setupZoomPan(svg);
  }
  // Inline production-facility popup: click an item cell in the step table.
  document.getElementById("details-table").addEventListener("click", (e) => {
    const cell = e.target.closest(".item-cell");
    if (!cell) return;
    showCellPopup(cell.dataset.item, e);
  });
  // Dismiss the popup on outside-click or Escape.
  document.addEventListener("click", (e) => {
    const pop = document.getElementById("cell-popup");
    if (pop.style.display !== "block") return;
    if (pop.contains(e.target) || e.target.closest(".item-cell")) return;
    closeCellPopup();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCellPopup(); });

  // Hand-crafting panel toggle (top bar).
  document.getElementById("handcraft-btn").addEventListener("click", () => toggleHandcraft());
  document.getElementById("handcraft-x").addEventListener("click", () => toggleHandcraft(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleHandcraft(false); });

  document.getElementById("legend-all").addEventListener("click", () => setAllVisibility(() => true));
  document.getElementById("legend-none").addEventListener("click", () => setAllVisibility(() => false));
  document.getElementById("legend-top10").addEventListener("click", () =>
    setAllVisibility(item => DATA.visible_default.includes(item)));
  // Render initial.
  renderAllCharts();
  // The flatten view's bottom panel (unmet inputs) is global, not
  // step-scoped, so populate it on load rather than waiting for a click.
  if (DATA.view === "flatten") renderDetails();
  // Re-render on resize.
  window.addEventListener("resize", () => { renderAllCharts(); });
});
</script>
</body>
</html>
"""


# The default 3-panel timeline. Each entry drives BOTH a chart pane (HTML,
# via `pane_title` + `id`) and a JS chart spec (`id`/`key`/`label`/`step_fn`),
# so a caller selects panels by passing a different list — no template editing.
# A later flatten-viz composes render_html() with its own 1-panel spec.
DEFAULT_CHARTS = [
    {
        "id": "chart-prod",
        "key": "prod",
        "label": "production rate",
        "step_fn": True,
        "pane_title": "Raw production rate (items/s, or MW for Power)",
    },
    {
        "id": "chart-net",
        "key": "net",
        "label": "net rate",
        "step_fn": True,
        "pane_title": "Net production rate (production − consumption)",
    },
    {
        "id": "chart-count",
        "key": "count",
        "label": "surplus count",
        "step_fn": False,
        "pane_title": "Surplus count over time (stockpile)",
    },
]


def _script_safe(json_text: str) -> str:
    """Make a JSON string safe to embed in an inline <script>: ``</`` → ``<\\/``
    so a value containing ``</script>`` can't terminate the element early, and
    the U+2028/U+2029 line separators (illegal in pre-ES2019 JS string literals,
    and the JSON is consumed as a JS expression, not via JSON.parse) → escapes.
    All still valid JSON, inert to the JS parser."""
    return (
        json_text.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _chart_panes_html(charts: list[dict]) -> str:
    return "\n".join(
        '      <div class="chart-pane">\n'
        f'        <div class="chart-title">{escape(c["pane_title"])}</div>\n'
        '        <div class="chart-svg-wrap">'
        f'<svg class="chart-svg" id="{escape(c["id"])}"></svg></div>\n'
        "      </div>"
        for c in charts
    )


def default_meta_parts(dataset: dict) -> list[str]:
    """The standard meta line (scenario/mode/l1/solver/total) for a dataset."""
    solver = dataset.get("solver") or {}
    parts = [
        f"scenario={dataset['scenario']}",
        f"mode={dataset['mode']}",
        f"l1={dataset['l1_method']}",
    ]
    if solver:
        parts.append(f"status={solver.get('status', '?')}")
        obj = solver.get("objective_s")
        if obj is not None:
            parts.append(f"obj={obj:.1f}s")
        gap = solver.get("gap")
        if gap is not None:
            parts.append(f"gap={gap * 100:.1f}%")
    parts.append(f"total={dataset['total_time']:.1f}s")
    if dataset.get("pseudo_recipes_version"):
        parts.append(f"pseudo_recipes_v={dataset['pseudo_recipes_version']}")
    return parts


def render_html(
    dataset: dict,
    *,
    charts: list[dict] | None = None,
    heading: str = "L2 timeline",
    title: str | None = None,
    meta: str | None = None,
) -> str:
    """Render the interactive timeline HTML for ``dataset``.

    ``charts`` selects the stacked panels (defaults to the 3-panel timeline);
    ``heading``/``title``/``meta`` override the page chrome. Keeping these
    parameters is what lets a later flatten-viz reuse this template with a
    single panel instead of string-rewriting it.
    """
    charts = charts if charts is not None else DEFAULT_CHARTS
    if title is None:
        title = f"{dataset['scenario']} ({dataset['mode']})"
    if meta is None:
        meta = " · ".join(default_meta_parts(dataset))
    charts_json = json.dumps(
        [
            {
                "id": c["id"],
                "key": c["key"],
                "label": c["label"],
                "stepFn": c["step_fn"],
                # Optional second series drawn faint under the primary line
                # (the flatten diff overlays the original rate).
                **({"overlayKey": c["overlay_key"]} if c.get("overlay_key") else {}),
            }
            for c in charts
        ]
    )
    data_json = json.dumps(dataset, separators=(",", ":"))
    repl = {
        "__HEADING__": escape(heading),
        "__CHART_PANES__": _chart_panes_html(charts),
        "__CHARTS_JSON__": _script_safe(charts_json),
        "__TITLE__": escape(title),
        "__META__": escape(meta),
        "__DATA_JSON__": _script_safe(data_json),
        "__JS_HELPERS__": _JS_SHARED_HELPERS,
    }
    # Single non-rescanning pass: a replacement's output is never scanned for
    # another placeholder, so untrusted title/meta text can't reintroduce a
    # later token like __DATA_JSON__ (chained .replace() would).
    return _PLACEHOLDER_RE.sub(lambda m: repl[m.group(0)], HTML_TEMPLATE)


# -- Flatten diff view ---------------------------------------------------
#
# The flatten view reuses the timeline template via render_html() with a
# single-panel spec (an overlay chart) and a `flatten` dataset that swaps the
# step-detail table for the unmet-input table. It is a PURE renderer of a
# `rates-post.yaml` (the flattened series + the persisted `post:` diagnostics)
# plus the source `rates.yaml` (the original series for the faint overlay) —
# no game model, no re-flattening. Detected by the `post:` block in the file.

# Single overlay panel: solid = flattened (the post file's prod), faint =
# original (injected as `orig` from the source solve). `pane_title` is
# completed with the method by render_flatten_html().
FLATTEN_CHARTS = [
    {
        "id": "chart-prod",
        "key": "prod",
        "label": "flattened rate",
        "step_fn": True,
        "overlay_key": "orig",
        "pane_title": "Production rate — faint = original, solid = flattened",
    },
]


def build_flatten_dataset(
    post_l2: dict, source_l2: dict | None = None, *, data_dir: Path | None = None
) -> dict:
    """Build the dataset for the flatten diff view from a ``rates-post.yaml``
    dict (``post_l2``) and, optionally, its source ``rates.yaml`` dict
    (``source_l2``) for the faint original-rate overlay.

    The post file's per-step production rate is already the *flattened* series
    (it becomes the solid line); the source's rate is injected per (step, item)
    as ``orig`` (the faint overlay). The revisit counts and unmet-input lines
    come straight from the persisted ``post:`` block — no recomputation.
    """
    ds = build_dataset(post_l2, data_dir=data_dir)
    post = post_l2.get("post") or {}

    has_orig = False
    if isinstance(source_l2, dict):
        src_steps = source_l2.get("steps", []) or []
        for i, step_rec in enumerate(ds["steps"]):
            if i >= len(src_steps):
                break
            srates = step_rec["rates"]
            for it in src_steps[i].get("items", []) or []:
                name = it.get("name")
                if name in srates:
                    srates[name]["orig"] = float(it.get("production_rate_per_s") or 0.0)
                    has_orig = True

    per_item = post.get("per_item") or {}
    ds["view"] = "flatten"
    ds["flatten_method"] = post.get("method", "?")
    ds["flatten_summary"] = post.get("summary") or {}
    ds["revisits"] = {
        k: v.get("revisits")
        for k, v in per_item.items()
        if isinstance(v, dict) and v.get("revisits") is not None
    }
    ds["deficits"] = post.get("deficits") or []
    ds["has_orig"] = has_orig
    return ds


def _flatten_meta_parts(dataset: dict, method: str) -> list[str]:
    summ = dataset.get("flatten_summary") or {}
    parts = [
        f"scenario={dataset['scenario']}",
        f"mode={dataset['mode']}",
        f"method={method}",
    ]
    if summ:
        parts.append(
            f"revisits={summ.get('revisits')} (was {summ.get('orig_segments')}, "
            f"saved {summ.get('revisits_saved')})"
        )
        parts.append(f"self-stockouts={summ.get('self_stockouts')}")
    parts.append(f"deficit-lines={len(dataset.get('deficits') or [])}")
    parts.append(f"total={dataset['total_time']:.1f}s")
    return parts


def render_flatten_html(dataset: dict, *, method: str | None = None) -> str:
    """Render the flatten diff HTML for a dataset from ``build_flatten_dataset``."""
    method = method or dataset.get("flatten_method", "?")
    charts = [
        dict(
            FLATTEN_CHARTS[0],
            pane_title=(
                f"Production rate — faint = original, solid = flattened ({method})"
            ),
        )
    ]
    return render_html(
        dataset,
        charts=charts,
        heading="L2 rate flattening",
        title=f"{dataset['scenario']} flatten ({method})",
        meta=" · ".join(_flatten_meta_parts(dataset, method)),
    )


# -- CLI -----------------------------------------------------------------


def _fmt_clock(t: float) -> str:
    """m:ss.s in-game time, matching the timeline view's left-column times."""
    m = int(t // 60)
    s = t - m * 60
    return f"{m}:{s:04.1f}"


_HEATMAP_CSS = """
<style>
  html,body{margin:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:12px;color:#222;}
  body{display:flex;flex-direction:column;height:100vh;}
  header{padding:6px 12px;background:#1f2937;color:#fff;display:flex;gap:12px;align-items:baseline;flex:0 0 auto;}
  header h1{font-size:14px;margin:0;font-weight:600;}
  header .sub{color:#cbd5e1;font-size:12px;}
  .key{padding:4px 12px;font-size:11px;color:#6b7280;display:flex;gap:16px;align-items:center;border-bottom:1px solid #e5e7eb;flex:0 0 auto;}
  .key b{display:inline-block;width:11px;height:11px;vertical-align:middle;margin-right:4px;border:1px solid #cbd5e1;}
  .wrap{display:flex;flex:1 1 auto;min-height:0;}
  .legend{flex:0 0 250px;overflow:auto;height:100%;border-right:2px solid #e5e7eb;padding:4px 0;}
  .lg-row{display:flex;gap:6px;align-items:baseline;padding:2px 8px;font-size:11px;white-space:nowrap;}
  .lg-row:hover{background:#dbeafe;}
  .lg-idx{flex:0 0 22px;text-align:right;color:#6b7280;font-variant-numeric:tabular-nums;}
  .lg-tech{flex:1;overflow:hidden;text-overflow:ellipsis;}
  .lg-time{color:#9ca3af;font-variant-numeric:tabular-nums;}
  .grid{flex:1 1 auto;overflow:auto;padding:4px;}
  table{border-collapse:collapse;}
  th.row-h{position:sticky;left:0;background:#fff;text-align:right;padding:0 6px;font-weight:500;font-size:11px;white-space:nowrap;z-index:2;}
  th.col-h{position:sticky;top:0;background:#fff;font-size:9px;color:#6b7280;font-weight:500;height:18px;width:14px;font-variant-numeric:tabular-nums;z-index:1;}
  td{width:14px;height:14px;border:1px solid #f1f5f9;padding:0;}
  td.c-sat{background:#111827;}
  td.c-slack{background:#e5e7eb;}
  td.c-absent{background:#fff;}
  .col-hi{outline:2px solid #2563eb;outline-offset:-2px;}
  .threshold-bar{flex:0 0 auto;display:flex;align-items:center;gap:12px;padding:8px 12px;border-top:2px solid #e5e7eb;background:#f9fafb;}
  .threshold-bar label{font-size:12px;color:#374151;white-space:nowrap;font-weight:500;}
  .threshold-bar select{font-size:12px;padding:2px 4px;}
  .threshold-bar .sep{width:1px;height:18px;background:#d1d5db;}
  .threshold-bar input[type=range]{flex:1 1 auto;cursor:pointer;}
  .threshold-bar .tval{font-variant-numeric:tabular-nums;font-weight:600;min-width:52px;text-align:right;}
  .threshold-bar .tcount{font-size:11px;color:#6b7280;white-space:nowrap;min-width:150px;text-align:right;}
</style>
"""


def build_heatmap_html(l2: dict) -> str:
    """Binary capacity-saturation heatmap (handoff Theme 2 utilization
    filter). Rows = capacity-constrained buildings; columns = steps in
    tech-tree order. A cell is BLACK when that building is *saturated*
    (utilization ≈ 1) at that step — i.e. it is on the critical surface and
    relevant to t_FINAL. Light grey = present but slack; blank = not active
    that step. Rows are ordered by first saturation so the bottleneck
    "baton-pass" reads as a staircase, and a left legend maps each column
    index to its tech + in-game timestamp (the same techs/times as the
    timeline view). The binary fill is deliberately the v0 — a colorscale
    (utilization, or the duration/count magnitude proxy) drops into the same
    cell later; for now black is the honest answer to "is this a bottleneck."
    """
    steps = l2.get("steps", []) or []
    clock = float(l2.get("initial_time_s", 0.0) or 0.0)
    cols: list[tuple[int, str, float]] = []
    cells: dict[str, dict[int, dict]] = defaultdict(dict)
    for i, s in enumerate(steps):
        dur = float(s.get("duration_s", 0.0) or 0.0)
        cols.append((i, s.get("label") or f"step-{i}", clock))
        clock += dur
        for c in s.get("capacity", []) or []:
            b = c.get("building")
            if not b:
                continue
            cells[b][i] = {
                "sat": bool(c.get("saturated")),
                "util": c.get("utilization"),
                "rs": c.get("recipe_seconds_used"),
                "cap": c.get("capacity_seconds"),
            }
    n = len(cols)

    def first_sat(b: str) -> int:
        for i in range(n):
            v = cells[b].get(i)
            if v and v["sat"]:
                return i
        return n + 1

    rows = sorted(
        cells,
        key=lambda b: (first_sat(b), -sum(1 for v in cells[b].values() if v["sat"]), b),
    )

    legend = "".join(
        f'<div class="lg-row" data-col="{i}">'
        f'<span class="lg-idx">{i}</span>'
        f'<span class="lg-tech">{escape(label)}</span>'
        f'<span class="lg-time">{_fmt_clock(t0)}</span></div>'
        for i, label, t0 in cols
    )
    head = "".join(
        f'<th class="col-h" data-col="{i}" title="{escape(label)} · {_fmt_clock(t0)}">{i}</th>'
        for i, label, t0 in cols
    )
    body_rows = []
    for b in rows:
        tds = []
        for i in range(n):
            v = cells[b].get(i)
            if v is None:
                tds.append(f'<td class="c-absent" data-col="{i}"></td>')
                continue
            util = v["util"]
            us = f"{util:.2f}" if util is not None else "—"
            du = f"{util:.4f}" if util is not None else ""
            tip = (
                f"{escape(b)} · col {i} · util={us} · "
                f"used {(v['rs'] or 0):.1f}s / cap {(v['cap'] or 0):.1f}s"
            )
            cls = "c-sat" if v["sat"] else "c-slack"
            # Server class = the solver's `saturated` bool (also the fallback when
            # JS can't recompute from data-util); the slider then drives c-sat by
            # utilization threshold for cells that have a number.
            tds.append(
                f'<td class="cell {cls}" data-col="{i}" '
                f'data-util="{du}" title="{tip}"></td>'
            )
        fs = first_sat(b)
        ts = sum(1 for v in cells[b].values() if v["sat"])
        body_rows.append(
            f'<tr data-name="{escape(b)}" data-firstsat="{fs}" data-totalsat="{ts}">'
            f'<th class="row-h" title="{escape(b)}">{escape(b)}</th>{"".join(tds)}</tr>'
        )

    obj = (l2.get("solver", {}) or {}).get("objective_s")
    seed = (l2.get("solver", {}) or {}).get("seed")
    sub = f"{len(rows)} buildings × {n} steps (tech order)"
    if obj is not None:
        sub += f" · t_FINAL {_fmt_clock(float(obj))}"
    if seed is not None:
        sub += f" · seed {escape(str(seed))}"

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>L2 capacity-saturation heatmap</title>" + _HEATMAP_CSS + "</head><body>"
        f"<header><h1>L2 capacity-saturation heatmap</h1>"
        f"<span class='sub'>{sub}</span></header>"
        "<div class='key'>"
        "<span><b style='background:#111827'></b>saturated — on the critical "
        "surface (relevant to t_FINAL)</span>"
        "<span><b style='background:#e5e7eb'></b>present but slack</span>"
        "<span><b style='background:#fff'></b>not active this step</span>"
        "<span>hover a tech (left) to highlight its column</span></div>"
        "<div class='wrap'>"
        f"<div class='legend'>{legend}</div>"
        "<div class='grid'><table>"
        f"<thead><tr><th class='row-h'></th>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div></div>"
        "<div class='threshold-bar'>"
        "<label>Rows</label>"
        "<select id='sort'>"
        "<option value='stair'>by first saturation</option>"
        "<option value='alpha'>alphabetical</option>"
        "</select>"
        "<span class='sep'></span>"
        "<label>Saturation threshold</label>"
        "<input type='range' id='thresh' min='95' max='100' step='0.1' value='98'>"
        "<span class='tval' id='thresh-val'>98.0%</span>"
        "<span class='tcount' id='thresh-count'></span></div>"
        "<script>"
        "document.querySelectorAll('.lg-row').forEach(function(r){"
        "var c=r.getAttribute('data-col');"
        "function hi(on){document.querySelectorAll('[data-col=\"'+c+'\"]')"
        ".forEach(function(e){e.classList.toggle('col-hi',on);});}"
        "r.addEventListener('mouseenter',function(){hi(true);});"
        "r.addEventListener('mouseleave',function(){hi(false);});});"
        "var slider=document.getElementById('thresh');"
        "var tval=document.getElementById('thresh-val');"
        "var tcount=document.getElementById('thresh-count');"
        "var cells=document.querySelectorAll('td.cell');"
        "function applyThreshold(){"
        "var t=parseFloat(slider.value)/100,nsat=0;"
        "cells.forEach(function(c){"
        "var u=parseFloat(c.getAttribute('data-util'));"
        # No utilization number → keep the server-computed saturated state
        # (c-sat) instead of forcing the cell to slack.
        "var sat=isNaN(u)?c.classList.contains('c-sat'):(u>=t);if(sat)nsat++;"
        "c.classList.toggle('c-sat',sat);c.classList.toggle('c-slack',!sat);});"
        "tval.textContent=parseFloat(slider.value).toFixed(1)+'%';"
        "tcount.textContent=nsat+' / '+cells.length+' cells saturated';}"
        "slider.addEventListener('input',applyThreshold);applyThreshold();"
        "var sortSel=document.getElementById('sort');"
        "var tbody=document.querySelector('tbody');"
        "function applySort(){"
        "var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr'));"
        "rows.sort(function(a,b){"
        "var na=a.getAttribute('data-name'),nb=b.getAttribute('data-name');"
        "if(sortSel.value==='alpha')return na.localeCompare(nb);"
        "var fa=+a.getAttribute('data-firstsat'),fb=+b.getAttribute('data-firstsat');"
        "if(fa!==fb)return fa-fb;"
        "var ta=+a.getAttribute('data-totalsat'),tb=+b.getAttribute('data-totalsat');"
        "if(ta!==tb)return tb-ta;return na.localeCompare(nb);});"
        "rows.forEach(function(r){tbody.appendChild(r);});}"
        "sortSel.addEventListener('change',applySort);"
        "</script></body></html>"
    )


# -- Ore-patch supply-curve view -----------------------------------------
#
# An interactive map of ore/oil patches (clickable to select which to commit
# miners to) against the solve's per-resource miner demand over time. Closes
# the patch-selection loop: choose patches by eye, "Export YAML", and feed that
# back into the next `rates solve` (`rates add-selection` / --patch-selection).
#
# This view is a PURE consumer of the solve output + the bound map artifact —
# NO game-model load. Patch capacity (tiles → drills) divides by the deployed
# footprint the solve recorded in its `spatial:` block, and the utilized-drill
# line reads `recipe_seconds_used / (base_speed · duration)` from the emitted
# `capacity` rows (weight-correct under trapezoidal — it does NOT divide by
# count_end). So the lines here match the caps the LP actually enforced, with
# no recomputation and no drift.

# Natural per-ore palette for the map (iron bluish, copper orange, coal dark …).
# This is deliberately NOT the timeline's per-item `color_for_item` hash: a
# geographic ore map reads far better with intuitive ore hues than with hashed
# ones, and these are the shared palette any future L3 map-style view should
# reuse for cross-view consistency.
_SC_RESOURCE_COLORS = {
    "iron-ore": "#b8d4e8",
    "copper-ore": "#e89868",
    "coal": "#555555",
    "stone": "#c4b48c",
    "uranium-ore": "#7fd874",
    "crude-oil": "#2a2a2a",
}
_SC_DRILL = "electric-mining-drill"
_SC_PLACEHOLDER_RE = re.compile(r"__(?:SC_(?:TITLE|VIEWBOX|DATA)|JS_HELPERS)__")


def build_supply_curve_dataset(l2: dict, map_probe: dict) -> dict | None:
    """Dataset for the ore-patch supply-curve view, or None if the map carries
    no patches to select.

    ``l2`` is a solved rates.yaml (demand + the ``spatial:`` block); ``map_probe``
    is the bound map artifact (patch / oil / water geometry). No model load.
    """
    patches_in = map_probe.get("patches") or []
    oil_clusters = map_probe.get("oil_clusters") or []
    if not patches_in and not oil_clusters:
        return None

    spatial = l2.get("spatial") or {}
    drill_info = (spatial.get("miners") or {}).get(_SC_DRILL) or {}
    fp = float(drill_info.get("footprint") or 0.0)
    drill_speed = float(drill_info.get("base_speed") or 0.0)

    t0_off = float(l2.get("initial_time_s", 0.0) or 0.0)
    steps = l2.get("steps") or []
    bounds: list[tuple[float, float, float]] = []
    t = t0_off
    for s in steps:
        dur = float(s.get("duration_s") or 0.0)
        bounds.append((t, t + dur, dur))
        t += dur

    # Resources to list: those actually mined, plus every patch resource (so an
    # unmined resource still shows its patches for selection).
    ores: set[str] = set()
    for s in steps:
        for e in s.get("mining_assignment") or []:
            if e.get("ore"):
                ores.add(str(e["ore"]))
    for p in patches_in:
        if p.get("resource"):
            ores.add(str(p["resource"]))

    series: dict[str, dict] = {}
    for ore in ores:
        st: list[dict] = []
        peak = 0.0
        for k, s in enumerate(steps):
            _t0, _t1, dur = bounds[k]
            built = sum(
                float(e.get("count_end") or 0.0)
                for e in (s.get("mining_assignment") or [])
                if e.get("ore") == ore
            )
            # Utilized drills (weight-correct): recipe-seconds / (speed·dur).
            rsec = sum(
                float(c.get("recipe_seconds_used") or 0.0)
                for c in (s.get("capacity") or [])
                if c.get("building") == f"{_SC_DRILL}@{ore}"
            )
            util = rsec / (drill_speed * dur) if drill_speed > 0 and dur > 0 else 0.0
            # Burner drill-equivalents on this ore (bootstrap context series).
            burner = sum(
                float(b.get("drills_equiv") or 0.0)
                for b in (s.get("burner_mining") or [])
                if b.get("ore") == ore
            )
            st.append(
                {
                    "t0": round(_t0, 2),
                    "t1": round(_t1, 2),
                    "built": round(built, 2),
                    "utilized": round(util, 2),
                    "burner": round(burner, 2),
                }
            )
            peak = max(peak, built)
        series[ore] = {"peak_demand_drills": round(peak, 2), "steps": st}

    # crude-oil is pumped: built = pumpjack count; capacity per cluster = spots.
    oil_steps: list[dict] = []
    oil_peak = 0.0
    for k, s in enumerate(steps):
        _t0, _t1, _dur = bounds[k]
        built = next(
            (
                float(it.get("count_end") or 0.0)
                for it in (s.get("items") or [])
                if it.get("name") == "pumpjack"
            ),
            0.0,
        )
        util = next(
            (
                float(c.get("utilization") or 0.0)
                for c in (s.get("capacity") or [])
                if c.get("building") == "pumpjack"
            ),
            1.0,
        )
        oil_steps.append(
            {
                "t0": round(_t0, 2),
                "t1": round(_t1, 2),
                "built": round(built, 2),
                "utilized": round(built * util, 2),
                "burner": 0.0,
            }
        )
        oil_peak = max(oil_peak, built)
    series["crude-oil"] = {
        "peak_demand_drills": round(oil_peak, 2),
        "steps": oil_steps,
        "unit": "pumpjacks",
    }

    # Patches (drilled ore) + oil clusters (pumped), with capacity in their unit.
    patches: list[dict] = []
    for i, p in enumerate(patches_in):
        tc = float(p.get("tile_count") or 0.0)
        bbox = max(
            (float(p.get("max_x", 0)) - float(p.get("min_x", 0)))
            * (float(p.get("max_y", 0)) - float(p.get("min_y", 0))),
            1.0,
        )
        res = str(p.get("resource", "?"))
        patches.append(
            {
                "id": i,
                "resource": res,
                "cx": float(p.get("centroid_x", 0)),
                "cy": float(p.get("centroid_y", 0)),
                "r": math.sqrt(tc / math.pi) if tc > 0 else 2.0,
                "distance": round(float(p.get("distance", 0)), 1),
                "tile_count": int(tc),
                "density": round(tc / bbox, 3),
                "capacity": round(tc / fp, 1) if fp > 0 else None,
                "unit": "drills",
                "is_oil": False,
                "color": _SC_RESOURCE_COLORS.get(res, "#888"),
            }
        )
    base = len(patches)
    for j, o in enumerate(oil_clusters):
        sc = int(o.get("spot_count", 0) or 0)
        patches.append(
            {
                "id": base + j,
                "resource": "crude-oil",
                "cx": float(o.get("centroid_x", 0)),
                "cy": float(o.get("centroid_y", 0)),
                "r": 4.0 + 2.0 * math.sqrt(max(sc, 1)),
                "distance": round(float(o.get("distance", 0)), 1),
                "tile_count": sc,
                "density": None,
                "capacity": float(sc),
                "unit": "pumpjacks",
                "is_oil": True,
                "spot_count": sc,
                "color": _SC_RESOURCE_COLORS["crude-oil"],
            }
        )

    # Resources ordered by peak demand (mined first), then name.
    res_order = sorted(
        {p["resource"] for p in patches},
        key=lambda r: (-series.get(r, {}).get("peak_demand_drills", 0.0), r),
    )

    # Default selection: nearest-first greedy to cover each resource's demand.
    # Only when patch capacities are known (fp > 0); without the spatial block
    # every capacity is None, so a greedy fill can never reach `need` and would
    # otherwise select *every* patch — leave the selection empty instead.
    by_res: dict[str, list[dict]] = defaultdict(list)
    for p in patches:
        by_res[p["resource"]].append(p)
    initial: list[int] = []
    if fp > 0:
        for r in res_order:
            need = series.get(r, {}).get("peak_demand_drills", 0.0)
            if need <= 0:
                continue
            cum = 0.0
            for p in sorted(by_res[r], key=lambda x: x["distance"]):
                if cum >= need:
                    break
                cum += p["capacity"] or 0.0
                initial.append(p["id"])

    oil_spots = [
        {"x": float(o.get("x", 0)), "y": float(o.get("y", 0))}
        for o in (map_probe.get("oil_spots") or [])
    ]
    water = [
        {
            "cx": float(w.get("centroid_x", 0)),
            "cy": float(w.get("centroid_y", 0)),
            "r": (
                math.sqrt(float(w.get("tile_count", 0)) / math.pi)
                if w.get("tile_count")
                else 2.0
            ),
            "tile_count": int(w.get("tile_count", 0) or 0),
            "distance": round(float(w.get("distance", 0)), 1),
        }
        for w in (map_probe.get("water_patches") or [])
    ]

    # Map rectangle = bbox of every drawn feature, padded.
    xs: list[float] = []
    ys: list[float] = []
    for p in patches:
        xs += [p["cx"] - p["r"], p["cx"] + p["r"]]
        ys += [p["cy"] - p["r"], p["cy"] + p["r"]]
    for w in water:
        xs += [w["cx"] - w["r"], w["cx"] + w["r"]]
        ys += [w["cy"] - w["r"], w["cy"] + w["r"]]
    if not xs:
        xs = ys = [-100.0, 100.0]
    pad = 15.0
    map_rect = {
        "x": min(xs) - pad,
        "y": min(ys) - pad,
        "w": max(xs) - min(xs) + 2 * pad,
        "h": max(ys) - min(ys) + 2 * pad,
    }

    return {
        "scenario": l2.get("scenario", "?"),
        "mode": l2.get("mode", "?"),
        "footprint": round(fp, 3) if fp > 0 else None,
        "has_footprint": fp > 0,
        "t0_offset": round(t0_off, 2),
        "resources": res_order,
        "patches": patches,
        "series": series,
        "oil_spots": oil_spots,
        "water": water,
        "map_rect": map_rect,
        "initial_selected": initial,
        "seed": map_probe.get("seed"),
    }


_SUPPLY_CURVE_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__SC_TITLE__</title>
<style>
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:12px; }
  #app { display:flex; height:100vh; }
  #left { flex:1; display:flex; flex-direction:column; min-width:0; }
  #right { width:330px; min-width:330px; border-left:1px solid #bbb; background:#f5f5f5;
           display:flex; flex-direction:column; }
  #toolbar { padding:6px 10px; border-bottom:1px solid #bbb; background:#ececec;
             display:flex; gap:14px; align-items:center; }
  #toolbar .meta { margin-left:auto; color:#555; font-size:11px; }
  #map-wrap { flex:1.4; position:relative; min-height:0; overflow:hidden; }
  #map { position:absolute; inset:0; width:100%; height:100%; background:#ffffff;
         user-select:none; display:block; }
  .map-bg { fill:#f4f1e8; }
  #chart-wrap { flex:1; border-top:1px solid #bbb; display:flex; flex-direction:column; min-height:0; }
  #chart-head { padding:5px 10px; background:#f0f0f0; display:flex; gap:12px; align-items:center; }
  #chart-svg { flex:1; width:100%; min-height:0; display:block; background:#fff; }
  .patch { cursor:pointer; stroke:#333; stroke-width:0.5; opacity:0.30; }
  .patch.sel { opacity:0.85; stroke:#1a1a1a; stroke-width:2; }
  .patch.oil { opacity:0.85; stroke:#88bb55; stroke-width:2; }
  .patch.oil.sel { stroke:#1a1a1a; }
  .patch-label { font-size:28px; fill:#ffffff; stroke:#000; stroke-width:2; paint-order:stroke fill;
                 font-weight:bold; pointer-events:none; }
  .origin-line { vector-effect:non-scaling-stroke; pointer-events:none; }
  .oil-spot { fill:#1a1a1a; opacity:0.7; pointer-events:none; }
  .water { fill:#1565c0; opacity:0.45; pointer-events:none; }
  .oil-label { font-size:24px; fill:#cfe; stroke:#000; stroke-width:2; paint-order:stroke fill;
               font-weight:bold; pointer-events:none; }
  #right h2 { margin:8px 12px 4px; font-size:13px; }
  #tbl-wrap { flex:1; overflow-y:auto; }
  table.grp { border-collapse:collapse; width:100%; font-size:11px; }
  table.grp th { background:#e3e3e3; padding:3px 6px; text-align:right; position:sticky; top:0; }
  table.grp th.l { text-align:left; }
  tr.group td { background:#dfe6ee; font-weight:bold; padding:4px 6px; cursor:pointer; border-top:1px solid #bbb; }
  tr.group td.ok { color:#0a7d2c; }
  tr.group td.short { color:#c0271a; }
  tr.patch td { padding:2px 6px; text-align:right; border-bottom:1px solid #eee; }
  tr.patch td.l { text-align:left; cursor:pointer; }
  tr.patch.sel td { background:#dceeff; }
  tr.patch:hover td { background:#eef4fb; }
  .num { font-variant-numeric:tabular-nums; }
  button { padding:3px 9px; font-size:11px; cursor:pointer; border:1px solid #999;
           background:#fafafa; border-radius:3px; }
  select { font-size:12px; padding:2px; }
</style></head>
<body>
<div id="app">
  <div id="left">
    <div id="toolbar">
      <strong>Ore supply curve</strong>
      <button id="reset-view">Reset view</button>
      <button id="export-yaml" title="Download the current selection as a patch-selection L2 input">Export YAML</button>
      <label><input type="checkbox" id="show-labels" checked> Patch labels</label>
      <label><input type="checkbox" id="show-origin" checked> Origin lines</label>
      <span class="meta"><span id="zoom-pct">zoom 100%</span> &middot; <span id="meta-text"></span></span>
    </div>
    <div id="map-wrap"><svg id="map" viewBox="__SC_VIEWBOX__"></svg></div>
    <div id="chart-wrap">
      <div id="chart-head">
        <span class="meta" id="chart-meta" style="color:#555;font-size:11px"></span>
        <span style="color:#444">&mdash; <span style="border-bottom:2px solid #888">solid</span> built &middot;
          <span style="border-bottom:2px dotted #888">faint</span> utilized &middot;
          <span style="border-bottom:2px dashed #b5651d">burner</span> equiv &middot;
          <span style="border-bottom:1px dashed #c0271a">&#9476;</span> cumulative patch capacity (by distance)</span>
        <label style="margin-left:auto">Resource: <select id="res-select"></select></label>
      </div>
      <svg id="chart-svg"></svg>
    </div>
  </div>
  <div id="right">
    <h2>Patches &mdash; click map or rows to select</h2>
    <div id="tbl-wrap"></div>
  </div>
</div>
<script>const DATA = __SC_DATA__;</script>
<script>
const NS="http://www.w3.org/2000/svg";
__JS_HELPERS__
const svg=document.getElementById("map");
const ivb=svg.viewBox.baseVal;
const initVB={x:ivb.x,y:ivb.y,w:ivb.width,h:ivb.height};
const patchById={}; for(const p of DATA.patches) patchById[p.id]=p;
const selected=new Set(DATA.initial_selected);
const collapsed=new Set();
let activeRes = DATA.resources.find(r => (DATA.series[r]||{}).peak_demand_drills>0) || DATA.resources[0];
const fpTxt = DATA.footprint==null ? "n/a (pre-spatial solve)" : (DATA.footprint+"t");

document.getElementById("meta-text").textContent =
  `${DATA.scenario} · ${DATA.mode} · seed ${DATA.seed} · drill fp ${fpTxt}`;

// ---------- map: in-map background rect, water, oil, patches ----------
const bg=document.createElementNS(NS,"g"); svg.appendChild(bg);
{ const r=DATA.map_rect, rect=document.createElementNS(NS,"rect");
  rect.setAttribute("x",r.x); rect.setAttribute("y",r.y);
  rect.setAttribute("width",r.w); rect.setAttribute("height",r.h);
  rect.classList.add("map-bg"); bg.appendChild(rect); }
for(const w of DATA.water){
  const c=document.createElementNS(NS,"circle");
  c.setAttribute("cx",w.cx); c.setAttribute("cy",w.cy); c.setAttribute("r",Math.max(w.r,2));
  c.classList.add("water");
  const ti=document.createElementNS(NS,"title"); ti.textContent=`water · ${w.tile_count}t · d=${w.distance}`;
  c.appendChild(ti); bg.appendChild(c);
}
for(const p of DATA.patches){
  const c=document.createElementNS(NS,"circle");
  c.setAttribute("cx",p.cx); c.setAttribute("cy",p.cy); c.setAttribute("r",Math.max(p.r,2));
  c.setAttribute("fill",p.color); c.classList.add("patch");
  if(p.is_oil) c.classList.add("oil");
  c.classList.toggle("sel",selected.has(p.id));
  c.dataset.pid=p.id;
  const cap=p.capacity==null?"?":p.capacity;
  const ti=document.createElementNS(NS,"title");
  ti.textContent = p.is_oil
    ? `#${p.id} crude-oil · ${p.spot_count} spots · cap ${cap} pumpjacks · d=${p.distance}`
    : `#${p.id} ${p.resource} · ${p.tile_count}t · cap ${cap} drills · d=${p.distance}`;
  c.appendChild(ti);
  c.addEventListener("click",e=>{e.stopPropagation(); toggle(p.id);});
  bg.appendChild(c);
  const l=document.createElementNS(NS,"text");
  l.setAttribute("text-anchor","middle");
  if(p.is_oil){
    l.setAttribute("x",p.cx); l.setAttribute("y",p.cy-p.r-3);
    l.classList.add("oil-label"); l.textContent=`oil#${p.id}`;
  } else {
    l.setAttribute("x",p.cx); l.setAttribute("y",p.cy); l.setAttribute("dy","0.3em");
    l.classList.add("patch-label"); l.textContent=`#${p.id}`;
  }
  bg.appendChild(l);
}
for(const o of DATA.oil_spots){
  const c=document.createElementNS(NS,"circle");
  c.setAttribute("cx",o.x); c.setAttribute("cy",o.y); c.setAttribute("r",3);
  c.classList.add("oil-spot"); bg.appendChild(c);
}

// ---------- origin (start position) orientation lines ----------
const originLayer=document.createElementNS(NS,"g"); svg.appendChild(originLayer);
function drawOriginLines(){
  while(originLayer.firstChild) originLayer.removeChild(originLayer.firstChild);
  if(!document.getElementById("show-origin").checked) return;
  for(const pid of selected){
    const p=patchById[pid]; if(!p) continue;
    const l=document.createElementNS(NS,"line");
    l.setAttribute("x1",p.cx); l.setAttribute("y1",p.cy);
    l.setAttribute("x2",0); l.setAttribute("y2",0);
    l.setAttribute("stroke",p.color); l.setAttribute("stroke-width",1.5);
    l.setAttribute("stroke-dasharray","4,4"); l.setAttribute("opacity",0.75);
    l.classList.add("origin-line");
    originLayer.appendChild(l);
  }
  const dot=document.createElementNS(NS,"circle");
  dot.setAttribute("cx",0); dot.setAttribute("cy",0); dot.setAttribute("r",4);
  dot.setAttribute("fill","#d11"); dot.setAttribute("stroke","#fff");
  dot.setAttribute("stroke-width",1.5); dot.classList.add("origin-line");
  originLayer.appendChild(dot);
}

// ---------- right pane: grouped table ----------
const tbl=document.getElementById("tbl-wrap");
function buildTable(){
  let h=`<table class="grp"><thead><tr>
    <th class="l">patch</th><th>dist</th><th>cap</th><th>dense</th></tr></thead><tbody>`;
  const byRes={};
  for(const p of DATA.patches){(byRes[p.resource]=byRes[p.resource]||[]).push(p);}
  for(const r of DATA.resources){
    const ps=(byRes[r]||[]).slice().sort((a,b)=>a.distance-b.distance);
    const req=(DATA.series[r]||{}).peak_demand_drills||0;
    const selCap=ps.filter(p=>selected.has(p.id)).reduce((s,p)=>s+(p.capacity||0),0);
    const nsel=ps.filter(p=>selected.has(p.id)).length;
    const unit=(DATA.series[r]||{}).unit||"drills";
    const cls = req<=0 ? "" : (selCap>=req ? "ok" : "short");
    const suff = req<=0 ? "(unmined)" : `${nsel} sel · ${selCap.toFixed(0)} / ${req.toFixed(0)} ${esc(unit)}`;
    const isC=collapsed.has(r);
    h+=`<tr class="group" data-res="${esc(r)}"><td colspan="4" class="${cls}">
        ${isC?"▸":"▾"} ${esc(r)} — ${suff}</td></tr>`;
    if(isC) continue;
    for(const p of ps){
      const sc=selected.has(p.id)?"sel":"";
      const capTxt=p.capacity==null?"—":p.capacity.toFixed(0);
      const denTxt=p.density==null?"—":(p.density*100).toFixed(0)+"%";
      h+=`<tr class="patch ${sc}" data-pid="${p.id}">
        <td class="l"><input type="checkbox" data-pid="${p.id}" ${selected.has(p.id)?"checked":""}>
          #${p.id}</td>
        <td class="num">${p.distance.toFixed(0)}</td>
        <td class="num">${capTxt}</td>
        <td class="num">${denTxt}</td></tr>`;
    }
  }
  h+=`</tbody></table>`;
  tbl.innerHTML=h;
  tbl.querySelectorAll('input[type=checkbox]').forEach(cb=>{
    cb.addEventListener("change",e=>{e.stopPropagation(); toggle(Number(cb.dataset.pid));});
  });
  tbl.querySelectorAll('tr.patch td.l').forEach(td=>{
    td.addEventListener("click",e=>{ if(e.target.tagName!=="INPUT") centerOn(Number(td.parentNode.dataset.pid)); });
  });
  tbl.querySelectorAll('tr.group').forEach(tr=>{
    tr.addEventListener("click",()=>{
      const r=tr.dataset.res;
      if(collapsed.has(r)) collapsed.delete(r); else collapsed.add(r);
      buildTable();
    });
  });
}

// ---------- selection ----------
function toggle(pid){
  if(selected.has(pid)) selected.delete(pid); else selected.add(pid);
  document.querySelector(`.patch[data-pid="${pid}"]`)?.classList.toggle("sel",selected.has(pid));
  buildTable(); drawChart(); drawOriginLines();
}
function centerOn(pid){
  const p=patchById[pid]; if(!p) return;
  const vb=svg.viewBox.baseVal;
  setVB(p.cx-vb.width/2, p.cy-vb.height/2, vb.width, vb.height);
}

// ---------- dropdown ----------
const sel=document.getElementById("res-select");
for(const r of DATA.resources){
  const o=document.createElement("option"); o.value=r;
  const req=(DATA.series[r]||{}).peak_demand_drills||0;
  o.textContent=req>0?`${r} (req ${req.toFixed(0)})`:`${r} (unmined)`;
  sel.appendChild(o);
}
function syncDropdown(){ sel.value=activeRes; }
sel.addEventListener("change",()=>{ activeRes=sel.value; drawChart(); });

// ---------- chart ----------
const csvg=document.getElementById("chart-svg");
function drawChart(){
  while(csvg.firstChild) csvg.removeChild(csvg.firstChild);
  const ser=DATA.series[activeRes]; if(!ser) return;
  const st=ser.steps;
  const W=csvg.clientWidth||800, H=csvg.clientHeight||240;
  const m={l:48,r:120,t:12,b:28};
  const x0=DATA.t0_offset, x1=st.length?st[st.length-1].t1:x0+1;
  const selP=DATA.patches.filter(p=>p.resource===activeRes&&selected.has(p.id)&&p.capacity!=null)
                         .sort((a,b)=>a.distance-b.distance);
  let cum=0; const thresholds=[];
  for(const p of selP){ cum+=p.capacity; thresholds.push({y:cum,p}); }
  const unit=ser.unit||"drills";
  const maxBuilt=Math.max(0,...st.map(s=>Math.max(s.built,s.burner||0)));
  const yMax=Math.max(maxBuilt, cum, ser.peak_demand_drills, 1)*1.1;
  const sx=v=>m.l+(v-x0)/(x1-x0||1)*(W-m.l-m.r);
  const sy=v=>H-m.b-(v/yMax)*(H-m.t-m.b);

  const ax=document.createElementNS(NS,"g");
  for(let i=0;i<=4;i++){
    const yv=yMax*i/4, y=sy(yv);
    ax.appendChild(line(m.l,y,W-m.r,y,"#eee",1));
    ax.appendChild(txt(m.l-6,y+3,yv.toFixed(0),"end","#666",10));
  }
  const xr=x1-x0, tickStep = xr>600?120:xr>120?60:xr>30?10:5;
  for(let xv=Math.ceil(x0/tickStep)*tickStep; xv<=x1; xv+=tickStep){
    const x=sx(xv);
    ax.appendChild(line(x,m.t,x,H-m.b,"#f3f3f3",1));
    ax.appendChild(txt(x,H-m.b+14,fmtAxisTime(xv),"middle","#666",10));
  }
  ax.appendChild(txt(2,m.t-2,unit,"start","#444",10));
  csvg.appendChild(ax);

  const anyBurner=st.some(s=>(s.burner||0)>1e-6);
  if(anyBurner) csvg.appendChild(stepPath(st,"burner",sx,sy,"#b5651d",1.6,0.9,"4,3"));
  csvg.appendChild(stepPath(st,"utilized",sx,sy,patchColor(activeRes),1.3,0.35,null));
  csvg.appendChild(stepPath(st,"built",sx,sy,patchColor(activeRes),2.2,1.0,null));

  for(let i=0;i<thresholds.length;i++){
    const th=thresholds[i], y=sy(th.y);
    const ln=line(m.l,y,W-m.r,y,"#c0271a",1.2); ln.setAttribute("stroke-dasharray","5,4");
    csvg.appendChild(ln);
    csvg.appendChild(txt(W-m.r+4,y+3,`#${th.p.id}: ${th.y.toFixed(0)}`,"start","#c0271a",9));
  }
  const req=ser.peak_demand_drills;
  document.getElementById("chart-meta").textContent =
    `${activeRes}: peak ${req.toFixed(0)} ${unit} · selected cap ${cum.toFixed(0)} `
    + (cum>=req?"✓ covers":"✗ short")+` · ${selP.length} patch(es)`;
}
function patchColor(r){ return (DATA.patches.find(p=>p.resource===r)||{}).color||"#1f77b4"; }
function stepPath(st,key,sx,sy,color,w,op,dash){
  let d="",started=false;
  for(const s of st){ const v=s[key]||0;
    d+=(started?"L":"M")+sx(s.t0).toFixed(1)+","+sy(v).toFixed(1)+" ";
    d+="L"+sx(s.t1).toFixed(1)+","+sy(v).toFixed(1)+" "; started=true; }
  const p=document.createElementNS(NS,"path"); p.setAttribute("d",d);
  p.setAttribute("fill","none"); p.setAttribute("stroke",color);
  p.setAttribute("stroke-width",w); p.setAttribute("opacity",op);
  if(dash) p.setAttribute("stroke-dasharray",dash);
  return p;
}
function line(x1,y1,x2,y2,c,w){const l=document.createElementNS(NS,"line");
  l.setAttribute("x1",x1);l.setAttribute("y1",y1);l.setAttribute("x2",x2);l.setAttribute("y2",y2);
  l.setAttribute("stroke",c);l.setAttribute("stroke-width",w);return l;}
function txt(x,y,s,anc,c,fs){const t=document.createElementNS(NS,"text");
  t.setAttribute("x",x);t.setAttribute("y",y);t.setAttribute("text-anchor",anc);
  t.setAttribute("fill",c);t.setAttribute("font-size",fs);t.textContent=s;return t;}

// ---------- pan / zoom ----------
function setVB(x,y,w,h){
  if(w>initVB.w+1e-6 || h>initVB.h+1e-6){ x=initVB.x; y=initVB.y; w=initVB.w; h=initVB.h; }
  svg.setAttribute("viewBox",`${x} ${y} ${w} ${h}`); updateZoom();
}
function updateZoom(){
  const vb=svg.viewBox.baseVal, z=initVB.w/vb.width;
  document.getElementById("zoom-pct").textContent=`zoom ${(z*100).toFixed(0)}%`;
  document.querySelectorAll(".patch-label").forEach(el=>{el.style.fontSize=(28/z)+"px";el.style.strokeWidth=(2/z);});
  document.querySelectorAll(".oil-label").forEach(el=>{el.style.fontSize=(24/z)+"px";el.style.strokeWidth=(2/z);});
}
function clientToUser(cx,cy){
  const ctm=svg.getScreenCTM(); if(!ctm) return {x:0,y:0};
  const pt=svg.createSVGPoint(); pt.x=cx; pt.y=cy; return pt.matrixTransform(ctm.inverse());
}
document.getElementById("reset-view").addEventListener("click",()=>setVB(initVB.x,initVB.y,initVB.w,initVB.h));
document.getElementById("show-labels").addEventListener("change",e=>{
  document.querySelectorAll(".patch-label").forEach(el=>el.style.display=e.target.checked?"":"none");});
document.getElementById("show-origin").addEventListener("change",drawOriginLines);
svg.addEventListener("wheel",e=>{e.preventDefault();
  const f=e.deltaY>0?1.2:1/1.2, vb=svg.viewBox.baseVal, u=clientToUser(e.clientX,e.clientY);
  setVB(u.x-(u.x-vb.x)*f, u.y-(u.y-vb.y)*f, vb.width*f, vb.height*f);},{passive:false});
let drag=null;
svg.addEventListener("mousedown",e=>{const vb=svg.viewBox.baseVal, rect=svg.getBoundingClientRect();
  const upp=Math.max(vb.width/rect.width, vb.height/rect.height);
  drag={x:e.clientX,y:e.clientY,vx:vb.x,vy:vb.y,upp};});
svg.addEventListener("mousemove",e=>{ if(!drag)return; const vb=svg.viewBox.baseVal;
  setVB(drag.vx-(e.clientX-drag.x)*drag.upp, drag.vy-(e.clientY-drag.y)*drag.upp, vb.width, vb.height);});
svg.addEventListener("mouseup",()=>drag=null); svg.addEventListener("mouseleave",()=>drag=null);

// ---------- YAML export (patch-selection L2 input) ----------
function buildYAML(){
  const L=[];
  L.push("# Patch selection — OPTIONAL L2 input (fplan rates add-selection / --patch-selection).");
  L.push("# Generated by the supply-curve viz. Per resource: which patches to commit miners");
  L.push("# to, and the derived cap. L2 overrides that resource's tile pool (drills) /");
  L.push("# oil-spot count (pumpjacks) with these; resources omitted keep full map availability.");
  L.push(`seed: ${DATA.seed}`);
  L.push(`scenario: ${DATA.scenario}`);
  if(DATA.footprint!=null) L.push(`drill_footprint: ${DATA.footprint}`);
  L.push("resources:");
  const byRes={};
  for(const p of DATA.patches){(byRes[p.resource]=byRes[p.resource]||[]).push(p);}
  let any=false;
  for(const r of DATA.resources){
    const ps=(byRes[r]||[]).filter(p=>selected.has(p.id)).sort((a,b)=>a.distance-b.distance);
    if(!ps.length) continue;
    any=true;
    const unit=(DATA.series[r]||{}).unit||"drills";
    const cap=ps.reduce((s,p)=>s+(p.capacity||0),0);
    const peak=(DATA.series[r]||{}).peak_demand_drills||0;
    L.push(`  ${r}:`);
    L.push(`    unit: ${unit}`);
    L.push(`    patches: [${ps.map(p=>p.id).join(", ")}]`);
    if(unit==="pumpjacks"){
      L.push(`    spots: ${ps.reduce((s,p)=>s+(p.spot_count||0),0)}`);
    } else {
      L.push(`    total_tiles: ${ps.reduce((s,p)=>s+(p.tile_count||0),0)}`);
    }
    L.push(`    capacity: ${cap.toFixed(1)}`);
    L.push(`    peak_demand: ${peak.toFixed(1)}`);
  }
  if(!any) L.push("  {}  # nothing selected");
  return L.join("\n")+"\n";
}
document.getElementById("export-yaml").addEventListener("click",()=>{
  const blob=new Blob([buildYAML()],{type:"text/yaml"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download=`${DATA.scenario}_patch-selection.yaml`;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
});

// ---------- boot ----------
buildTable(); syncDropdown(); updateZoom(); drawOriginLines();
window.addEventListener("resize",drawChart);
requestAnimationFrame(drawChart);
</script></body></html>
"""


def render_supply_curve_html(ds: dict) -> str:
    """Render the supply-curve dataset to self-contained interactive HTML.

    Untrusted map/solve text reaches two sinks: the embedded JSON (neutralized
    with ``_script_safe``) and ``innerHTML`` interpolations in the JS, which use
    an ``esc()`` helper. The title is HTML-escaped; the viewBox is numeric.
    """
    r = ds["map_rect"]
    viewbox = f"{r['x']:.1f} {r['y']:.1f} {r['w']:.1f} {r['h']:.1f}"
    repl = {
        "__SC_TITLE__": escape(f"supply-curve {ds['scenario']}"),
        "__SC_VIEWBOX__": viewbox,
        "__SC_DATA__": _script_safe(json.dumps(ds, separators=(",", ":"))),
        "__JS_HELPERS__": _JS_SHARED_HELPERS,
    }
    return _SC_PLACEHOLDER_RE.sub(lambda m: repl[m.group(0)], _SUPPLY_CURVE_TEMPLATE)
