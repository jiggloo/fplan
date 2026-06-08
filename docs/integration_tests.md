# Integration tests

Most of fplan is covered by the automated suite (`pytest`), which runs in CI
against captured fixtures. This file is the home for the checks that can't —
**manual** for now, and the place to document integration testing as it grows.

## Table of Contents

- [Manual integration tests](#manual-integration-tests)
  - [Setup](#setup)
  - [Game model load](#game-model-load)
  - [Map extraction](#map-extraction)
  - [Map from string](#map-from-string)
  - [L2 solve](#l2-solve)
  - [L2 viz](#l2-viz)
  - [L2 post](#l2-post)
  - [Cleanup](#cleanup)

## Manual integration tests

Some functionality needs a **real Factorio installation** and can't run in CI —
the live prototype-data load, the headless map extraction, and the SCIP solve (a
per-seed primal coin flip). Verify them by hand after changes that touch the
loaders or the solver.

### Setup

Configure Factorio and seed the working directory with the bundled examples:

```bash
.venv/bin/fplan init --copy-examples
```

`init` records the Factorio paths (see [Configuration](usage.md#configuration));
`--copy-examples` drops the example scenarios / tech-orders / maps / run
manifests into the current directory so the steps below run from the repo root.
([Cleanup](#cleanup) is at the end.)

### Game model load

Parse the installed Factorio prototype data and print a summary:

```bash
.venv/bin/python -m fplan.model
```

Confirm it succeeds and the counts look sane (hundreds of recipes/items). The
automated tests exercise the model *cleaning* against a small captured prototype
fixture; this exercises the live Lua load that fixture stands in for.

### Map extraction

Run a headless extraction against a save and confirm the artifact (and that the
source save is untouched):

```bash
.venv/bin/fplan map from-save path/to/save.zip --out maps/save.yaml
.venv/bin/fplan map show maps/save.yaml
```

### Map from string

Generate an artifact from a map-exchange string (the `>>>…<<<` blob exported by
Factorio's map-generation screen). This is the only check of the
string→generate→probe path; it runs Factorio twice (create + probe). Confirm the
artifact is sane and that its seed/resources match what Factorio shows for that
string:

```bash
.venv/bin/fplan map from-string --from path/to/exchange.txt --out maps/exch.yaml
pbpaste | .venv/bin/fplan map from-string --from - --out maps/exch.yaml  # or paste
.venv/bin/fplan map show maps/exch.yaml
```

### L2 solve

The SCIP optimize needs the full model and is a per-seed primal coin flip, so
it's exercised here rather than in CI (the automated tests cover the
solver-*neutral* L2 layer — config, scenario, instance build, deployment —
against the fixture). Solve the **steelaxe** example (the quickest smoke):

```bash
.venv/bin/fplan rates solve steelaxe
.venv/bin/fplan run show steelaxe
```

Confirm it reports a feasible `t_FINAL` and writes `rates.yaml`; `run show
steelaxe` then lists `rates.yaml` under artifacts, and
`runs/steelaxe/manifest.yaml` has gained an `l2:` block
(mode/seed/objective_s/status/solve_time_s/config).

The committed `fishminer` run binds the full `default-victory` campaign — larger,
and it may need several seeds to land an incumbent. Drive several in one command
(solved in parallel, up to one process per seed, capped at your CPU count) and
promote the best:

```bash
.venv/bin/fplan rates solve fishminer --seeds 8 --time-limit-s 300   # -j N caps workers
```

Each seed's candidate lands under `runs/fishminer/rates-search/` (with a
`summary.yaml` index); the best is promoted to `runs/fishminer/rates.yaml` after
a prompt (`--force` to skip). The `rates-search/` scratch is ephemeral.

The `default-victory` campaign also exercises the two module hacks against the
full model (the fixture can't drive a real solve). Confirm the solve log prints
both `⚙ rocket-silo modules: …` and `⚙ lab modules: …`, and — since the time
objective only adopts the slower productive labs when science is worth saving —
check the promoted `rates.yaml` for `productivity-module` production feeding the
research steps (the lab productivity-module variant being used, not just offered).

It also exercises **facility assignment** end-to-end. Confirm the solve log prints
`⚙ facility assignment: mining … smelting … crafting …` and a
`[constraint-stats] … nonlinear=` count in the ~2,000 range (the curated split is
tractable — the barrier backend finds a primal). In the promoted `rates.yaml`,
check that steps carry `mining_assignment` / `smelting_assignment` /
`assembler_assignment` records (`<building>@<key>`), that the per-recipe assembler
blocks ramp and **hold** (monotone — the optimizer dedicates machines and rarely
pays to repurpose), and that an `assembler-machine-2@…` block **repurposes** from
a science pack to a rocket-part material once research ends (the end-game
reassignment the player-time cost is meant to allow only when it pays off).

### L2 viz

Render a solved run's `rates.yaml` as interactive HTML (timeline +
capacity-saturation heatmap) under `runs/<run>/viz/`:

```bash
.venv/bin/fplan rates viz steelaxe --open
```

Confirm it writes `viz/rates-timeline.html` + `viz/rates-heatmap.html` and (with
`--open`) opens the timeline.

### L2 post

Post-process a solved `rates.yaml` into the layout-stage input and auto-generate
the visualization. `post` is the L2→L3 stage (still under development); its
current operation is rate-flattening:

```bash
.venv/bin/fplan rates post steelaxe
.venv/bin/fplan rates viz steelaxe --from runs/steelaxe/rates-post.yaml
```

Confirm `rates post` writes `rates-post.yaml` (with a `post:` block) and
`viz/rates-post-timeline.html`, and prints a revisits summary. The second command
regenerates the diff view from the post file — it auto-detects the flatten view
from the `post:` block.

> `rates-post.yaml` is the **provisional** L2→L3 input — both its role and its
> schema are temporary while L2→L3 is explored (see
> [L2 rate-flattening](L2-rate-flattening.md)); don't build anything downstream
> that assumes the schema is stable.

### Cleanup

The solve/viz/post outputs land under `maps/` and `runs/`, which are gitignored.
The copied example inputs land in the tracked `scenarios/` and `tech-orders/`, so
they show as untracked files — discard them when you're done:

```bash
git clean -n scenarios tech-orders   # preview; drop -n to actually delete
```
