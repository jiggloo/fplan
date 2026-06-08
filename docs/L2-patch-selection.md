# Patch selection — the supply curve and the L2 feedback input

L2 is **geometry-blind**: it pools all patches of a resource into one tile budget
and caps drills at `tile_pool / footprint`, so its `drill@<ore>` lock is an
*ore*-lock, not a *patch*-lock. Which patches actually carry those drills — a few
large patches far away, or many small ones nearby — is a fixed-charge
facility-location trade no single rule settles. **Patch selection** lets you make
that call by eye and feed it back: choose patches against the real demand,
export, and re-solve L2 against exactly that patch set.

Two pieces: the **supply-curve view** (`rates viz`) that shows the trade and
exports a selection, and the **patch-selection input** that L2 reads back.

## The supply-curve view

`rates viz` writes `runs/<run>/viz/<stem>-supply-curve.html` alongside the
timeline and heatmap (it needs the run's bound map; it's skipped with a note if
the map is unavailable, and suppressed with `--no-supply-curve`). Three linked
regions:

- **Map** — every patch at its real centroid (ore + oil clusters), water and oil
  as context, clickable to select/deselect. Origin lines from selected patches to
  spawn for orientation; zoom clamped so the map never shrinks below the pane.
- **Right pane** — a grouped table (group = resource, with the solve's peak miner
  demand and a selected-capacity sufficiency check; rows = patches with capacity,
  distance, density). Click a group header to collapse it.
- **Chart** (one resource via the dropdown) — miner count vs. time (absolute
  in-game time, matching the timeline axis). Solid = **built** miners (committed),
  faint = **utilized** (actually running), a brown dashed line = **burner-drill
  equivalents** (the bootstrap contribution), and red dashed horizontals = the
  **cumulative capacity of the selected patches, stacked by distance**. When the
  demand line rises above the *k*-th horizontal, the *k* closest selected patches
  no longer suffice.

Units are per-resource: **drills** (`capacity = tile_count / footprint`) for ore,
**pumpjacks** (one per oil spot) for crude-oil. `density` (= `tile_count /
bbox_area`) is surfaced because the capacity estimate is a ±30% ballpark — a
low-density patch packs fewer drills than its tile count implies; a true per-patch
drill layout is L3's job.

### It reads the solve, not the model

The view is a **pure consumer** of the run's `rates.yaml` and the bound map — it
**never loads the game model**. Demand comes from the solve output
(`mining_assignment` built drills, `capacity` utilization, `burner_mining`), and
the tiles→drills footprint comes from the solve's **`spatial:`** block (the
deployed footprint the LP actually capped against). So the capacity lines match
the caps the solve enforced — no recomputation, no drift, and no Factorio install
needed. The *utilized* line is `recipe_seconds_used / (base_speed · duration)`,
which is correct under the trapezoidal/lower-bound capacity weighting (it does
**not** divide by the end-of-step count).

A `rates.yaml` produced before the `spatial:` block existed has no footprint to
divide by; the view still renders, with patch capacities blank and a note to
re-solve.

## The patch-selection file (the contract)

**Export YAML** downloads the current selection, computed client-side. It carries
the *resolved totals*, not just patch ids, so it's self-contained and independent
of map-probe ordering:

```yaml
seed: 1063559207
scenario: default-victory
drill_footprint: 11.375
resources:
  copper-ore:
    unit: drills
    patches: [0, 11]      # patch ids (informational)
    total_tiles: 4828     # sum of selected patches' tiles  <- L2 reads this
    capacity: 424.5       # total_tiles / footprint (informational)
    peak_demand: 400.0    # from the solve (reference)
  crude-oil:
    unit: pumpjacks
    patches: [50]
    spots: 11             # sum of selected clusters' spots  <- L2 reads this
    capacity: 11.0
    peak_demand: 4.8
```

L2 reads only `total_tiles` (drills) / `spots` (pumpjacks); `capacity` and
`peak_demand` are informational. A resource omitted from the file keeps its
**full** map availability.

## L2 integration

Bind the file to a run, then solve:

```bash
.venv/bin/fplan rates add-selection my-run my-run_patch-selection.yaml
.venv/bin/fplan rates solve my-run
```

`add-selection` records it under the manifest's `inputs:` (content-hashed, like
the other inputs); a one-off `rates solve --patch-selection PATH` overrides the
bound input without touching the manifest. See
[`rates add-selection`](usage.md#rates-add-selection).

The override needs **no new LP constraint** — it reuses the existing tile-pool
path (`apply_patch_selection` in `fplan.l2.instance`, applied in
`build_instance`):

- drill resources → replace that resource's `tile_pool` with `total_tiles`. The
  per-ore cap is already `tile_pool / footprint`, so this *is* the miner cap (and
  it also tightens the per-resource spatial rate cap, which reads the same pool).
- crude-oil → replace `oil_spot_count` with the selected `spots`.

The file is **untrusted**: a non-mapping file (or unreadable YAML) is a clean
error, while a single malformed per-resource entry is skipped with a warning
rather than aborting the solve.

## Scope and open items

- **Oil is spot-coverage, not yield-weighted.** Demand is the pumpjack *count*
  and capacity is one-per-spot; per-pumpjack yield (which the solve does account
  for) isn't surfaced. Fine for "which oil field to commit."
- **Burner drills are context, not capacity.** They're a pooled, hard-capped
  bootstrap — not ore-split and not tile-pool-capped — so they're shown as a
  drill-equivalent contribution series, but the capacity-sufficiency check stays
  electric-only, matching what the LP actually caps against patch tiles.
- **No fixed-charge optimizer in the loop.** The view shows the trade; it doesn't
  *solve* the facility-location problem. It's the human-in-the-loop precursor and
  the L2 feedback path; the min-cost selection is L3's job.
- **Distance is spawn-relative.** The cost that ultimately matters is
  patch→consumer, which depends on placement — the chicken-and-egg that puts
  assignment inside the L3 placement loop.
