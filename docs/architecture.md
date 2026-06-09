# Architecture — the L1→L4 pipeline (and where to read about each part)

fplan plans a factory in four stages, each consuming the stage before it. This
page is the contributor's front door: the data that flows between stages, where
the optimization actually sits, and an index of every design doc. For the
user-facing definitions of the nouns below — **scenario**, **tech-order**,
**map**, **run** — see the README [Concepts](../README.md#concepts); they aren't
restated here.

## The pipeline at a glance

```
 L1  tech-order build      scenario ───────────────▶ tech-order
                                                          │
                           ┌────────────── run binds ─────┤
                           scenario + tech-order + map     │   (a run is L2→L4;
                           └──────────────┬────────────────┘    the tech-order is
                                          ▼                      an input to it)
 L2  rates solve           ───▶ rates.yaml ──(rates post)──▶ rates-post.yaml
                                          │                   (provisional L3 input)
                                          ▼
 L3  layout                ───▶ machine placement            (stub)
                                          ▼
 L4  execution             ───▶ TAS action steps             (stub)
```

**L1** orders the research. **L2** solves the production schedule and
post-processes it for layout. **L3** places the machines; **L4** emits the action
steps a TAS generator replays. L1 and L2 run end-to-end today; L3 and L4 are
stubs (each unbuilt command says so in its `--help` and exits `71` — see the
[command tree](usage.md#the-command-tree)).

## Where the solver fits

The four stages are not equal in weight. **L2 `rates solve` is the one heavy
optimization** — a nonconvex nonlinear program (bilinear machine-capacity
constraints) solved with SCIP, where most of the project's modeling lives. The
others are lighter in kind: **L1** is a layered topological ordering of the tech
tree; **L2 `rates post`** is a deterministic post-transform of the solve's
output (rate-flattening, no re-solve); **L3** will be spatial placement and
**L4** action-step generation. So "the solver algorithm" almost always means L2
solve — its full treatment is [L2 rates — the solve](L2-rates-solve.md).

## Stage contracts (what each hands the next)

| Stage | Consumes | Produces | Where it's documented |
|---|---|---|---|
| **L1** `tech-order build` | a scenario (the goal) | a `tech-order` (layered research order; carries only a `scenario:` *reference*, no goal content) | [usage: tech-order](usage.md#tech-order) |
| **L2** `rates solve` | scenario + tech-order + optional map | `rates.yaml` — the per-step schedule and `t_FINAL` | [L2 rates — the solve](L2-rates-solve.md) |
| **L2** `rates post` | `rates.yaml` | `rates-post.yaml` — the flattened schedule, the **provisional** L3 input | [L2 rate-flattening](L2-rate-flattening.md) |
| **L3** `layout` | `rates-post.yaml` | machine placement *(stub)* | — |
| **L4** `execution` | a layout | TAS action steps *(stub)* | — |

Two contract notes worth holding onto:

- **The L1 output is decoupled from its input.** A tech-order records a
  lightweight reference (name + path + content hash) to the scenario it was built
  from, not the scenario's content — so the two stay disjoint and `verify`
  re-resolves the goal from that reference.
- **The L2→L3 handoff is deliberately unsettled.** `rates-post.yaml` mirrors the
  `rates.yaml` schema only because L3's preferred input format isn't decided yet;
  it's tagged `schema: provisional-rates-mirror` and nothing downstream should
  assume it's stable. See [L2 rate-flattening § Provisional by
  design](L2-rate-flattening.md#provisional-by-design).

## How knowledge attaches to the model

One shared `GameModel` (techs, recipes, items) flows through every stage, and
each stage **enriches downward and never reaches up**: the model layer is
base-pure (L1's minimal view), and L2 owns its own increments — deployment
packings, spatial caps, energy assumptions — as the `L2Instance` the solver
consumes. L3/L4 follow the same shape. The rationale (a dependency-inversion
rule) is in [Stage enrichment](stage-enrichment.md).

## Design-doc index

User-facing reference:

- [Usage reference](usage.md) — the full CLI: every command, configuration, exit
  codes.
- [Reading your results](reading-results.md) — interpret a solved run: the
  `rates.yaml`, the three views, and the `post` diff, as one reading workflow.
- [Repository structure & conventions](structure.md) — the tracked-vs-generated
  rule and the versioning scheme.
- [Manual integration tests](integration_tests.md) — the checks that need a real
  Factorio install (can't run in CI).

Design / contributor-facing, by stage:

| Topic | Doc |
|---|---|
| **L2** — the solve (the NLP: model, variables, constraints, solver hacks, extending) | [L2-rates-solve.md](L2-rates-solve.md) |
| **L2** — `rates post` rate-flattening (the flattening methods + diff viz) | [L2-rate-flattening.md](L2-rate-flattening.md) |
| **L2** — facility assignment (committing drills / furnaces / assemblers to a job) | [L2-assignment.md](L2-assignment.md) |
| **L2** — the facility-area view + base-area split (the spatial L2→L3 lens) | [L2-area-viz.md](L2-area-viz.md) |
| **L2** — patch selection (the supply-curve view + the feedback input) | [L2-patch-selection.md](L2-patch-selection.md) |
| **Cross-cutting** — stage enrichment (model base-pure, enrich downward) | [stage-enrichment.md](stage-enrichment.md) |

L1 has no standalone design doc yet (the ordering is covered in
[usage: tech-order](usage.md#tech-order)); L3 and L4 are stubs and get design
docs as they're built.
