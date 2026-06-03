# L2 rate-flattening (`fplan rates post`)

> **Scope.** `fplan rates post` is the L2 → L3 (layout-stage) post-processing
> stage and is still under development — it is expected to grow more
> operations. This document covers its **current** operation: rate-flattening.
> Don't read "post" as a synonym for "flatten"; flattening is one thing post
> does today.

**What this is.** A *post-solve* transform of an L2 output (`rates.yaml`)
that estimates how few times a TAS player must revisit an assembler to
re-allocate machines, by replacing each item's jagged per-step production
rate with the smoothest constant-rate-per-segment schedule that still
meets every deadline. It does **not** change the solve or `t_FINAL`; it
reshapes production in time and reports where smoothing is possible and,
crucially, where it is *not*. The math lives in `fplan.l2.flatten`; the
diff visualization is rendered by `fplan.l2.viz` and auto-detected by
`fplan rates viz`.

For the upstream solve this post-processes — the model, `rates.yaml`, and how to
read it — see [L2 rates — the solve](L2-rates-solve.md).

## Provisional by design

`rates post` writes `runs/<run>/rates-post.yaml` — the temporary **L3
input**. Two things are intentionally temporary and must not be relied on:

1. **The output is the temporary L3 input.** L3's preferred input format
   isn't decided yet; we're still characterizing the L2→L3 data.
2. **The output *schema* is temporary too.** It mirrors the `rates.yaml`
   schema (same step/item shape) with the *flattened* production
   characteristics substituted in, plus a sibling `post:` metadata block
   (tagged `schema: provisional-rates-mirror`, the marker `rates viz`
   auto-detects). This is a placeholder chosen because it's the format we
   already have.

Concretely, `post` rewrites **production only** — each item's
`production_rate_per_s` and `produced` become the flattened values;
consumption and inventory (`count_start`/`count_end`) pass through from
the solve unchanged. The divergence between flattened production and the
solve's inventory is exactly what the unmet-input report quantifies. Don't
build anything downstream that assumes this schema is stable.

## Why it matters

Each change in an item's production rate is a point where the player must
walk back to the relevant assemblers and re-allocate machines (change
recipe / add / remove). That is real player-time, and in a WR TAS it must
be minimized. L2 emits step-*averaged* rates that can swing wildly between
adjacent steps; many of those swings are *unforced* — the item was merely
building surplus — and can be flattened away. The headline output is, per
item, **#revisits** = the number of distinct constant-rate segments the
schedule can be collapsed to (shown in the viz legend as `↻N`).

## The causal tube (the key game-mechanics constraint)

The naive instinct — "flatten the curve" — is *wrong* if read as "make it
globally flat," because Factorio cannot produce ahead of causality. You
cannot build blue-science assemblers before the tech is researched, before
their machines exist, or before their inputs exist. The original L2 solve
already encodes all of that, so **its running-total production `P_orig(t)`
is a hard upper bound**: a flattened schedule may never produce *more by
time t* than the solver already proved feasible.

So for item *I*, with `P[k]` = original running-total units produced by
step boundary `k` and `inv[k]` = inventory there (authoritative
`count_end`), the flattened curve must live inside the **tube**

```
R[k] := P[k] - inv[k]   <=   P'[k]   <=   P[k] =: P_orig[k]
P'[0] = 0,   P'[N] = P[N],   P'  monotone nondecreasing
```

- **lower bound `R[k]`** — running-total requirement (demand − initial
  inventory). Touching it means inventory hits 0; `R[k] ≈ P[k]` marks a
  **hard deadline**. Staying above it ⇒ never stock out.
- **upper bound `P_orig[k]`** — never produce earlier than the solver did
  ⇒ respects unlocks, capacity ramp, input availability.

Where the original ran just-in-time the tube is **pinched** (`R =
P_orig`, no flattening possible); where it carried surplus the tube
**opens** and the rate can be smoothed inside it. Because L2's rates are
constant within a step, both bounds are piecewise-linear with breakpoints
only at step boundaries, so the tube constraint at boundaries is **exact**.

## Three flattening rules (`--method`)

The CLI default is **`chord`** — it collapses to the fewest revisits (the
headline metric), and its self-stockouts are surfaced as the unmet-input
report (the informative payload, not a bug). `tube` is the zero-stockout,
strictly-feasible alternative.

- **`chord`** (default) — the first-instinct rule: straight chords between
  consecutive surplus-zero deadlines (and the endpoints), **ignoring the
  tube**. Fewest revisits, but a chord can dip below `R` (self-stockout)
  *or* rise above `P_orig` (impossible front-loading); both are counted and
  reported. `tube` has zero self-stockouts by construction, `chord` does not.

- **`tube`** — the Euclidean **taut string** through the tube
  `[R, P_orig]` from `(t0, 0)` to `(tN, P[N])`. The smoothest feasible
  schedule that **neither stocks out (≥ R) nor front-loads past causality
  (≤ P_orig)**. A shortest path in a corridor turns only at corners, so it
  is computed exactly as a DAG shortest path over the gate corners (N ~ 48
  ⇒ trivially fast). #revisits = #segments of the taut string.

