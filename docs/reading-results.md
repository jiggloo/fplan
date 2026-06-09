# Reading your results

You've solved a run (`rates solve`) and maybe post-processed it (`rates post`).
This page is the map of what you got: each artifact, the one question it answers,
and where to read it in depth. It's an orientation hub — it points at the deep
docs rather than repeating them, so you can stop as soon as you've found the
piece you need.

## What you produced

```bash
.venv/bin/fplan rates solve steelaxe-exp        # → rates.yaml + viz
.venv/bin/fplan rates viz   steelaxe-exp --open  # → three interactive views
.venv/bin/fplan rates post  steelaxe-exp         # → rates-post.yaml + diff view
```

| Artifact | Answers | Read it in |
|---|---|---|
| `rates.yaml` | *What's the plan?* — per-step schedule + the headline `t_FINAL` | [solve § Reading `rates.yaml`](L2-rates-solve.md#3-reading-ratesyaml) |
| **Timeline** view | *When is each item produced, and how fast?* | [solve § Visualizer reference](L2-rates-solve.md#6-the-visualizer-reference-rates-viz) |
| **Facility-area** view | *How much area does the plan reserve, and how much runs?* | [L2-area-viz.md](L2-area-viz.md) |
| **Supply-curve** view | *Which ore patches should I mine?* | [L2-patch-selection.md](L2-patch-selection.md) |
| **Diff** view (after `post`) | *Where can production be smoothed — and where can't it?* | [L2-rate-flattening.md](L2-rate-flattening.md) |

The rest of this page is one short paragraph per row: what to read off it, and
the one action it sets up. **Most readers want just one of these** — jump to it.

## The headline: did it solve, and how long?

The viz top bar (and `rates.yaml`'s status block) carry the three numbers you
check first: **`obj`** — the optimized `t_FINAL`, the schedule's elapsed time;
**`gap`** — how far from proven-optimal the solver got (a feasible plan, not
necessarily the best one); and **`total`** — the absolute in-game clock the plan
ends at, when the scenario starts partway into a save. A large `gap` means *keep
this plan, but don't read it as optimal* — and is the cue to try a [multi-seed
search](usage.md#multi-seed-search---seeds). The full read — solver status,
solution quality, and why two solves of the same problem differ — is
[solve § 3.1](L2-rates-solve.md#31-solver-status-and-solution-quality) and
[§ 3.3](L2-rates-solve.md#33-why-results-vary).

## The timeline — the production schedule over time

The default view: three stacked panels (raw production rate, net rate, surplus
count) on one zoomable time axis, with a click-to-toggle legend of every item and
recipe. Pick an item, hover a step, read its exact rate. This is where you answer
"when does science ramp," "what's the bottleneck step," "does this item ever
stall." The at-a-glance tour is [solve § 2](L2-rates-solve.md#2-what-a-result-looks-like-the-visualization);
the full chart set, the step-detail table, and every interaction are in
[solve § 6](L2-rates-solve.md#6-the-visualizer-reference-rates-viz).

## The facility-area view — what the plan reserves vs. runs

Per item over time, two curves of footprint × machine-count: **allocated**
(solid — machines committed to that item) and **utilized** (faint — machines
actually running it). The gap between them is **built-but-not-running** area that
L3 must still place. Click an item in the bottom table to break a step into its
per-facility counts (e.g. how many drills are assigned but idle). This is the
spatial L2→L3 handoff lens; read it when you care about *area*, not rate. Full
treatment — the area math, the per-facility popup, and the base-area split that
`rates post` echoes — is [L2-area-viz.md](L2-area-viz.md).

## The supply-curve view — which patches to mine

L2 is geometry-blind: it pools all patches of a resource into one tile budget, so
*which* patches carry the drills is a call it leaves to you. The supply-curve view
shows each patch on a map against per-resource miner demand over time; you click
patches to commit, then **Export YAML** a patch-selection file and feed it back
into the next solve ([`rates add-selection`](usage.md#rates-add-selection)). This
is the one view that produces an input, not just a reading. It needs the run's
bound map and is skipped with a note when that's unavailable. Full treatment:
[L2-patch-selection.md](L2-patch-selection.md).

## After `rates post` — the flattened schedule and its diff

`rates post` rewrites each item's jagged per-step production into the smoothest
constant-rate schedule that still meets every deadline — an estimate of how few
times a TAS player must revisit an assembler. It doesn't change `t_FINAL`. Point
`rates viz` at the post output to get the **diff view**: original (faint) vs.
flattened (solid) production, plus an **unmet-input table** flagging where
smoothing would self-stockout.

```bash
.venv/bin/fplan rates viz steelaxe-exp --from runs/steelaxe-exp/rates-post.yaml
```

The unmet-input rows are the point: they mark where the flattened schedule
*can't* hold without producing ahead of causality — so the smoothing is real
information, not just cosmetics. The methods (`chord` / `tube` / `mrp`) and the
formulation are in [L2-rate-flattening.md](L2-rate-flattening.md); `post` also
prints a base-area split to the console ([L2-area-viz.md § base-area
split](L2-area-viz.md#the-base-area-split-a-rates-post-report)).

## Where to go next

- **Probe the model yourself** — cap a building, add a checkpoint, change the
  objective: [solve § 5, Extending the solve](L2-rates-solve.md#5-extending-the-solve-asking-your-own-questions).
- **See how the stages connect** — the L1→L4 data flow and the design-doc index:
  [Architecture](architecture.md).
- **Every command and flag** — [Usage reference](usage.md).
