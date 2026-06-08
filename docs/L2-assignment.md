# L2 — facility assignment

A facility is committed to **one job**: a mining drill to one ore, a furnace to
one product, an assembler to one recipe. This note records how that's modeled,
why the three classes share a shape but differ in mechanics, and the
tractability trade-off that shapes the crafting split.

Assignment exists for **L3**. L3 placement (a VLSI-style layout) needs *static
blocks* — a fixed set of machines each dedicated to a job — not a pooled
capacity that silently swaps what it makes between steps. That swapping also
rarely happens in real runs: the player isn't standing next to a machine to
re-set it, and few recipes share the exact input ratios that would make a swap
free. So L2 commits facilities up front and hands L3 blocks it can place.

## One concept, three mechanics

Every class replaces a building's single **pooled** capacity (one bilinear
`count · duration` term per step) with **per-key** capacity buckets that sum to
the building's count — `<building>@<key>`, keyed on the job. They differ only in
how a bucket may change between steps, which follows the facility's physics:

| Class | Key | Buildings (default) | Temporal rule |
| --- | --- | --- | --- |
| Mining | ore | electric- / burner-mining-drill | strict non-decreasing |
| Smelting | output | stone- / steel-furnace | non-decreasing, **+ teardown** if consumable |
| Crafting | recipe | assembling-machine-1 / -2 | **repurposable** at a player-time cost |

The config block (`assignment:` in `resources/l2-defaults.yaml`) sets the
buildings per class; an empty `buildings` list disables a class. See the
[usage reference](usage.md#facility-assignment) for the knobs.

### Mining — strict non-decreasing

A drill sits on an ore patch and can't change what it mines, so a
`drill[b, ore, tier]` bucket only ever grows: a drill placed on an ore stays
there. Buckets sum to the building count and are capped per ore by how many
drills physically fit on that ore's patch (`tile_pool / footprint`). Neither
drill is consumed by any recipe, so there's no teardown. Applied to **both**
electric and burner drills (burner drills were previously a pooled bootstrap;
splitting them gives L3 real per-ore counts and the coupling below a handle).

### Smelting — non-decreasing, with a teardown for consumable furnaces

A furnace smelts whatever its input belt feeds, so it's committed per output the
same way. One wrinkle: **stone furnaces are consumed** — a boiler's recipe and a
burner drill's recipe each eat one. A consumed furnace is picked up and
destroyed, not repurposed, so its bucket must be able to *shrink*; a strict
non-decreasing rule would forbid the very bootstrap of smelting on stone furnaces
and then cannibalizing them. So a **consumable** furnace gets per-output
`destroy` vars that relax monotonicity by at most the step's real consumption.
Whether a furnace is consumable is read from the recipe ingredients
(`_building_consumers` in `fplan.l2.solve`), never hard-coded — steel furnaces
have no consumer and stay strictly non-decreasing.

A **bootstrap 1:1 coupling** ties the two: in the hand-placed starter base a
burner drill feeds a stone furnace ~1:1, so every `burner-mining-drill@<ore>`
requires at least one `stone-furnace@<plate>` on the product that ore smelts
into (the ore→plate map is derived from the smelting recipes). It's a lower
bound enforced every tier; it goes slack once burner drills phase out under their
cap and electric drills take over.

### Crafting — repurposable at a player-time cost

Setting a real assembler to a recipe is a player action, and so is changing it.
So unlike drills and furnaces, an assembler **can** repurpose — late in a run,
once research finishes, many assemblers move from science-pack crafting to
rocket-part materials — but it pays for it. Each listed assembler splits into a
pooled `unassigned` count plus per-recipe `assigned[b, r, tier]` buckets, with
`unassigned + Σ assigned == count`. Transitions per step are charged to the
serial player-time budget:

- **assign** (`unassigned → r`): one game tick (reuses `placement_tick_s`),
- **unassign** (`r → unassigned`): `unassign_cost_s` (walk back + clear; a knob),
- **destroy** (`r` consumed as an ingredient, AM1→AM2→AM3): **free**, capped by
  the step's real consumption — the clean equivalent of "build AM2 from any
  assigned AM1" without a recipe-variant explosion.

All transition vars carry the building-count upper bound: assign and unassign sit
on opposite sides of the balance, so without a finite cap they form a free ray
(both → ∞ together) that wrecks SCIP's LP relaxation. Because the switch cost is
what makes a dedicated recipe meaningful, crafting assignment is withheld when
player-time isn't modeled (`--no-player-time`); mining and smelting, which carry
no player-time cost, stay on.

## Why the crafting split is curated

The bilinear-term count — not the variable count — is SCIP's cost driver, and a
per-recipe bucket is a bilinear capacity term per step. Splitting **every**
recipe on the assemblers is intractable (factorio_explore measured the root LP
never clearing). So the crafting split is confined to where dedicating a machine
actually shapes the production curve: every `*-science-pack`, plus a curated
`split_items` list of higher-value, lumpier intermediates (engine-units,
inserters, belts, pipes, boilers, steam-engines, and the drill/furnace builds
themselves). Everything else on an assembler stays in the pooled `unassigned`
capacity (one bilinear term). AM3 is intentionally left unsplit — it's terminal
and carries the rocket-silo module hack.

## Building retirement

An assembler whose successor is unlocked is dead weight: by the time
low-density-structure is researched, plans have upgraded AM1 → AM2/AM3 (observed
directly), so AM1's recipe/step variables only ever sit at zero. `crafting.retire_after`
(building → tech) drops them once the tech is researched — realism-free var/bucket
pruning that shrinks the curated split where it's largest. On default-victory it
removes the **~228 post-LDS AM1 buckets (~11% of all bilinear)**, taking the model
from ~2,018 to ~1,783. It's applied in the solver's `x_real` construction,
independently of whether the crafting split is active.

## Tractability

On `default-victory` (47 planning steps) the three classes raise the bilinear
(nonconvex) constraint count from **844** (mining + smelting on the single
electric drill / steel furnace only) to **~1,783** with burner drills, stone
furnaces, and the curated crafting split added (after AM1 retirement; ~2,018
without it) — within the range the HiGHS-barrier backend solves, though the model
is **seed-sensitive** at this size: a multi-seed search (the standard workflow)
finds a feasible primal on a subset of seeds, the best ~comparable to the
pre-assignment baseline. The levers, in order: `crafting.retire_after`, then
`crafting.split_items`, never the temporal rules. See [the solve
doc](L2-rates-solve.md) for the barrier-vs-simplex story. The result is a
feasible/time-limited primal, not a proven optimum.

## Output

Each step emits `mining_assignment`, `smelting_assignment`, and
`assembler_assignment` records — `<building>@<key>` with `count_start` /
`count_end` — mirroring each other so L3 reads one shape. The crafting records
carry `repurpose_penalized: true` to flag that the commitment can move between
steps (the drill/furnace commitments can't). When burner drills are split they
carry real per-ore counts in `mining_assignment`; the older
`burner_mining` drill-*equivalents* field is emitted only as a fallback when
burner drills are left pooled.

## Not yet migrated

factorio_explore additionally **pins** the tier-0 buckets to the hand-placed
starter base's recorded breakdown (e.g. 14 burners on iron-ore, 14 stone
furnaces on iron-plate). That needs a per-building assignment breakdown on the
scenario's initial state, which `scenario.InitialState` doesn't yet carry; until
it does, t₀ buckets are free.