- **`mrp`** — cross-dependency flattening (MRP-style). Smoothing starts at
  science (whose demand is exogenous research draw) and propagates
  *backward* through the recipe graph: each item is flattened to meet its
  *consumers'* already-flattened demand plus its own exogenous draws. Run
  as a Jacobi fixpoint (tolerates the gear→assembler→gear feedback loop).
  Per level it uses the chord flattener, so it inherits chord's
  science-completion deadlines (science revisits = chord) while
  intermediates get *fewer* revisits (their demand is now smooth). Fluids
  and raw mined/pumped items are excluded (already rate-pinned by
  non-fungible drills). v1 = aggregate-curve revisits. It is a low-revisit
  **stage-1**: like chord it can still self-stock-out, leaving a
  minimal-perturbation repair as a deliberate following stage.

> Superseded: an earlier `envelope` rule (the global least-concave-
> majorant of `R` alone, *without* the `P_orig` ceiling) was removed — it
> front-loaded production across unlock boundaries (e.g. "build all
> blue-science assemblers at t0"), which is physically impossible and
> unusable downstream. The `P_orig` upper bound is exactly what it was
> missing.

## Area conservation is a correctness invariant

Every method anchors each item's final running-total to the **original
total production `P[n]`**, so the area under the rate curve is conserved
per item — flattening only *redistributes* production in time, never
changes how much is made. This matters beyond tidiness: a plan whose
totals match the original is **adaptable** — imperfect by nature, but only
a slight real-world adjustment away from working. A plan whose area drifts
is **unadaptable** — it is no longer making the right amount of anything.
(An early `mrp` bug anchored to *propagated demand* instead, which
silently dropped building accumulation — assemblers/drills are produced
but never consumed, so their "demand" read as ~0 — and under-produced
iron-plate by ~38%. Anchoring to `P[n]` while letting propagated demand
reshape only the *interior* fixed it: totals conserve to 0.00%.)

## The unmet-input report (buffer-aware, running-total)

The signal is whether the flattened plan ever falls behind the demand it
must serve — **measured as a running total, not by instantaneous rate** —
so that buffers built up earlier are correctly credited. For each input
item and each step boundary `b`, compare:

- **required** = the raw L2 solution's running-total requirement by `b`,
  `R[b] = P_raw[b] − inv[b]` (= running-total consumption − initial
  inventory). This is how much *must have been created* by then.
- **made** = the flattened plan's running-total production by `b`.

A **shortfall** `required − made > 0` means the flattened plan has, in
total, made fewer of that input than were needed by then — inventory would
have gone negative. Each `(step, consuming recipe, input item)` with a
shortfall is a line, sorted by step then recipe. By construction `tube`
reports zero (it never drops below `R`); `chord` and `mrp` report where
their coarser schedules fall behind. The model is required for this report
(it provides the recipe→ingredient map) and for the `mrp` explosion —
which is why `rates post` needs a configured `data_dir`, unlike the
model-optional `rates viz`.

Why running-total beats rate: an instantaneous "demand-rate > supply-rate"
test fires on *intentional banking* (a flow fed from a reserve has supply
rate 0 but is perfectly fine). The running-total integral only fires when
the reserve is actually exhausted — the real stockout. These shortfalls
are the **payload, not a bug to fix here**: they mark where smoothing is
genuinely impossible (accept a revisit or pre-build a buffer); resolving
them is the follow-up repair stage's / L3's job.

## The visualization

`rates post` auto-generates a diff viz (`--no-viz` to skip), and `rates
viz --from rates-post.yaml` regenerates it on demand. The viz is a **pure
renderer**: it reads the flattened series and the persisted `post:`
diagnostics from `rates-post.yaml` plus the original series from the
referenced source `rates.yaml` (the `post.source` field) — **no
re-flattening**, and the game model is loaded only *best-effort* (to
enrich the legend's facility counts), so it still works without a Factorio
install. It reuses the timeline template with a single
overlay panel (faint = original, solid = flattened) and swaps the
step-detail table for the unmet-input table; the legend annotates each
item with its `↻N` revisit count.

## Scope / limitations (v1)

- The unmet check compares flattened production against the **raw**
  solution's running-total requirement (its known-feasible consumption
  schedule). If the *consumers* are also flattened to draw later, this can
  flag a shortfall that the shifted consumption would actually tolerate; a
  fully self-consistent version would compare against the *flattened*
  consumption. Raw is the deliberate v1 reference (simpler, and the
  feasibility bar the original already cleared).
- The deficit check is **one-shot** (fixed deadlines from the original
  solve), not an iterative re-balance — by design. The deficits are the
  payload (where smoothing is impossible), not something to solve away.
- Fuel (coal-as-fuel) draws are not yet in the deficit check — only recipe
  `ingredients`. Fuel starvation under flattening is future work.
- The `P_orig` upper bound forbids producing earlier than the original *as
  a running total*, but within an open tube window the taut string can ask
  for a higher instantaneous rate than the original peak; if the original
  was capacity-limited inside that window, that rate may not be physically
  installable. A per-step capacity ceiling is the natural tightening.
- Multi-output recipes use `outputs[0]` as the principal for the flatten
  factor.
- Revisits are counted on *continuous* rate changes; quantizing by integer
  machine count (a change too small to add/remove a machine is not a real
  revisit) is a planned refinement.

## Open question

Whether the per-item #revisits should eventually feed back into L2's
player-time budget as a cost term (making the solve itself prefer smooth
trajectories) or stay a pure report. This leans report-first — L1 owns
ordering — but the metric this tool produces is exactly what such a cost
term would need.
