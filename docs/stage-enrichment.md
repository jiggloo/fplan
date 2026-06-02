# Stage enrichment: the model stays base-pure; each level enriches downward

A decision note on how per-stage knowledge attaches to the shared model, and
why the deployment overlay lives in L2 rather than in the model layer.

## The problem

Each planning level integrates *more* information than the last: L1 needs only
the bare game model (techs, recipes, items); L2 adds deployment packings,
spatial caps, pseudo-recipes, energy assumptions; L3 will add placement detail;
L4 execution. The shared `GameModel` is therefore the **minimal (L1) model**,
and later levels need the increments.

The naive way to attach L2's increments is to have the base layer reach *up* for
them — e.g. `model.make_facility` importing the L2 deployment registry to fill a
Facility's `infrastructure_items`/`tile_footprint`. In a flat module namespace
that feels harmless, but once the layers are packages (`fplan/model/` vs
`fplan/l2/`) it's a **dependency-inversion violation**: the lower, more-stable
layer imports the higher one. A lazy in-function import hides the import cycle
but keeps the backwards dependency.

## The rule

**Dependencies point one way — downward. Each stage enriches the artifacts of
the stage(s) below it and never reaches up.** This is the Clean/Onion
"Dependency Rule" / SOLID Dependency Inversion, applied to the planning chain.

Concretely:

- `fplan/model/` is base-pure and imports nothing from `fplan/l2/` (or above).
  `make_facility` returns a bare `Facility`: empty `infrastructure_items` and the
  bare prototype footprint (`Facility.tile_footprint` = the building's
  `base_tile_footprint`).
- `fplan/l2/` owns its own enrichment. `l2.deployment.deployed_facility(model,
  building, config)` calls the base factory and overlays the deployment pattern
  from the L2 config. L2 code uses *that* when it needs a deployment-aware
  facility (footprints for spatial caps, infra for the reservation/player-time).
- The L2-enriched "model" already exists as `L2Instance` — the artifact L2
  produces and the solver consumes. The deployment overlay is just one more
  piece of that enrichment, kept on the L2 side.

L3/L4 follow the same shape: enrich the prior level's artifacts, importing only
downward.

## Schema-on-base, behavior-in-L2

The `infrastructure_items`/`tile_footprint` *fields* stay on the shared
`Facility` (empty-defaulted) — the base type knows the *shape* of the
enrichment. The *values* and the *code that applies them* live in L2. This keeps
one Facility type flowing through the pipeline (no per-stage subtypes to convert
between) while removing the dependency violation, which is the part that
actually bites. If the base `Facility` later accretes too many higher-stage
fields, promoting to per-stage enriched types is the escalation; it isn't needed
yet.

## Relationship to L2 binding principle #14

In `factorio_explore` this overlay was done inside `make_facility` (binding
principle #14: "deployment = a Facility extension that `make_facility`
populates"). This note **refines #14** for the packaged layout: deployment is
still a Facility extension populated from a registry, but the population happens
in `l2.deployment.deployed_facility`, not in the base `make_facility` — so the
model layer carries no L2 knowledge. The registry itself moved into the tunable
L2 config (`fplan/l2/config.py`, `src/fplan/resources/l2-defaults.yaml`).
