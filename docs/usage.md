# Usage reference

The full reference for the `fplan` command-line interface: how to invoke it,
how to configure it, and per-command examples. The [README](../README.md) is the
starting point; this document is where the detail lives and grows as commands
are implemented.

## Table of Contents

- [Invoking the CLI](#invoking-the-cli)
- [The command tree](#the-command-tree)
- [Configuration](#configuration)
- [Plan your own factory](#plan-your-own-factory)
- [Commands](#commands) (alphabetical by group)
  - [`inspect`](#inspect)
  - [`map`](#map)
  - [`rates`](#rates)
  - [`run`](#run)
  - [`tech-order`](#tech-order)
- [Extras](#extras)
  - [HiGHS + SCIP setup](#highs--scip-setup)

## Invoking the CLI

The CLI is fplan's primary interface. After installing into the virtualenv (see
the README's *Install* section), invoke it via `.venv/bin/fplan`:

```bash
.venv/bin/fplan                 # working directory + resolved config status
.venv/bin/fplan --help          # the full command tree
.venv/bin/fplan --version       # the installed version
.venv/bin/fplan map --help      # help for any group or command
```

Run with no arguments, `fplan` prints the working directory it operates from and
the status of the config it found — a quick "where am I, what's set up" check.

(Activate the virtualenv with `source .venv/bin/activate` if you'd rather type
`fplan` directly.)

The package version is also importable directly:

```bash
.venv/bin/python -c "import fplan; print(fplan.__version__)"
```

## The command tree

The command groups (alphabetical for lookup; the `Level` column shows where each
sits in the L1 → L4 planning pipeline):

| Group / command | Level | Purpose |
|---|---|---|
| `execution` | L4 | Step generation (TAS-generator input) |
| `init` | — | Create the config file |
| `inspect` | — | Inspect the game model (tech / item / recipe) |
| `layout` | L3 | Spatial placement |
| `map` | — | Map artifact generation and inspection |
| `rates` | L2 | Production-rate solving |
| `run` | L2–L4 | Create and manage pipeline runs |
| `tech-order` | L1 | Technology research ordering |

The surface is complete, but the stages are being migrated incrementally. A
command that isn't built yet prints a clear notice and exits with a reserved
code rather than failing cryptically — two codes distinguish the states so
scripts can tell them apart:

| Exit code | Meaning |
|---|---|
| `70` | Exists in the source project (factorio_explore) but not yet ported |
| `71` | Planned but not yet implemented |

Commands that take a side-effecting action (write an artifact, create the config
file, run a stage) also accept `--dry-run`, which reports what would happen
without doing it.

**Effective settings.** When a command has optional parameters, it prints a
`settings:` line up front showing the value in effect for each — with `(default)`
marking the ones you didn't override — so omitting a flag is never opaque. For
example, `rates solve` without `--seed` still prints the (random) seed it chose,
so the run stays reproducible.

## Configuration

fplan reads `.fplan-config.yaml` from the current working directory. It mainly
records where Factorio is installed — its data directory (prototype files, for
commands that load the game model) and its executable (for commands that run
Factorio headless). Generate it with:

```bash
.venv/bin/fplan init
```

`init` asks before scanning the known install locations for your OS, fills in
what it finds, and otherwise writes a template for you to complete (see
[`.fplan-config.example.yaml`](../.fplan-config.example.yaml) for the format). It
never overwrites an existing file — delete it to regenerate. Auto-detection is
only verified on macOS today; on Windows/Linux it warns and you should check the
paths it writes.

`init` also records a **solver preference** under `solver.lp_algorithm`. SCIP
links exactly one LP solver at build time, and `init` detects which one the
active install provides: **HiGHS** offers a barrier (interior-point) method that
can run faster on the larger `rates solve` models — it sidesteps the simplex
degeneracy that stalls the nonconvex root LP — so `init` prefers it when
available (SCIP marks the HiGHS LP interface experimental; see
[HiGHS + SCIP setup](#highs--scip-setup)). Other solvers (e.g. SoPlex) provide
simplex only. `rates solve` reads this preference;
[`--lp-algorithm`](#rates-solve) overrides it. To switch backends,
install fplan into an environment whose SCIP provides the one you want and re-run
`init`. If SCIP isn't importable, `init` records the safe simplex default. The
prebuilt `pyscipopt` wheel ships SoPlex; getting the HiGHS barrier means building
SCIP against HiGHS — see [HiGHS + SCIP setup](#highs--scip-setup).

Add `--copy-examples` to also copy the bundled example **scenarios**,
**tech-orders**, **maps**, and **run manifests** into the current directory:

```bash
.venv/bin/fplan init --copy-examples
```

This seeds the working directory so you can solve a run immediately without
changing directories — e.g. `fplan rates solve steelaxe`. It copies into
`scenarios/`, `tech-orders/`, `maps/`, and `runs/<run>/manifest.yaml`, **never
overwriting** files already there (your edits are safe), and reports how many it
copied. The flag is independent of writing the config — it still copies if the
config already exists, so you can run it on its own. (A run's per-run solve
outputs aren't copied; regenerate those with `rates solve`.) See the
[Quickstart](../README.md#quickstart).

Selecting a non-default config file:

```bash
.venv/bin/fplan --config-file /path/to/config.yaml map show maps/MySave.yaml
```

`--config-file` is a global option, so it precedes the subcommand.

**Require vs. warn.** Commands that *require* Factorio treat a missing or
invalid config as a fatal error (message to stderr, non-zero exit). `init` and
bare `fplan` only warn (to stdout) and continue.

There is no environment-variable support; CLI arguments take precedence over
config-file values where commands expose such options. `.fplan-config.yaml` is
git-ignored; the committed `.example` file is the documentation.

## Plan your own factory

The [Quickstart](../README.md#quickstart) solves a bundled example; this walks
the same flow for a goal of your own. It's deliberately minimal — each step
links to its command's reference below, where the options and variations live.

**1. Describe the goal — a scenario.** A scenario is the *problem*: the world you
want (and, optionally, the world you start from). Write a YAML file under
`scenarios/`:

```yaml
# scenarios/my-plan.yaml
name: my-plan
techs_researched:      # technologies to research
  - automation
items_produced:        # items to have produced (by name → count)
  iron-gear-wheel: 100
```

That's the whole contract for a from-scratch goal. To plan from an existing
world instead of nothing, add an `initial_state:` block (what exists at t₀) — see
[`examples/scenarios/steelaxe.yaml`](../examples/scenarios/steelaxe.yaml).

**2. Order the research (L1).** Turn the goal into a layered tech-order:

```bash
.venv/bin/fplan tech-order build scenarios/my-plan.yaml --out tech-orders/my-plan.yaml
```

**3. Pick a map.** The map is the resources / water / oil around spawn. Reuse a
bundled one (`fplan init --copy-examples` drops `maps/zaspar-wr.yaml`) or extract
your own from a save with [`map from-save`](#map-from-save).

**4. Bind them into a run.** A run ties one scenario + tech-order + map together:

```bash
.venv/bin/fplan run create my-plan \
    --scenario scenarios/my-plan.yaml \
    --tech-order tech-orders/my-plan.yaml \
    --map maps/zaspar-wr.yaml
```

**5. Solve, then look (L2).** Solve the production plan and open the result:

```bash
.venv/bin/fplan rates solve my-plan
.venv/bin/fplan rates viz my-plan --open
```

From here, explore variations in the reference below: ordering
[`--method`](#tech-order-build)s, [`rates solve`](#rates-solve) options
(multi-seed search, L2 tuning), and post-processing with
[`rates post`](#rates-post). If a from-scratch goal won't solve, it usually wants
an `initial_state` (step 1) — the bundled examples are known-good baselines to
copy and edit.

## Commands

Per-group command reference, in alphabetical order.

### `inspect`

Browse the loaded game model. Needs the configured `data_dir`.

All three subcommands — `tech`, `item`, `recipe` — share the same three modes:

```bash
.venv/bin/fplan inspect tech steel-axe          # detail for one entry
.venv/bin/fplan inspect tech --filter science   # detail for every match
.venv/bin/fplan inspect tech                     # list all names
```

A bare name shows that entry's detail; a bare command lists every name (a
discovery index); `--filter <substring>` shows the **full detail** of every entry
whose name contains the substring — so a search and an inspect are a single call,
not a list followed by per-name lookups. An unknown name is fatal (exit 1).

#### `inspect tech`

The detail view shows the science-pack cost (or research trigger), prerequisites,
the recipes the tech unlocks, and which techs require it (an essential tech is
also flagged with an `*(essential)*` marker on the name line):

```
steel-axe
  cost:          50 × (automation-science-packx1), 30s each
  prerequisites: steel-processing
  unlocks:       (no recipes)
  required by:   (nothing)
```

#### `inspect item`

Shows stack size, fuel value (when the item is a fuel), and the recipes that
produce and consume it, plus the techs that unlock it. A fluid is flagged with a
`*(fluid)*` marker on the name line:

```
iron-plate
  stack size:    100
  produced by:   iron-plate
  consumed by:   electronic-circuit, inserter, iron-gear-wheel
  unlocked by:   (available at start)
```

#### `inspect recipe`

Shows the category, crafting time, ingredients, outputs, the buildings that can
run it, and the techs that unlock it. A non-crafting recipe (a synthetic mining
or pumping recipe) is flagged with its kind on the name line:

```
mine/iron-ore  *(mining)*
  category:      basic-solid
  time:          1s
  ingredients:   (none)
  outputs:       iron-orex1
  made in:       burner-mining-drill, electric-mining-drill
  unlocked by:   (available at start)
```

### `map`

A *map artifact* is a single self-describing YAML bundle (seed, map-gen
settings, probe radius, resource patches, oil fields, water bodies, tree count)
describing the world around spawn, so a map can be reproduced and inspected from
the file alone.

#### `map from-save`

Turn a Factorio save into a map artifact. The output path is given explicitly
with `--out` (required — there is no implicit naming):

```bash
.venv/bin/fplan map from-save ~/Downloads/MySave.zip --out maps/world.yaml
.venv/bin/fplan map from-save MySave.zip --out maps/world.yaml --dry-run
```

It runs Factorio headless with a bundled extraction mod, so it needs the
configured Factorio **executable** (`fplan init`, or `binary:` in the config).
Notes:

- **The original save is never modified.** It's copied first, because headless
  Factorio autosaves on exit.
- **`--out` is required** and is written verbatim (no `maps/<name>` defaulting).
  `maps/` is the conventional, git-ignored location for these artifacts.
- **Existing output is not clobbered silently.** If the `--out` file already
  exists you're asked to confirm the overwrite; in a non-interactive session the
  command refuses (remove the file or choose another path). This check happens
  before Factorio runs.
- As with `init`, the headless interaction is only verified on macOS; on
  Windows/Linux it warns.

#### `map show`

Print a text summary of an artifact:

```bash
.venv/bin/fplan map show maps/world.yaml
```

```
seed=1063559207  radius=512 tiles
resources: 51 patches across 5 types
  coal: 8 patches, 9616 tiles total; nearest 29.1 tiles away (1201 tiles)
  copper-ore: 11 patches, 11663 tiles total; nearest 55.9 tiles away (1143 tiles)
  iron-ore: 12 patches, 11095 tiles total; nearest 47.8 tiles away (8242 tiles)
  stone: 17 patches, 1720 tiles total; nearest 19.1 tiles away (33 tiles)
  uranium-ore: 3 patches, 1636 tiles total; nearest 220.4 tiles away (974 tiles)
oil: 36 spots in 3 fields; nearest field 201.0 tiles away; avg yield 1074%/spot
water: 30 bodies; nearest 49.7 tiles away
trees: 4244
```

Each resource line gives the patch count, total tiles of that ore, and the
distance to (plus size of) the nearest patch of that type. The oil line gives
the nearest field's distance, the field count, and the average per-spot pumpjack
yield. All distances are tiles from spawn.

#### `map from-string`

Turn a Factorio **map-exchange string** (the `>>>…<<<` blob the map-generation
screen exports) into a map artifact. The string holds only the map-gen settings
and seed — not a generated world — so this generates a world from those settings
with headless Factorio and probes it, producing the **same artifact as
`from-save`**. It therefore also needs the configured Factorio **executable**.

The string comes from one of three sources; `--out` is required:

```bash
.venv/bin/fplan map from-string --from exch.txt --out maps/world.yaml   # a file
pbpaste | .venv/bin/fplan map from-string --from - --out maps/world.yaml  # stdin
.venv/bin/fplan map from-string --out maps/world.yaml                   # paste when prompted
```

`--from <path>` reads a file, `--from -` reads stdin, and omitting `--from` drops
into an interactive paste prompt (when stdin is a TTY; otherwise the command
exits with guidance to use `--from`). A `settings:` line reports the resolved
source and the Factorio version decoded from the string's header. Notes:

- The string is **validated before Factorio runs** (envelope, base64, zlib,
  version header), so a bad paste fails immediately rather than after a
  multi-minute generation.
- **Existing output is not clobbered silently** — same overwrite guard as
  `from-save`, checked before Factorio runs.
- The string carries the Factorio version it was exported from; a string from a
  major version your install can't parse will surface as a clean error.
- As with `from-save`, the headless interaction is only verified on macOS; on
  Windows/Linux it warns.

### `rates`

L2 — solve a run's production-rate plan with SCIP. `rates solve` is **run-aware**:
it reads a run's manifest, solves, writes `runs/<run>/rates.yaml`, and records the
L2 settings + outcome back into the manifest. Needs the configured `data_dir`.

For how the solve works and how to read its output, see
[L2 rates — the solve](L2-rates-solve.md).

#### `rates solve`

```bash
.venv/bin/fplan run create steelaxe-exp \
    --scenario scenarios/steelaxe.yaml \
    --tech-order tech-orders/steelaxe.yaml \
    --map maps/world.yaml
.venv/bin/fplan rates solve steelaxe-exp --mode experimental --seed 7
```

- Reads the run manifest's **scenario / tech-order / map**, resolved relative to
  the current working directory (matching `run show`).
- Writes the per-step plan to `runs/<run>/rates.yaml` (durations, per-recipe
  activity, energy, item flows with per-second rates + buffer seconds, capacity
  utilization, mining/smelting assignments, per-ore burner-drill extraction, fuel
  burn) plus a top-level **`spatial:`** block recording the map-derived caps the
  solve used (the deployed drill footprint + base speed, per-resource tile pool /
  drill cap, oil-spot count, map area) — single-sourced so the supply-curve view
  reads the exact caps the LP enforced. **Grows the manifest** with an `l2:`
  block: `mode`, `seed`, `objective_s`, `status`, `solve_time_s`, the config
  reference (and the patch-selection source, when one applied).
- `--mode lower-bound|experimental|trapezoidal` (default `experimental`).
- `--seed N` sets SCIP's randomization for a single solve (a random seed is
  picked and printed if omitted, so a good run replays exactly). Primal
  reliability is a coin flip per seed — for several seeds in one command, use
  `--seeds` (below).
- `--out PATH` redirects a **single** solve to an arbitrary file instead of
  `runs/<run>/rates.yaml`. It is a pure export: only the YAML is written, the
  manifest is **not** updated (the `l2:` block always means the promoted
  `rates.yaml`). Mutually exclusive with `--seeds`.
- `--l2-config PATH` deep-merges a tuning override over the packaged defaults —
  see [the L2 config](#l2-tuning-config) below.
- Solver controls: `--time-limit-s`, `--gap-limit`, `--stall-nodes`,
  `--node-limit`. Modeling A/B: `--max-area-fraction`, `--no-deployment`,
  `--no-player-time`.
- `--patch-selection PATH` restricts per-resource miner availability to a chosen
  patch set (the supply-curve view's export). It **overrides** a patch-selection
  bound on the run via [`rates add-selection`](#rates-add-selection); omit both
  for full map availability. See [patch selection](L2-rates-post.md#the-supply-curve-view--patch-selection).
- `--lp-algorithm barrier|simplex` picks the LP method, overriding the config's
  detected [`solver.lp_algorithm`](#configuration). Barrier needs a HiGHS-linked
  SCIP; omit it to use the config (or SCIP's default if unset).
- `--dry-run` builds the instance and prints a summary without solving. An
  existing `rates.yaml` is not clobbered silently (`--force` to overwrite).
- Exit `0` if a feasible incumbent was found, non-zero otherwise.

#### Multi-seed search (`--seeds`)

Because the primal is a per-seed coin flip, `--seeds` solves several seeds in one
command, stores each candidate, ranks them by `t_FINAL`, and promotes the best:

```bash
.venv/bin/fplan rates solve steelaxe-exp --seeds 8          # 8 random seeds
.venv/bin/fplan rates solve steelaxe-exp --seeds '[1,2,3]'  # exactly these seeds
```

- **`--seeds N`** (a bare integer) runs **N distinct random seeds**, each printed
  for reproducibility. **`--seeds '[a,b,c]'`** (a bracketed list) runs **exactly
  those seeds** (quote it so the shell doesn't glob/split the brackets; duplicates
  are de-duplicated, order preserved). Seeds must be in `1..2147483647`.
- Mutually exclusive with `--seed` and `--out`. The other per-solve options
  (`--mode`, the solver controls, `--l2-config`, the modeling A/B flags) apply
  uniformly to every seed.
- **Seeds solve in parallel** — `--jobs N` / `-j N` sets how many worker
  processes run concurrently (default: up to the CPU count, capped by the seed
  count; `--jobs 1` forces serial). Each solve is its own process (SCIP is
  single-threaded and not thread-safe), so this scales an 8-seed search from
  ~8× a single solve down to roughly one solve's wall-clock on enough cores.
  Each solve is heavy on CPU **and memory**, so cap `--jobs` if you're
  memory-bound. In parallel, the live per-seed lines print in completion order;
  `summary.yaml` is always seed-sorted.
- Each candidate is written to **`runs/<run>/rates-search/seed-<N>.yaml`** (same
  schema as `rates.yaml`), with a **`summary.yaml`** index recording every seed's
  objective / status / solve-time, the search settings, and the chosen best. The
  promoted `rates.yaml` is never touched during the search itself.
- **Per-seed logs (parallel).** When solving in parallel, each seed's full
  output — including SCIP's live progress table — is redirected to its own
  **`runs/<run>/rates-search/seed-<N>.log`** (captured at the file-descriptor
  level, so the solver's C-level output lands there too, not just Python's). The
  console prints the log paths up front so you can monitor any one solve:
  ```bash
  tail -f runs/<run>/rates-search/seed-<N>.log
  ```
  This keeps the parent console readable instead of interleaving every worker's
  output. `--quiet-solver` silences SCIP, leaving the per-seed logs near-empty
  (use it when you only want the ranked result, not live monitoring). Serial and
  single-seed solves run SCIP quietly as before — only fplan's own status lines
  print to the console; SCIP's progress table is a parallel-search-only feature.
- One seed failing or coming back infeasible does not abort the search — it is
  recorded in `summary.yaml` and the rest continue. **Best** = lowest `t_FINAL`
  among feasible seeds (infeasible seeds are never promotable).
- After ranking, you are prompted to **promote** the best seed to
  `runs/<run>/rates.yaml`; if one already exists, a second confirm guards the
  overwrite. Promotion copies the winning candidate and grows the manifest's
  `l2:` block (adding a `search:` record of the seeds tried + the promoted file).
  `--force` skips both prompts; a **non-interactive** session never clobbers
  `rates.yaml` — it leaves the candidates in place and tells you how to promote.
- If every seed is infeasible, the candidates + summary are still written, nothing
  is promoted, and the command exits non-zero.

#### L2 tuning config

L2's tunable values — per-building deployment packings, player-physics constants,
spatial caps, the character stand-in, mode weights + bootstrap seeding, and the
modeling-scope policy sets — live in a packaged default
(`src/fplan/resources/l2-defaults.yaml`). A power user overrides any subset with
`--l2-config PATH`; the file is **deep-merged** over the defaults, so you specify
only the keys you want to change:

```yaml
# my-tuning.yaml — only the deltas
caps:
  burner_drill: 80
deployment:
  pumpjack:
    tile_footprint: 18.0
```

Game-physics facts (boiler/rocket constants), the constraint formulation itself,
and the SCIP random seed are not in the config — the first two stay authoritative
in code, the seed is a per-solve flag recorded in the manifest.

#### Rocket-silo modules

The rocket-silo crafts rocket-parts much faster with modules and a beacon ring,
and that speedup is **scenario-driven**: the modules and beacons a scenario lists
in `items_produced` are applied to the silo's rocket-part crafting. Productivity
modules fill the silo's slots (raising rocket-part output per craft); speed
modules go in the beacons ringing it (raising crafting speed), transmitted at the
beacon's `distribution_effectivity`. Productivity modules in beacons are
disallowed (the game forbids them), and the effect magnitudes come from the game
data — so naming `productivity-module-3` vs `productivity-module` just works.

The bundled `default-victory` declares the WR-TAS loadout — 4× `productivity-module-3`
in the silo and 40× `speed-module` across 20 beacons (→ ×4.40 speed, ×1.40
rocket-part output, 13.15 MW). When the hack fires, `rates solve` prints a
`⚙ rocket-silo modules: …` line so the effect is visible. A scenario that
declares no silo modules runs the silo at base speed.

Disable the behavior with a `silo_modules` block in the same
[L2 tuning config](#l2-tuning-config) you pass via `--l2-config` (e.g. to
compare against an un-moduled silo):

```yaml
# my-tuning.yaml — passed with: rates solve <run> --l2-config my-tuning.yaml
silo_modules:
  enabled: false
```

#### Lab productivity modules

Research can run on **productive labs** — labs with every module slot filled by a
productivity module. Each productive lab trades speed (and draws more power) for
bonus research per cycle, so a tech finishes on fewer science packs. Unlike the
silo's fixed loadout, the count of productive labs is the **solver's choice**: it
runs research on productive labs only where that saves enough science to beat the
slowdown, and reserves the modules as infrastructure (produced on demand, not
declared in `items_produced`). The variant is offered only for research that runs
**after** the module is unlocked; earlier research stays on bare labs.

The module fills every slot of the lab (2 in vanilla → 2× `productivity-module`,
the prod-1 tier, by default). Effect magnitudes and the slot count come from the
game data. When the variant is active, `rates solve` prints a `⚙ lab modules: …`
line summarizing the loadout (e.g. `research output ×1.08, speed ×0.90, power
×2.00`).

Configure it with a `lab_modules` block in the same
[L2 tuning config](#l2-tuning-config) — disable the variant, or pick a higher
module tier:

```yaml
# my-tuning.yaml — passed with: rates solve <run> --l2-config my-tuning.yaml
lab_modules:
  enabled: false                  # run all research on bare labs
  # module: productivity-module-3   # or keep it enabled and pick a higher tier
```

#### Facility assignment

By default a facility is **committed to one job** — a mining drill to one ore, a
furnace to one product, an assembler to one recipe — so the solver produces
**static blocks** for L3 placement instead of a pool that swaps job step to step
(which rarely happens in real runs and can't be placed by a VLSI layout). One
concept, three classes that differ only in the underlying mechanics:

- **Mining** drills, per ore: a drill sits on a patch and can't switch ore, so
  each ore gets its own non-decreasing drill count.
- **Smelting** furnaces, per output: a furnace smelts whatever its input belt
  feeds. A furnace that gets consumed (a stone furnace is eaten by boilers and
  burner drills) may be torn down — detected from the recipe data — but never
  repurposed; steel furnaces stay strictly non-decreasing.
- **Crafting** assemblers, per recipe: switching a real assembler's recipe is a
  player action, so the solver pays player time to **assign** (set a recipe) and
  **unassign** (walk back and clear) — repurposing only where it's worth the cost.
  Consuming an assembler (AM1→AM2→AM3) is a free teardown, again from the recipe
  data. The split is **curated** (every science pack plus a list of higher-value
  intermediates) because the bilinear-term count, not the variable count, drives
  solve time; a full split is intractable.

When any class is active, `rates solve` prints a `⚙ facility assignment: …` line.
The plan emits `mining_assignment` / `smelting_assignment` / `assembler_assignment`
records (`<building>@<ore|output|recipe>`) per step for L3. Design rationale —
the three mechanics, the consumable-furnace teardown, the burner↔stone-furnace
bootstrap coupling, and the tractability trade-off — is in
[facility assignment (§4.7)](L2-rates-solve.md#47-facility-assignment).

Configure it with an `assignment` block in the same
[L2 tuning config](#l2-tuning-config). Set a class's `buildings: []` to disable
it (the facility reverts to a single pooled capacity):

```yaml
# my-tuning.yaml — passed with: rates solve <run> --l2-config my-tuning.yaml
assignment:
  mining:
    buildings: [electric-mining-drill, burner-mining-drill]
  smelting:
    buildings: [stone-furnace, steel-furnace]
  crafting:
    enabled: true
    buildings: [assembling-machine-1, assembling-machine-2]
    unassign_cost_s: 1.0          # player-time to clear a recipe (assign = 1 tick)
    split_science_packs: true     # every *-science-pack recipe gets its own block
    split_items: [engine-unit, inserter, transport-belt, pipe, boiler,
                  steam-engine, steel-furnace, electric-mining-drill]
    retire_after:                 # drop a building's vars once a tech is researched
      assembling-machine-1: low-density-structure
```

`retire_after` drops an assembler's recipe/step variables once the named tech is
researched — by then plans have upgraded to a higher tier (AM1→AM2/AM3 by
low-density-structure), so those variables are pure overhead. It's realism-free
pruning that shrinks the curated split (its tractability impact is quantified in
[facility assignment (§4.7)](L2-rates-solve.md#47-facility-assignment)). The map deep-merges per building;
to keep a building usable for the whole campaign, give it an empty tech
(`assembling-machine-1: ""`).

#### `rates viz`

Render a solved run's `rates.yaml` as **self-contained interactive HTML** —
written under `runs/<run>/viz/`:

```bash
.venv/bin/fplan rates viz steelaxe-exp            # timeline + facility area
.venv/bin/fplan rates viz steelaxe-exp --open     # ...and open the timeline
```

For the charts, the step detail table, and every interaction, see the
[visualizer reference](L2-rates-solve.md#6-the-visualizer-reference-rates-viz).

Three views:
- **`<stem>-timeline.html`** — three stacked panels (raw production rate, net
  rate, surplus count) on one zoomable x-axis, with a tree-grouped click-to-toggle
  legend (science packs + electric-mining-drill visible by default).
- **`<stem>-area.html`** — the **facility-area view**: per item, solid =
  allocated facility area (footprint × committed machines), faint = utilized
  (footprint × running machines); the gap is built-but-not-running area L3 must
  place. The spatial L2→L3 handoff lens. Suppress with `--no-area`. See the
  [facility-area design doc](L2-rates-post.md#the-facility-area-view).
- **`<stem>-supply-curve.html`** — interactive ore-patch map: click which patches
  to commit miners to against per-resource demand over time, then **Export YAML**
  a patch-selection file to feed back into the next solve. Needs the run's bound
  map (skipped with a note if it's unavailable); suppress with
  `--no-supply-curve`. See [patch selection](L2-rates-post.md#the-supply-curve-view--patch-selection).

Options:
- `--from PATH` visualizes any rates-shaped YAML instead of the run's
  `rates.yaml` — e.g. a search candidate `runs/<run>/rates-search/seed-N.yaml`
  to compare a losing seed. The output filename is stemmed from the input
  (`rates.yaml` → `rates-*`, `seed-9.yaml` → `seed-9-*`) so candidate views never
  clobber the promoted run's.
- `--open` opens the timeline in the default browser after writing. It follows
  `fplan init`'s platform convention: it abstracts the OS-specific open
  (`webbrowser`). On an **unrecognized** platform it skips opening and prints the
  path; on a **recognized-but-untested** one it notes that and still attempts; on
  any failure it falls back to printing the path.
- `--dry-run` reports what it would write without writing.

The game model is loaded **best-effort** (from the configured `data_dir`, to
enrich the legend with per-recipe facility counts); if it's unset or unavailable,
viz renders from the YAML alone with a one-line notice — **no Factorio install is
required** (the supply-curve view never loads the model — it reads the caps from
the solve's `spatial:` block). The HTML is self-contained (no external assets)
and overwritten freely (it's regenerated from `rates.yaml`); the manifest is not
modified.

#### `rates add-selection`

Bind a patch-selection file (the supply-curve view's **Export YAML**) to a run as
an optional L2-feedback input:

```bash
.venv/bin/fplan rates add-selection steelaxe-exp steelaxe_patch-selection.yaml
.venv/bin/fplan rates add-selection steelaxe-exp --remove   # unbind
```

- Records the file under the manifest's `inputs:` (with a content hash, like the
  other inputs, so `run show` reports its freshness). The next `rates solve` then
  restricts per-resource miner availability to that patch set.
- Re-running **replaces** a prior binding; `--remove` unbinds it. A one-off
  [`rates solve --patch-selection PATH`](#rates-solve) overrides the bound input
  without changing the manifest.

The file format, the L2 override it drives, and the supply-curve view that
produces it are documented in [patch selection](L2-rates-post.md#the-supply-curve-view--patch-selection).

#### `rates post`

Post-process a solved `rates.yaml` into the **layout-stage (L3) input**.
`rates post` is the L2→L3 post-processing stage; it's still under development and
will grow more operations. Its **current operation is rate-flattening**:
replacing each item's per-step production rate with the smoothest
constant-rate-per-segment schedule that still meets every deadline — minimizing
the number of assembler revisits (real TAS player-time) without producing ahead
of game causality. Writes `runs/<run>/rates-post.yaml` and, by default, a
visualization (for flattening, a diff of original vs flattened):

```bash
.venv/bin/fplan rates post steelaxe-exp                # chord (default) + viz
.venv/bin/fplan rates post steelaxe-exp --method tube  # taut-string method
.venv/bin/fplan rates post steelaxe-exp --no-viz       # data output only
```

The output `rates-post.yaml` is the same step/item schema as `rates.yaml` with
the post-processed production characteristics (`production_rate_per_s` /
`produced`), plus a sibling `post:` block recording the operation's settings, the
source, a summary, and the per-item / unmet-input diagnostics. It's the run's L3
input; the manifest gains a matching `post:` block.

Alongside the flatten summary, `post` echoes a **base-area split** — the
per-step base area (tiles) divided into *penalized* (machines an assignment
block committed to one job, statically placeable), *flexible* (the remainder on
a repurposable kind L3 may still pool), and *static* (a fixed kind, already a
block). The penalized fraction is the data-driven dial for how static the base
is — the spatial companion to #revisits — read from the solve's `facilities:`
footprints (re-solve an older `rates.yaml` to populate them).

> **Provisional.** `rates-post.yaml` is the *temporary* L3 input and **its
> schema is temporary too** — it mirrors `rates.yaml` only because L3's
> preferred format isn't decided yet. Don't build anything downstream that
> assumes the schema is stable.

Flattening methods (`--method`, default `chord`):
- **`chord`** (default) — straight chords between surplus-zero deadlines; the
  fewest revisits, but can self-stockout (counted and reported as unmet inputs).
- **`tube`** — the taut string through the causal tube; smoothest schedule that
  never stocks out and never front-loads past causality (zero self-stockouts by
  construction).
- **`mrp`** — cross-dependency backward demand explosion; fewer revisits on
  intermediates, still stage-1 (can self-stockout).

Other options:
- `--from PATH` post-processes any rates-shaped YAML instead of the run's
  `rates.yaml` (e.g. a search candidate); the output is still the run's
  `rates-post.yaml`.
- `--no-viz` skips the auto-generated viz; `--open` opens it after writing
  (same platform convention as `rates viz`); `--force` overwrites an existing
  `rates-post.yaml` without prompting; `--dry-run` reports what it would write.

Unlike `rates viz`, `post` **requires** the game model (a configured `data_dir`):
the unmet-input diagnostics and the `mrp` dependency graph both need the
recipe→ingredient map. See [L2 rates post-processing](L2-rates-post.md) for the
formulation.

The diff visualization (original vs flattened production, faint vs solid, plus
the unmet-input table) is auto-detected by `rates viz`: pointing it at a
post-processed file renders the diff view instead of the timeline —

```bash
.venv/bin/fplan rates viz steelaxe-exp --from runs/steelaxe-exp/rates-post.yaml
```

This regeneration is a **pure render** — it reads the flattened series and the
persisted `post:` diagnostics, plus the original series from the referenced
source `rates.yaml`; **no re-flattening**, and the model is loaded only
best-effort (to enrich the legend's facility counts), so it works without a
Factorio install. It has no companion facility-area view (assignment is
unchanged by flattening).

### `run`

A *run* is one execution of the L2→L4 pipeline. It lives in `runs/<name>/` and
is described by a `manifest.yaml` that binds the run's inputs — a **scenario**,
a **tech-order**, and a **map** — by reference (path + content hash),
and (as later stages land) accumulates their settings and outputs. L1 is an
*input* to a run, not part of it. See [Concepts](../README.md#concepts).

#### `run create`

Create a run directory and write its manifest:

```bash
.venv/bin/fplan run create steelaxe-exp \
    --scenario scenarios/steelaxe.yaml \
    --tech-order tech-orders/steelaxe.yaml \
    --map maps/world.yaml
```

- A run is **named** — `run create <name>` creates `runs/<name>/`. `runs/` is a
  managed, git-ignored directory like `maps/`.
- `--scenario`, `--tech-order`, and `--map` are all required — a run is L2→L4,
  and placement (L3) needs a map.
- Refuses if the run already exists (remove it, or use `run clone`).
- `--dry-run` reports what would be created and writes nothing.

#### `run clone`

Start a new run from an existing one's manifest — same input bindings, fresh
identity, **no stage artifacts copied** (a clean re-solve of the same problem):

```bash
.venv/bin/fplan run clone steelaxe-exp steelaxe-trap
```

#### `run show`

Show a run's bindings, whether each referenced input is still current (a
content-hash check flags edits since `create`), and which stage artifacts exist:

```bash
.venv/bin/fplan run show steelaxe-exp
```

```
run: steelaxe-exp
created: 2026-06-02T09:24:42+00:00
fplan: 0.0.10
inputs:
  scenario: scenarios/steelaxe.yaml [✓ current]
  tech-order: tech-orders/steelaxe.yaml [⚠ changed]
  map: maps/world.yaml [✗ missing]
artifacts: (none yet)
```

#### `run full`

Executing the whole L2→L4 chain against a run's manifest is planned but not yet
implemented — `fplan run full` currently exits with code `71`.

### `tech-order`

L1 — turn a scenario (a goal) into a layered technology research order, and
verify an order is a valid plan. Needs the configured `data_dir`.

#### `tech-order build`

Compute a research order from a scenario and write it as YAML. The output path
is given explicitly with `--out` (required):

```bash
.venv/bin/fplan tech-order build examples/scenarios/steelaxe.yaml --out tech-orders/steelaxe.yaml
.venv/bin/fplan tech-order build examples/scenarios/steelaxe.yaml --out o.yaml --method balanced
.venv/bin/fplan tech-order build examples/scenarios/steelaxe.yaml --out o.yaml --dry-run
```

```
Goal 'steelaxe': 3 techs across 2 layers — research order (layer 0 = earliest, last layer = goal asks)

── Layer 0  (2 techs)
   automation  [10 × (automation-science-packx1)]
   steel-processing  [50 × (automation-science-packx1)]

── Layer 1  (1 tech)
   steel-axe  [50 × (automation-science-packx1)]

→ tech-orders/steelaxe.yaml
```

- The scenario is a `GoalState` YAML — `techs_researched`, `items_produced`,
  `rocket_launches` (a scenario file's L2 `initial_state`/`checkpoints` blocks
  are ignored here). See [`examples/scenarios/`](../examples/scenarios/).
- `--method` selects the ordering: `forward` (default; ASAP, goal last),
  `from-goal` (ALAP, goal first), or `balanced` (slack-window midpoint).
  **`forward` and `balanced` produce executable, verifiable plans;** `from-goal`
  is a backward-planning *view* (goal first) that `verify` will reject — build
  with `forward` for a plan you intend to verify or feed downstream.
- `--out` is required and is not clobbered silently — an existing file prompts
  to confirm (interactive) or refuses (non-interactive), protecting a
  hand-edited order. `--dry-run` prints the order and writes nothing.
- The order is L1's *output*; the scenario is L1's *input*. The two are kept
  disjoint: the order carries **no goal content**, only a lightweight
  `scenario:` reference (name + path + content hash) recording what it was
  built from. `verify` resolves the goal from that reference.

#### `tech-order verify`

Check that a tech order is a valid research plan — every tech real and unique,
the set equals the goal's required closure, and the linear order respects all
prerequisites (extra techs and same-layer prereq pairs are non-fatal warnings):

```bash
.venv/bin/fplan tech-order verify examples/tech-orders/steelaxe.yaml
.venv/bin/fplan tech-order verify my-order.yaml --scenario examples/scenarios/steelaxe.yaml
```

The goal is resolved from the order's `scenario:` reference by default (a
content-hash mismatch warns but still verifies against the current scenario),
or from `--scenario PATH`. Exit `0` if valid, `1` if invalid.

#### `tech-order viz`

Rendering a tech order (layers / DAG) is planned but not yet implemented —
`fplan tech-order viz` currently exits with code `71`.

## Extras

### HiGHS + SCIP setup

`rates solve` can run faster on the larger models when SCIP uses **HiGHS**'s
barrier (interior-point) method (see [Configuration](#configuration)). The prebuilt
`pyscipopt` wheel links **SoPlex**, which has simplex only — so the HiGHS barrier
requires building SCIP against HiGHS and `pyscipopt` against that SCIP. The chain
is **HiGHS → SCIP (linked to HiGHS) → pyscipopt (linked to that SCIP)**, three
installs in order:

1. **Install the HiGHS C++ library.** This is a separate project and the
   prerequisite for everything below. Easiest is a package manager
   (macOS: `brew install highs`); otherwise use a
   [precompiled release](https://github.com/ERGO-Code/HiGHS/releases) or build
   from source — see [Install HiGHS](https://ergo-code.github.io/HiGHS/stable/installation/).
   Note: the PyPI `highspy` package is a Python *wrapper*, **not** this library,
   and won't work as the SCIP backend.
2. **Build SCIP with HiGHS as its LP solver.** SCIP picks its LP solver at
   CMake-configure time: `cmake .. -DLPS=highs -DHIGHS_DIR=<highs-install>/lib/cmake/highs`,
   then build and install. `HIGHS_DIR` must point at the *installed* HiGHS, not a
   build directory. See [SCIP — building with CMake](https://www.scipopt.org/doc/html/md_INSTALL.php).
   SCIP marks the HiGHS LP interface **experimental**, so treat it as such.
3. **Build `pyscipopt` against that SCIP.** Point `SCIPOPTDIR` at the SCIP install
   and force a source build so the SoPlex wheel isn't used:
   `SCIPOPTDIR=<scip-install> pip install --no-binary pyscipopt pyscipopt`. The
   SCIP must be CMake-built (not the legacy Makefile layout). See
   [PySCIPOpt — building from source](https://pyscipopt.readthedocs.io/en/latest/build.html).

Then `fplan init` (re-run it) detects the active backend and records
`solver.lp_algorithm: barrier`; it prints `SCIP LP backend: HiGHS …` when the
link worked. The steps are verified on macOS today; the authoritative docs above
cover the per-OS specifics for Linux and Windows.
