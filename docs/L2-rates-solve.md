# L2 rates — the solve (`fplan rates solve`)

This document helps you do three things with the solve, in order of how most
people need them:

1. **Use it** — understand what it computes and what a result looks like
   ([§1](#1-inputs-and-output)–[§2](#2-what-a-result-looks-like-the-visualization)).
2. **Read it** — interpret a result in depth ([§3](#3-reading-ratesyaml)).
3. **Extend it** — change the model to probe for new solutions
   ([§4](#4-how-the-solve-works-under-the-hood)–[§5](#5-extending-the-solve-asking-your-own-questions)).

You can stop after [§3](#3-reading-ratesyaml) if you only want to read plans;
continue to [§5](#5-extending-the-solve-asking-your-own-questions) when you want
the model to answer a question of your own.

## Table of Contents

- [1. Inputs and output](#1-inputs-and-output)
- [2. What a result looks like (the visualization)](#2-what-a-result-looks-like-the-visualization)
- [3. Reading `rates.yaml`](#3-reading-ratesyaml)
  - [3.1 Solver status and solution quality](#31-solver-status-and-solution-quality)
  - [3.2 The per-step records](#32-the-per-step-records)
  - [3.3 Why results vary](#33-why-results-vary)
- [4. How the solve works (under the hood)](#4-how-the-solve-works-under-the-hood)
  - [4.1 Steps, tiers, and the objective](#41-steps-tiers-and-the-objective)
  - [4.2 The decision variables](#42-the-decision-variables)
  - [4.3 The constraints](#43-the-constraints)
  - [4.4 Modeling assumptions](#44-modeling-assumptions)
  - [4.5 Solver-specific choices and tractability hacks (SCIP)](#45-solver-specific-choices-and-tractability-hacks-scip)
  - [4.6 Downstream feedback (back-propagation)](#46-downstream-feedback-back-propagation)
- [5. Extending the solve (asking your own questions)](#5-extending-the-solve-asking-your-own-questions)
  - [5.1 How the solver code is organized](#51-how-the-solver-code-is-organized)
  - [5.2 Example: capping the number of labs](#52-example-capping-the-number-of-labs)
  - [5.3 Example: adding a checkpoint](#53-example-adding-a-checkpoint-a-scenario-change)
  - [5.4 Variable reference](#54-variable-reference)
  - [5.5 Constraint reference](#55-constraint-reference)
  - [5.6 Assumptions a new constraint must respect](#56-assumptions-a-new-constraint-must-respect)
  - [5.7 Beyond constraints: objective, recipes, physics](#57-beyond-constraints-objective-recipes-physics)
- [6. The visualizer reference (`rates viz`)](#6-the-visualizer-reference-rates-viz)
  - [6.1 The timeline charts](#61-the-timeline-charts)
  - [6.2 The step detail table](#62-the-step-detail-table)
  - [6.3 Interactions](#63-interactions)
  - [6.4 The capacity heatmap](#64-the-capacity-heatmap)
- [Pointers](#pointers)

---

## 1. Inputs and output

`fplan rates solve` computes the fastest **schedule** for reaching a scenario's
goal — how much of each item to produce, and how many of each machine to have,
across the whole run. It reads a **scenario** (the goal: techs to research, items
to produce, rockets to launch; plus the world you start from), an **L1
tech-order** (the sequence the technologies are researched in), and optionally a
**map** (the resource patches, oil, and water available, which bound how much
extraction can fit). It writes `rates.yaml`: that schedule, plus the single
number it minimizes — **`t_FINAL`**, the total time the schedule takes.

The result is a schedule to reach the scenario goal, not the factory itself.
Downstream stages turn it into the concrete blueprint: L3 places the machines and
L4 emits the action steps a TAS generator replays. This document is about the
schedule — what L2 decides, and how to read it.

## 2. What a result looks like (the visualization)

A plan is a sequence of **steps**. Each step is the interval of time during which
one technology is being researched, followed by a single trailing step (labelled
*FINAL*) that researches nothing and just lets the last production finish. The
boundaries between steps are **tiers** — snapshots of your world at an instant
(tier 0 is what you start with; the last tier is where the scenario goal is met).

Within that timeline the solve decides, for you:

- **when** each technology is researched (the step boundaries),
- **how many** of each machine to have built by each tier,
- **how much** of each item to produce in each step.

The headline `t_FINAL` is simply the sum of all the step durations — the elapsed
time from start to goal.

The easiest way to explore a result is the visualization (`fplan rates viz`).
Here is one item's production isolated from a `default-victory` plan:

![L2 timeline viz: the logistic-science-pack production-rate chart, with the
advanced-material-processing step selected and a hover tooltip reading
0.94/s](images/01_l2_rates_solve_viz.png)

*The `logistic-science-pack` raw production rate across a `default-victory` run.*

Everything you need to read a result is on this screen:

- The **top bar** is the run's summary: scenario, mode, and the solve's headline
  numbers — here `obj=2956.7s` (the optimized `t_FINAL`), `gap=111.8%` (how far
  from proven-optimal — see [§3.1](#31-solver-status-and-solution-quality)), and
  `total=3148.7s` (the absolute clock time
  the plan ends at, because this scenario starts partway into a save).
- The **left bar** lists the steps by the technology each researches and the
  clock time it starts — click one to jump to it.
- The **right bar** lists every item and recipe, grouped — check one to plot it.
  Here only `logistic-science-pack` is checked.
- The **chart** plots the selected item's rate over time. Hovering a step reads
  out the exact value: during the **advanced-material-processing** step
  (≈ 10m30s) the plan produces `logistic-science-pack` at **0.94/s**.

This is the at-a-glance tour; for the full chart set and every interaction, jump
to [§6](#6-the-visualizer-reference-rates-viz).

That single number — 0.94 items per second during one step — is one cell of the
full `rates.yaml`, which §3 reads in full.

## 3. Reading `rates.yaml`

The viz is a view onto `rates.yaml`; the file carries more than any one chart
shows. This section walks the fields you'll actually act on.

### 3.1 Solver status and solution quality

The `l2:` block in the run manifest (and the top bar of the viz) tells you
whether to trust a result before you read its details:

- **`status`** — `optimal` means the solver proved no faster plan exists *within
  L2's model* — under its assumptions and simplifications, not the last word.
  Later stages model more game mechanics and refine this plan (L3 places the
  machines, L4 emits the execution). `timelimit` means it found a working plan
  but hit the time limit before proving it optimal (the `default-victory` run
  above). `infeasible` means the scenario goal can't be met under the current
  inputs.
- **`objective_s`** — the `t_FINAL` it achieved, in seconds (the `obj=2956.7s`
  in the figure). This is a *real, feasible* time: a plan that actually reaches
  the goal in that long.
- **`gap`** — how far the achieved time is from the best lower bound the solver
  could prove (`dual_bound`), relative to that bound. `rates.yaml` stores it as a
  fraction (the figure's run records `gap: 1.118`); the viz shows it as a percent
  (`111.8%`). `0` means proven-optimal; a large gap means the solver has a working
  plan but *can't yet rule out* a much faster one — read `objective_s` as an upper
  bound on what's achievable.
- **`seed`** — which random seed produced this plan. The solver's search is
  randomized across seeds (see [§3.3](#33-why-results-vary)); this field records
  the one behind the result in front of you.

### 3.2 The per-step records

The body of `rates.yaml` is one record per step. Each carries:

- **`duration_s`** — how long the step takes (these sum to `t_FINAL`).
- **`research`** — the technology being researched and how many science cycles
  it costs.
- **`activity`** — which recipes ran, on which buildings, for how many cycles —
  the actual work done in the step.
- **`items`** — one row per item, the heart of the data. Each row gives:
  - `count_start` / `count_end` — inventory at the two tier boundaries;
  - `produced` / `consumed` — totals made and used during the step;
  - `production_rate_per_s` / `consumption_rate_per_s` — those totals as a
    per-second rate. **This is what the viz plots** — the `0.94/s` in the figure
    is `logistic-science-pack`'s `production_rate_per_s` during step 6;
  - `buffer_seconds` — how long the end inventory would last at the current
    consumption rate, a slack signal (large = comfortably ahead; near zero = the
    item is consumed as fast as it's made).
- **`mining_assignment` / `smelting_assignment`** — how drills are split across
  ores and furnaces across products (a machine can't switch mid-run, so each
  gets a dedicated count).
- **`energy` / `player_time`** — the step's electric demand vs. supply, and the
  single character's walking/placing/tree-felling time.
- **`capacity`** — per building, how much of its available machine-time the step
  used: `recipe_seconds_used` against `capacity_seconds`, their ratio as
  `utilization`, and a `saturated` flag when utilization is at the ceiling
  (≥ 0.98). The viz also shows this as a heatmap. It's a per-building signal you
  read directly — the model has thousands of coupled constraints, so no single
  field names "the" bottleneck; high utilization is one place to start looking.

### 3.3 Why results vary

The solve returns *one* good schedule, not *the* schedule. For a hard scenario
like `default-victory` it stops at the time limit (`status: timelimit`) with a
large gap, so the plan it returns is one feasible point among many — and it
shifts when you change the seed, the mode, or any of the many config knobs the
model exposes (capacity weighting, building caps, deployment, the character
stand-in, and more). The solver's search is randomized across seeds, so `fplan
rates solve` runs several and keeps the best; the `seed` field records which one
produced a given plan.

One knob worth reading alongside any result is the **mode** (`mode=trapezoidal`
in the figure), which changes how within-step capacity is credited and so moves
`t_FINAL` — the modes are defined in
[§4.6](#46-downstream-feedback-back-propagation).

Comparing runs is a real way to explore the solution space
([§5](#5-extending-the-solve-asking-your-own-questions)), but a comparison only
means something when you know which knob you changed.

→ *If you only want to read plans, you can stop here. Continue for how the model
produces them and how to change it.*

---

## 4. How the solve works (under the hood)

[§1](#1-inputs-and-output)–[§3](#3-reading-ratesyaml) are enough to use and read
a result. This section opens the hood — the model that produces those results —
for readers who want to know *why* a plan is shaped the way it is, and as the
groundwork for changing it ([§5](#5-extending-the-solve-asking-your-own-questions)).
It stays at the level of *what the model represents*; the exact source lines are
catalogued in the [§5](#5-extending-the-solve-asking-your-own-questions)
references.

### 4.1 Steps, tiers, and the objective

The solver builds the timeline from
[§2](#2-what-a-result-looks-like-the-visualization) directly: one **step** per
technology in the L1 tech-order, in that order, then a trailing **FINAL** step
that researches nothing (`before_recipe` checkpoints can carve out an extra step
or two). Between consecutive steps sit the **tiers** — instantaneous snapshots,
one more tier than there are steps; tier 0 is the initial world, the last tier is
the goal state.

That split is why [§3](#3-reading-ratesyaml)'s fields land where they do: a
**step** owns its `duration_s` and the activity within it (`produced`,
`consumed`, the rates), while a **tier** owns the inventory snapshot
(`count_start` is the count at the step's opening tier, `count_end` at its
closing tier).

The objective is one line — minimize the total elapsed time:

```
t_FINAL = Σ duration[step]
```

In code that's the lone `setObjective` call at the end of `build_lp`
([§5.1](#51-how-the-solver-code-is-organized)). Everything in
[§4.3](#43-the-constraints) is a rule the solver must satisfy while
driving that sum down. Two of its own variables multiply inside the capacity rule
([§4.3](#43-the-constraints)), so the program is **nonconvex and nonlinear**;
SCIP solves it by spatial branch-and-bound over those product terms. All
variables are continuous — there are no integer machine counts to branch on
([§4.4](#44-modeling-assumptions)) — so a scenario's difficulty comes from the
nonconvexity and the sheer number of coupled constraints. That's why
`default-victory` stops at `timelimit`
([§3.1](#31-solver-status-and-solution-quality)) while `steelaxe` proves
`optimal`.

### 4.2 The decision variables

Every number in a result traces back to one of these — the nouns from
[§2](#2-what-a-result-looks-like-the-visualization), now with their exact shape,
and the handles you reference when you add a constraint
([§5.4](#54-variable-reference)):

| Variable | Indexed by | What it is |
| --- | --- | --- |
| `item[n, tier]` | item `n`, tier | Units of `n` in existence at that tier. Buildings are items, so machine counts live here too. Surfaces as `count_start` / `count_end`. |
| `x_real[r, b, step]` | recipe, building, step | Cycles of real recipe `r` run on building `b` during the step. Surfaces as `activity`; drives `produced` / `consumed`. |
| `x_pseudo[name, step]` | pseudo-recipe, step | Cycles of a research / launch / burn pseudo-recipe (see [Pointers](#pointers)). |
| `duration[step]` | step | Length of the step in seconds (0–600). Sums to `t_FINAL`. |
| `drill_assign[ore, tier]` | ore, tier | Electric mining drills committed to that ore. |
| `furnace_assign[out, tier]` | smelted item, tier | Steel furnaces committed to that product. |
| `fuel_burn[fuel, b, step]` | fuel, burner, step | Units of `fuel` burned by burner building `b` in the step. |
| `char_credit[step]` | step | Electric work the player character supplies that step. |

All are continuous and ≥ 0. Building counts additionally carry a map-derived
upper bound the solver needs to stay tractable
([§4.5](#45-solver-specific-choices-and-tractability-hacks-scip)).

### 4.3 The constraints

A full instance has thousands of individual constraints (the `default-victory`
run records about 4,400). What matters for reading and extending is the
**families**, not every member:

- **Material balance** — for each tracked item and step, the count at the next
  tier equals the count now plus what recipes produce minus what they consume
  (and minus fuel burned). Nothing is consumed that wasn't produced or already in
  stock. This backbone links `item[n, tier]` across tiers through `x_real` /
  `x_pseudo`.
- **Capacity** — for each building and step, recipe-cycles run can't exceed what
  the building count provides, roughly `Σ recipe_time · x ≤ count · speed ·
  duration`. The `count · duration` product is the nonlinear term that makes the
  program nonconvex; the **mode** sets whether `count` is read at the step's
  start, end, or a blend ([§4.4](#44-modeling-assumptions)).
- **Unlocks** — a recipe gets a variable only in steps where its technology is
  researched and a host building exists, so it simply *cannot* run before its
  prerequisites do (enforced by omitting the variable, not by a constraint).
- **Goal and checkpoints** — the final tier must meet the scenario goal (items
  produced, rockets launched); intermediate checkpoints impose the same floor at
  an earlier tier.
- **Research and launches** — research is pinned to its step at the exact
  science-cycle count it needs (with a floor on how short a research step can
  be); launches must total the required count across the steps where the silo
  exists.
- **Dedicated machines** — a drill can't switch ore and a steel furnace can't
  switch product mid-run, so `drill_assign` / `furnace_assign` split the totals
  per ore/product, never decrease, and sum to no more than the built count.
- **Space** — with a map loaded, drills, pumpjacks, offshore pumps, and total
  building footprint are capped by the map's tiles, oil spots, water perimeter,
  and area; wood draw is capped by its trees.
- **Energy** — each step's electric demand must be met by character work plus
  boiler burns, and each burner must be fed enough fuel to run its recipes.
- **Player-time** — one character acts serially, so the time to walk to and place
  the step's new buildings (and fell trees) must fit inside the step's duration.
- **Storage** — fluids and banked solids are capped by the pipes, tanks, and
  chests built to hold them.

Each family is the formal version of an intuition from
[§1](#1-inputs-and-output)–[§3](#3-reading-ratesyaml);
[§5.5](#55-constraint-reference) describes them with their game-mechanics meaning
so you can sit a new constraint beside the right one.

### 4.4 Modeling assumptions

The model is a deliberate simplification of Factorio — that's what makes it
solvable, and what later stages refine
([§3.1](#31-solver-status-and-solution-quality)). The assumptions that most shape
a plan:

- **Continuous everything.** Counts and cycles are real numbers, never integers —
  3.4 labs is legal and means "3.4 labs' worth of machine-time." No machine is
  rounded; that's L3's job. The whole program stays a continuous relaxation, with
  no integer branching.
- **Recipes, not items, are the unit of work.** An item with several recipes is
  several variables, and the solver picks among routes; there is no privileged
  "recipe for X."
- **Capacity timing is a mode, not a fact.** Whether a machine built *during* a
  step helps that same step is a per-`--mode` weighting on `count · duration` — an
  assumption about within-step ramp-up with no single right value, so the same
  scenario yields a different `t_FINAL` under each mode. The modes, and what each
  assumes, are defined in [§4.6](#46-downstream-feedback-back-propagation).
- **No spatial layout.** L2 plans *how much* and *when*, not *where*. Placement,
  belts, inserters, and **hand-feeding** (the player carrying items
  producer-to-consumer by hand) belong to L3 and aren't represented here; a few
  constraints stand in for their effects (see the transition caps in
  [§5.5](#55-constraint-reference)).

Each is a place the model trades fidelity for tractability — and therefore a
natural place to ask "what if it were tighter?"
([§5.7](#57-beyond-constraints-objective-recipes-physics)).

### 4.5 Solver-specific choices and tractability hacks (SCIP)

§4.4 simplifies Factorio; this section covers choices made for the *solver*. Some
are clean numerical hygiene. Others are frank **hacks — temporary, not
by-design** — that exist only to keep the hardest scenario (`default-victory`)
solving within roughly ten minutes of wall time. They're labelled below so you
don't read them as intended behavior.

The cost driver is the **number of bilinear terms**: every capacity constraint's
`count · duration` is a nonconvex product, and the more of them, the slower SCIP's
spatial branch-and-bound. Several choices below exist only to keep that count
down.

- **Coefficient ranges are bounded** *(numerical hygiene).* A solver grows
  unstable when one constraint mixes very large and very small numbers. The aim is
  a max/min magnitude ratio within roughly **1e6 across the model** and **1e4
  within any single row or column**; `build_lp` prints the min/max coefficients
  and the worst row ratio to the console so you can tune it. The rescalings serve
  this: energy in **MJ / MW** rather than joules/watts, and storage in
  centi-units — counting a `storage-tank` in hundredths of a tank pulls the
  item-banking row ratio from ~9600 to ~96 and lifts the global coefficient floor.
  The model layer still reports raw joules/watts; the scaling is internal.
- **Finite bounds for the bilinear relaxation** *(numerical).* SCIP relaxes each
  `count · duration` with a McCormick envelope, which needs finite bounds on
  *both* factors or the relaxation is uselessly loose ("cannot guarantee finite
  termination"). So step durations are capped (600 s) and every building count
  carries a map-derived upper bound — kept deliberately **loose**, just enough to
  box the search; a tight "physically true" cap (say, silo ≤ 2) can push the
  feasible region somewhere the nonlinear sub-solver can't get a foothold, and
  SCIP then returns *no* plan at all.
- **Fewer bilinear terms** *(hack — temporary, not by design).* To hold
  default-victory under ~10 minutes, several things are modeled more crudely than
  reality, purely to avoid multiplying `count · duration` terms:
  - *Smelting is collapsed.* Electric-furnace smelting is disabled outright; only
    the steel furnace is split per output; stone furnaces stay pooled (and
    capped). Each extra smelting building would multiply the per-output bilinear
    terms.
  - *The player is a fixed prop, not a facility.* The character is modeled as
    2× assembling-machine-1 (the "stand-in"), not a real variable production
    facility — a proper player-as-facility would add a whole new family of
    bilinear terms.
  - *Modules aren't modeled.* Module and beacon *effects* are a TODO
    (productivity stays 1.0). The rocket phase fakes it: `default-victory`'s
    checkpoint forces the silo's beacons and modules to be *built* — paying their
    construction cost — without applying their speed/productivity effect. General
    module use is omitted entirely, because a moduled machine is effectively a new
    production facility: the same bilinear blow-up as modeling player crafting.
  - *Dead variables are pruned.* Unreachable choices (a fuel you can't make,
    terminal items nothing consumes) are dropped rather than left at zero — fewer
    variables and a narrower coefficient range.
  - *The one big craft is linearized.* Building the single rocket-silo is forced
    onto one machine with a purely *linear* constraint (exact because its
    whole-plan demand is 1), avoiding yet another bilinear term.
- **Some game constraints double as solver stabilizers.** The spatial caps
  ([§4.3](#43-the-constraints)) have a real terrain meaning, but they also stop
  the relaxation from driving a count huge-and-short; without them SCIP finds no
  incumbent on a large scenario. Dropping such a constraint "because the map is
  unbounded" can quietly break the solve.
- **A few SCIP parameters are set directly.** A condition-number warning threshold
  (`lp/conditionlimit`) flags a numerically unhealthy LP; a single randomization
  seed (`randomization/randomseedshift`) varies SCIP's heuristics and branching —
  what multi-seed search exploits ([§3.3](#33-why-results-vary)) — and optional
  time / gap / node limits bound the search.

These hacks are concentrated, not scattered: `solve.py` is the only module that
imports SCIP ([§5.1](#51-how-the-solver-code-is-organized)), so the rest of L2
stays solver-neutral — and the items above are where to start when modules,
proper player modeling, or richer smelting eventually land.

### 4.6 Downstream feedback (back-propagation)

A few of L2's coefficients aren't meant to stay fixed — they're **placeholders
for feedback from downstream**. L2 sets them by naive assumption today, but each
is a single scalar designed to be *informed* later, once L3 models placement and
can report back. Back-propagation is deliberately limited to **simple scalar
coefficients** — numbers flowing back up, not L3 re-deriving L2's model. Giving a
coefficient a home now, even as a constant, is what lets it be **nudged** by
uninformed trial-and-error today and driven by **shadow prices and gradients**
(informed feedback) later.

Two coefficients sit on this seam:

- **The production coefficient — start vs. end of a step.** A machine built
  *during* a step doesn't run for the whole step, so how much of its output should
  count? That fraction is the capacity end-weight, chosen by `--mode`. The three
  modes are **uninformed trial-and-error settings** of this one coefficient — no
  downstream feedback, no data behind them:
  - **`lower-bound`** — the worst case: production is fully delayed for the entire
    step, so a machine built mid-step contributes nothing until the next step.
    This forces durations *longer* than optimal. It's chosen over an *admissible*
    setting (one giving a true lower bound on `t_FINAL`) deliberately — a
    worst-case L2 plan leaves later stages more room to find a feasible layout.
  - **`experimental`** — only *raw-resource* extraction is fully delayed within a
    step (the rationale: long belt delays hauling ore in from outposts);
    everything else counts as available.
  - **`trapezoidal`** — a flat 50-50 compromise (weight 0.5), not based on any
    data.

  Because this coefficient already *exists*, it's a ready back-prop target: nudge
  it now, and eventually set it from L3 — which, once it models placement, knows
  *when within a step* a machine (say a mining drill) comes online, and therefore
  its true within-step production fraction.
- **The player-time movement fraction.** This coefficient **doesn't exist yet**.
  Today player-time is modeled serially — the character can't walk and place at
  once — so movement is some sub-100% slice of each step's player-time, estimated
  from a naive walk formula (`2·√footprint / walking-speed` per building, the
  player-time family in [§4.3](#43-the-constraints)). The *real* fraction depends
  on layout — how far the character actually walks — which only L3 knows once
  placement is modeled. It's the same shape as the production coefficient: a scalar
  L2 should receive from downstream rather than guess, and a natural one to add
  next.

This is why the modes shift `t_FINAL` ([§3.3](#33-why-results-vary)) and why
they're "naive": they're the current, feedback-free settings of a coefficient a
downstream loop is meant to refine.

---

## 5. Extending the solve (asking your own questions)

[§4](#4-how-the-solve-works-under-the-hood) described the model; here you change
things to ask a question of your own. There are two levers: edit the **problem** —
the scenario, no code ([§5.3](#53-example-adding-a-checkpoint-a-scenario-change)) —
or edit the **model** — the solver code
([§5.2](#52-example-capping-the-number-of-labs) and the references that follow).
This section walks one example of each, then the reference you reach into for
changes of your own.

This section is also the **code map** that
[§4](#4-how-the-solve-works-under-the-hood) points into. The split is
deliberate: §4 names a *concept*, this section names *where it lives*, and the
code's own landmarks point the rest of the way. Those landmarks are the section
banners (`# --- … ---`) and the constraint **name prefixes** (`name="cap_…"`,
`"flow_…"`, …) — both far more stable than line numbers, which drift with every
edit. When a pointer below says `cap_`, grep for it.

### 5.1 How the solver code is organized

One function builds the whole program: **`build_lp(...)` in
`src/fplan/l2/solve.py`** — the only module that imports the solver. It creates
the variables, adds every constraint, and sets the objective in one place,
**by design**: the data is coupled, so the variables are in scope exactly where
the constraints that use them are written. You extend the model by editing this
function in place, next to the family your change belongs to — not by registering
a callback that hides the context you need.

`build_lp` reads top to bottom through banner'd sections:

```
# --- decision variables ---                          (§5.4)
# --- fuel allocation for non-boiler burner buildings ---
# --- flow constraints ---                            material balance
# --- capacity constraints (one per (building, step)) ---
# --- single-machine constraint ... ---
# --- electric-mining-drill: per-ore assignment ... ---
# --- steel-furnace: per-output assignment ... ---
# --- spatial caps ... + infrastructure reservation ---   (caps, player-time, storage live here)
# --- energy balance: per-burner-building (fuel) and per-step (electric) ---
m.setObjective( Σ duration )                          the §4.1 objective
```

Supporting modules: **`instance.py`** builds the `L2Instance` (the steps and
tiers from [§4.1](#41-steps-tiers-and-the-objective), and the **mode** weighting
via `capacity_end_weight`); **`config.py`** holds the tunable knobs
([usage.md](usage.md)); **`pseudo_recipes.py`** defines the research / launch /
burn recipes.

### 5.2 Example: capping the number of labs

This is a **code** change — a new constraint in the model. (It illustrates the
shape; the specific outcome below hasn't been measured on a solved run.) Suppose
you want to ask: *what if I never have more than N labs?* — does the plan
still reach the goal, how much later, and what reshapes to compensate?

You don't need anything new. The model already has constraints of exactly this
shape — the burner-drill and stone-furnace transition caps are each just
`item[building, tier] ≤ cap`. Copy the stone-furnace one and point it at `lab`,
placing it beside the others in the spatial-caps section (the `*_cap` block):

```python
# Example extension: cap labs at N at every tier.
LAB_CAP = 10.0
if "lab" in inst.reachable_buildings:
    for i in range(n_tiers):
        if ("lab", i) not in item_vars:
            continue
        m.addCons(
            item_vars[("lab", i)] <= LAB_CAP,
            name=_safe(f"lab_cap_{i}"),
        )
```

Then re-solve and read the result against a baseline:

```bash
fplan rates solve <run>          # re-solve with the cap in place
fplan rates viz <run> --open     # read what changed (§2, §3)
```

What to look for, using [§3](#3-reading-ratesyaml): the tiers where `item[lab]`
now presses against `LAB_CAP`; research steps that lean on labs stretching their
`duration_s` (the capacity rule has less machine to work with); and other
buildings picking up `utilization` as the plan compensates. That's the loop —
**hypothesis → constraint → re-solve → diff** — and it's the whole method for
probing new solutions. (Pick `N` by reading a baseline first: capping something
that isn't binding changes nothing.)

### 5.3 Example: adding a checkpoint (a scenario change)

Not every lever is in the solver code. The **scenario** is the problem
description, and editing it reshapes the plan without touching the model. The
clearest example is a **checkpoint** — a required intermediate state.

A checkpoint with a `before_recipe` trigger carves out a step at the latest point
a recipe could run, forbids that recipe in earlier steps, and applies a
lower-bound floor at the start of the carved step — in plain terms, *"before you
may start making X, you must already have Y on hand."* You add one in the
scenario YAML; no code changes:

```yaml
# scenarios/default-victory.yaml
checkpoints:
  - name: "bank science before committing to rockets"
    trigger:
      kind: before_recipe
      recipe: rocket-part
    requires:
      items:
        automation-science-pack: 50
        logistic-science-pack: 50
```

This forces 50 each of red and green science to exist before the first
`rocket-part` is built, and pushes all rocket-part production into the carved step
and beyond. Re-solve and read the result as before:

```bash
fplan rates solve <run>
fplan rates viz <run> --open
```

You'd expect production to front-load to meet the science floor, with `t_FINAL`
shifting to accommodate the staged buffer. Because this changes the *problem*,
not the *model*, it composes with code changes — you can cap labs (§5.2) and
require a buffer (here) in the same run.

(`default-victory` already ships a richer version of this checkpoint; the full
`trigger` / `requires` schema is in `src/fplan/scenario.py`.)

### 5.4 Variable reference

Where each variable from [§4.2](#42-the-decision-variables) is created in
`build_lp`, by name prefix:

| Variable | Section banner | Name prefix |
| --- | --- | --- |
| `item[n, tier]` | `decision variables` | `item_` |
| `x_real` / `x_pseudo` | `decision variables` | `x_` |
| `duration[step]` | `decision variables` | `duration_` |
| `fuel_burn` | `fuel allocation …` | `burn_` |
| `char_credit` | `fuel allocation …` | `char_credit_` |
| `drill_assign` | `electric-mining-drill: per-ore …` | `drill_` |
| `furnace_assign` | `steel-furnace: per-output …` | `furnace_` |

### 5.5 Constraint reference

Each constraint family from [§4.3](#43-the-constraints), what it means in
Factorio's mechanics, and the `name=` prefix to grep for it in `build_lp`:

- **Material balance** (`flow_`, `init_`) — Each item's count carries across
  tiers: next = now + produced − consumed (− fuel burned), and nothing is used
  that wasn't made earlier or in the starting inventory. *In game:* gears can't
  be assembled until the iron plates they eat have been smelted (or were on hand
  at t₀) — there is no iron from nothing.
- **Capacity** (`cap_`) — A building crafts at a bounded rate set by its speed; a
  step's recipe-cycles can't exceed `count × speed × duration`. *In game:* an
  assembling-machine-1 (speed 0.5) on a 0.5 s recipe makes about one item per
  second, so 10/s needs ~10 machines — "produce more" means build more (or
  faster) machines, or take longer.
- **Unlocks** (no constraint — enforced by *omitting* the variable) — A recipe
  has no variable in steps before its technology is researched and a host
  building exists. *In game:* you can't assemble electronic circuits before
  researching them; there's nothing to grep because the option never exists.
- **Dedicated machines** (`cap_drill_*`, `drill_total_`, `drill_mono_`,
  `no_mine_`, `cap_furnace_*`, `furnace_total_`, `furnace_mono_`) — A mining drill
  works one ore patch and a steel furnace smelts one product; the model commits
  them per ore/product, never decreasing, summing to no more than built. *In
  game:* drills on iron can't pivot to copper next step, and furnaces busy on
  iron-plate aren't smelting copper-plate at the same time. (Electric-furnace
  smelting is disabled and only steel furnaces are split — a tractability hack,
  [§4.5](#45-solver-specific-choices-and-tractability-hacks-scip).)
- **Goal & checkpoints** (`floor_`, `ckpt_`) — The final tier must meet the
  scenario goal; a checkpoint
  ([§5.3](#53-example-adding-a-checkpoint-a-scenario-change)) forces an
  intermediate floor at a carved boundary. *In game:* "launch 1 rocket" forces
  ≥ 1 launch by the end; a checkpoint can force "50 of each science pack banked
  before the first rocket-part."
- **Research & launches** (`research_`, `research_cycle_floor_`, `launch_`) —
  Research draws science packs through labs over the tech's research time, and no
  number of labs shrinks a research step below one science cycle; each launch
  consumes 100 rocket-parts in the silo. *In game:* stacking labs speeds research
  only down to one cycle's length; a launch needs a full 100-part rocket first.
- **Single-machine craft** (`singlecraft_`) — Some singleton builds run as one
  indivisible cycle on one machine, not parallelized across fractional machines.
  *In game:* the lone rocket-silo is assembled over one full craft — you can't
  halve the time with "twice the machines." (Written as a *linear* constraint to dodge
  a bilinear term — [§4.5](#45-solver-specific-choices-and-tractability-hacks-scip).)
- **Transition caps** (`burner_cap_`, `stone_furnace_cap_`) — Caps on burner
  mining drills and stone furnaces that stand in for a mechanic L2 doesn't model:
  **hand-feeding**. Early on the player carries items from producer to consumer by
  hand rather than laying belts; because L2 models no placement or location
  ([§4.4](#44-modeling-assumptions)), it can't represent hand-feeding directly. The
  burner-drill cap sits around the crossover where building belt infrastructure
  becomes more efficient than hand-feeding — past it the plan must move to electric
  drills (and, for smelting, steel furnaces). *In game:* you can't hand-feed an
  unbounded number of burner drills; beyond a point you'd lay belts.
  (Mechanically the same `item ≤ cap` shape as the lab cap in
  [§5.2](#52-example-capping-the-number-of-labs).)
- **Space** (`space_`, `map_area_`, `oil_spots_`, `water_pumps_`) — With a map
  loaded, density is bounded by terrain: drills by ore tiles, pumpjacks by oil
  spots, offshore pumps by water edge, all buildings by buildable area, wood by
  trees. *In game:* one pumpjack per oil spot, and only so many drills fit on a
  given ore patch.
- **Storage** (`fluid_buffer_`, `item_banking_`) — You can hold only what you
  have containers for: fluids by pipes and tanks (a storage-tank holds 25 000),
  solids by inventory and chest slots (player 80, iron-chest 32, steel-chest 48),
  scaled by each item's stack size. *In game:* banking 10 000 iron-plate (stack
  100) is 100 slots — past the 80-slot inventory, so you'd build chests.
- **Infrastructure reservation** (`infra_`) — Deployed machines reserve the
  supporting entities they need (belts, inserters, power poles), which must be
  produced too. *In game:* every electric drill you place ties up the belts and
  poles that carry its ore and feed it power.
- **Player-time** (`player_time_`, `player_space_`) — One character acts
  serially: walking to sites, placing entities, and chopping trees must all fit a
  step's duration. *In game:* you can't place 200 machines instantly — the
  walking and placing time is real, and if it overflows the step, the step
  lengthens.
- **Energy** (`fuel_energy_`, `electric_balance_`, `char_credit_*`) — Machines
  need power: burners (stone/steel furnace, burner drill) eat chemical fuel, and
  electric machines draw from generation (boilers + steam engines, plus the
  character's small contribution). *In game:* 50 electric drills run only if
  enough coal-fed boilers and steam engines cover their combined draw.

### 5.6 Assumptions a new constraint must respect

The model's conventions ([§4.4](#44-modeling-assumptions)) are easy to violate by
accident. Before you trust a new constraint:

- **Guard variable existence.** Variables are pruned by reachability and unlocks,
  so not every `(name, tier)` exists. Check `if (name, i) not in item_vars:
  continue` first — exactly what the cap template does — or you'll `KeyError` (or,
  worse, silently reference nothing).
- **Guard map-dependent assumptions.** Spatial caps only apply when a map is
  loaded; gate anything that assumes resources on `inst.reachable_buildings` /
  the map fields, the way the existing caps do.
- **Mind rescaled units.** Energy is in MJ/MW internally, and chests/tanks are
  rescaled ([§4.5](#45-solver-specific-choices-and-tractability-hacks-scip)); don't
  mix raw and rescaled quantities in one expression.
- **Counts are continuous.** `item[building, tier]` is fractional — a cap of
  `10.0` is a real bound, not "10 machines." Don't write logic that assumes
  integers.
- **Capacity reads through the mode.** If your constraint touches capacity-like
  terms, remember the effective count is the mode's start/end/blend weighting,
  not a single tier's count.

### 5.7 Beyond constraints: objective, recipes, physics

Adding a constraint is the gentle first step. The deeper levers:

- **Change the objective** ([§4.1](#41-steps-tiers-and-the-objective)). Edit the
  `setObjective` call to minimize something other than pure `t_FINAL` — player
  actions, a weighted blend, peak machine count. This changes what "best" *means*
  and is the highest-leverage edit.
- **Add a pseudo-recipe** (`pseudo_recipes.py`). Model a new *activity* that
  consumes capacity and items without being a crafting recipe — hand-crafting,
  manual mining — the same seam research / launches / burns already use.
- **Forbid or force a route.** The `before_recipe` checkpoint machinery already
  restricts a recipe to a carved step; generalizing it to forbid a recipe
  outright (or pin a route) lets you ask *"is route X actually faster?"* — the
  most direct way to discover new strategies, since the recipe-native model
  ([§4.4](#44-modeling-assumptions)) already carries every alternative route.
- **Add new physics.** Modules and beacons (the `Facility` wrapper exists for
  this), logistics, quality — the heaviest changes, adding both variables and
  constraint families. Frontier work, not a first extension.

---

## 6. The visualizer reference (`rates viz`)

[§2](#2-what-a-result-looks-like-the-visualization) introduced the viz at a
glance; this is the reference — the charts it draws, the step detail table, and
every interaction. `fplan rates viz` writes two views: the **timeline** (the
default, below) and a **capacity heatmap** ([§6.4](#64-the-capacity-heatmap)).

### 6.1 The timeline charts

Three charts stack above one shared, zoomable x-axis (absolute clock time). Each
visible item is one coloured line; the legend ([§6.3](#63-interactions)) controls
which are shown.

- **Raw production rate** (item/s; Power in MW) — how fast each item is produced
  in each step (the figure in
  [§2](#2-what-a-result-looks-like-the-visualization)).
- **Net production rate** (production − consumption) — above zero the item is
  accumulating, below zero it's being drawn down, at zero it's made and consumed
  in lockstep.
- **Surplus count over time** — the running stockpile (inventory) of each item.

![Net production rate and surplus-count charts with the step-6 detail
table](images/02_l2_rates_solve_more_viz.png)

*Net production rate and surplus count for `logistic-science-pack`, with step 6
(advanced-material-processing) selected — its numbers are in the detail table.*

### 6.2 The step detail table

Below the charts sit the selected step's numbers. A header gives the step label,
its clock span, and duration; then one row per visible item: `prod /s`,
`cons /s`, `net /s`, `count start`, `count end`, `Δ count`. In the figure, step 6
shows `logistic-science-pack` at prod 0.94 / cons 0.94 / net 0.00 with count
start and end both 0 — made and consumed in lockstep, so nothing stockpiles.

Clicking an item cell opens a **production-facility breakdown**: per recipe and
building, the number of facility-equivalents running it and its rate, with a
total. (It reads "facility data unavailable" when the game model wasn't loaded —
`rates viz` loads the model only best-effort.)

### 6.3 Interactions

- **Legend (right).** Check or uncheck an item to show/hide its line in every
  chart; click a category header to collapse it; **All**, **None**, and
  **Top 10** set the visible set at once.
- **Technology list (left).** Click a technology to recenter the timeline on its
  step and select it. Selection highlights that step's boundary across the charts
  and fills the detail table; conversely, selecting a step by clicking a chart
  scrolls the matching technology into view — the two stay in sync.
- **Charts.** **Hover** for a tooltip (the step, the cursor time, and the top
  visible items' values for that chart); **click** to select the step under the
  cursor; **scroll** to zoom the time axis at the cursor; **drag** to pan;
  **hover a line** to thicken it. The indicator in the top bar shows the current
  time range and zoom level.

### 6.4 The capacity heatmap

A second file (`rates-heatmap.html`) renders the per-building capacity
`utilization` ([§3.2](#32-the-per-step-records)) as a grid — building × step,
brighter where a building ran closer to saturated. **What to actually read from
it is still open.** With thousands of coupled constraints a saturated cell isn't
necessarily *the* bottleneck (§3 deliberately avoids that claim), so the heatmap
is for now an exploratory view — we haven't yet settled which insights it
reliably supports.

---

## Pointers

- **Pseudo-recipes** (research / launch / burn): `src/fplan/l2/pseudo_recipes.py`.
- **Config knobs** (modes, deployment, caps, character): see
  [usage.md](usage.md) and `src/fplan/l2/config.py`.
- **The post stage** (flattening, the L2→L3 hand-off): currently
  [L2 rate-flattening](L2-rate-flattening.md).
- **Authorship rules** (binding invariants for changing the code): `CLAUDE.md`.
