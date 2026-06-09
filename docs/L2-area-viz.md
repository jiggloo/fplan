# L2 — facility area

The facility-area view answers one question for L3: **how much area does the
plan reserve for each item, and how much of it is actually running?** It plots,
per item over time, two curves of footprint × machine-count:

- **allocated** (solid) — the machines *committed* to producing that item: the
  per-key [assignment](L2-assignment.md) buckets (drills@ore, furnaces@product,
  assemblers@recipe) plus any pooled machines running its recipe.
- **utilized** (faint) — the machines *actually running* its recipe that step.

The gap, `idle = allocated − utilized ≥ 0`, is **built-but-not-running** area —
placed facilities L3 must still reserve even when momentarily idle. For a
repurpose-penalized bucket the gap is real (a committed bucket can sit larger
than its running count); for a pooled item the two lines coincide. This is the
spatial L2→L3 handoff lens, and the successor to the retired capacity-saturation
heatmap.

`rates viz` writes it as `runs/<run>/viz/<stem>-area.html` by default
(`--no-area` to skip). The view reuses the timeline's template, legend, nav, and
zoom (one overlay panel, solid + faint), composed through
`render_html(dataset, charts=AREA_CHARTS)` — no string-surgery on the template.

The bottom table breaks the selected step (the peak-area step by default) into
each visible item's allocated / utilized / idle area. **Clicking an item** there
pops a per-facility breakdown — how many of each facility are *assigned* (built)
vs *running* for that item, and the tiles each contributes (e.g. `coal` at the
`electronics` step → some electric-mining-drills, mostly assigned-but-idle, plus
fully-utilized burner-mining-drills). It reuses the timeline's popup element and
placement; the per-(step, item, building) counts come from `_area_step_detail`
in `fplan.l2.viz`, the same core the area series sums.

## What it reads — emitted, not recomputed

`rates viz` runs without a Factorio install (the model load is best-effort), so
the area view cannot recompute footprints or recipe outputs at render time.
Instead the solve emits the two reference maps it needs, single-sourced from the
instance + model where both are authoritative — the same no-drift discipline as
the `spatial:` block:

- **`facilities: {building: {footprint, base_speed}}`** — the **deployed**
  footprint (infrastructure included — the real tiles L3 places) and base
  crafting speed of every building the solve used. From
  `_facilities_dict` in `fplan.l2.solve`.
- **`recipe_outputs: {recipe: item}`** — each recipe's principal output
  (`outputs[0]`), so a running or committed machine's area attributes to the
  item it makes. Pseudo-rows (research / launch / power) carry no item output and
  are omitted. From `_recipe_outputs_dict`.

The view computes, per step:

```
allocated[item] = Σ_penalized-buckets  max(count_start, count_end) · footprint
                + Σ_pooled-activity     machines · footprint
utilized[item]  = Σ_activity            machines · footprint
machines        = recipe_sec_used / (base_speed · duration)
```

Allocated uses `max(count_start, count_end)` — the most machines on the ground at
either boundary, i.e. what L3 must place (a bucket grows as machines are built,
or starts high and shrinks as they're consumed; the max is what was there). Since
`machines ≤ max(start, end)` for a penalized bucket, `idle ≥ 0` falls out. See
`compute_area_series` / `build_area_dataset` in `fplan.l2.viz`.

## The base-area split (a `rates post` report)

`rates post` echoes a companion console report — the spatial counterpart to
#revisits — splitting each step's base area into:

- **penalized** — machines an assignment block committed to one job (statically
  placeable).
- **flexible** — the remainder on a *repurposable* kind (`assembling-machine`,
  `furnace`, `mining-drill`) that L3 may still pool step-to-step.
- **static** — the remainder on a fixed kind (boilers, labs, …): already a block.

The **penalized fraction is the data-driven dial** for how static the base is:
nearer 100% → easier L3 placement, traded against `t_FINAL` (committing more
machines costs player-time to repurpose). The report reads the emitted
`facilities:` footprints (deployed, no drift) and the model's building `kind`;
`compute_area_split` / `format_area_split` live in `fplan.l2.flatten`. A
`rates.yaml` predating the `facilities:` block prints a one-line note instead —
re-solve to populate it.

## Not yet migrated

- **Productive labs and pseudo / "+"-joined rows** carry no single-building
  footprint in the `facilities:` map, so their area is omitted (consistent with
  the prototype). The labs consume rather than produce an item, so this is a gap
  only for the rare module-loadout area, not for normal production.
