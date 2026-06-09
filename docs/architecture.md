# Architecture — the L1→L4 pipeline (and why it's cut this way)

fplan plans a factory in four stages, each consuming the stage before it. This
page is the contributor's front door: the data that flows between stages, **why
the stages are cut the way they are**, and an index of every design doc. For the
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

The full problem — produce the *minimum-time replayable run* — is one
intractable joint optimization (research order × production × placement ×
keystrokes). The rest of this page is how that joint problem is decomposed, and
why this particular decomposition. The framing below reads a deliberate design as
three forces in balance; it's a lens on the design, not a separately-recorded
history.

## Why this shape — three forces in balance

The cut resolves a tension between two opposing pressures, along a third axis:

- **Decoupling is *possible*** because the stages solve genuinely different
  aspects — ordering, rates, placement, actions. Separable problems.
- **Coupling is *necessary*** because each stage consumes the previous stage's
  output: L1's order fixes L2's science-pack consumption rates (which largely
  shape the run); L2's machine counts size L3's blocks; L3's placement is what L4
  sequences actions to build. This information dependency is what forces **both
  the staging and its direction**.
- **Time is deferred.** Time is the hardest dimension — not because it's large,
  but because its causal coupling is *long-range*: an early decision determines a
  late outcome across a huge gap, and a time-forward search over a full run at the
  1/60 s tick has an impossible branching factor. So the design **defers explicit
  temporal-causal reasoning to the last stage** and gives every earlier stage a
  cheaper surrogate for time.

### Each stage owns one hard substructure

The decomposition partitions the joint problem's sources of difficulty so that
**no single stage is hard in every way** — each is hard in one way:

| Stage | Owns the hard substructure | Problem class | Time appears as |
|---|---|---|---|
| **L1** | ordering / integrality | combinatorial (topological layering) | precedence — a partial order |
| **L2** | production rates | continuous nonconvex NLP — **integer-free** | a coarse budget/cost (minimize `t_FINAL`) |
| **L3** | geometry | spatial placement *(stub)* | static blocks (repurposing penalized) |
| **L4** | sequencing | action ordering / scheduling | exact actions at exact ticks |

The load-bearing example is **L2's integer-freedom.** Lifting the tech order into
L1 isn't only an information dependency — it *removes the combinatorial decision
from L2*, which is exactly what lets L2 be a continuous nonconvex NLP instead of a
far harder nonconvex **MINLP**. (All L2 quantities are floats — even machine
counts; integrality is relaxed here and restored downstream when L3/L4 place whole
entities.) The same partition sends geometry to L3 and keystroke sequencing to L4,
so each solver stays in the easiest class its aspect allows.

### Time, deferred — the part that feels backwards

Humans reason about a speedrun **time-forward**: first do this, then that. The
pipeline does almost the opposite — it treats explicit time as the thing to defer,
and reconstructs the time-forward action sequence only at the very end, as an
*output*, not a reasoning medium. Read down the last column above: **L1** replaces
time with *ordering* (this is partial-order / least-commitment planning); **L2**
represents time as a coarse forward-causal budget (inventory carried step to step,
deadlines, `t_FINAL` minimized) — it handles *coarse* causality but defers exact
timing; **L3**, by penalizing facility repurposing, hands forward *static* blocks
so placement barely sees time; **L4** finally commits to exact actions at exact
ticks — the only stage that confronts time head-on, by which point the upstream
commitments have collapsed the branching factor that made it intractable. The
ladder is **precedence → budget → static → schedule**, each stage committing less
about time than the one below it.

### Why this *particular* order

These three rationales are independent — separability, information dependency,
time-deferral — and they **converge on the same L1→L4 order**: information
dependency forces it topologically; time-deferral wants exact time last;
problem-class partitioning wants integrality first and sequencing last. When
several independent pressures select the same structure, that agreement — not any
one clever idea — is the evidence the structure is near-natural for the problem.

## Stage contracts (what each hands the next)

| Stage | Consumes | Produces | Where it's documented |
|---|---|---|---|
| **L1** `tech-order build` | a scenario (the goal) | a `tech-order` (layered research order; carries only a `scenario:` *reference*, no goal content) | [usage: tech-order](usage.md#tech-order) |
| **L2** `rates solve` | scenario + tech-order + optional map | `rates.yaml` — the per-step schedule and `t_FINAL` | [L2 rates — the solve](L2-rates-solve.md) |
| **L2** `rates post` | `rates.yaml` | `rates-post.yaml` — the flattened schedule, the **provisional** L3 input | [L2 rate-flattening](L2-rate-flattening.md) |
| **L3** `layout` | `rates-post.yaml` | machine placement *(stub)* | — |
| **L4** `execution` | a layout | TAS action steps *(stub)* | — |

Three contract notes worth holding onto:

- **The L1 output is decoupled from its input.** A tech-order records a
  lightweight reference (name + path + content hash) to the scenario it was built
  from, not the scenario's content — so the two stay disjoint and `verify`
  re-resolves the goal from that reference.
- **The L2→L3 handoff is deliberately unsettled.** `rates-post.yaml` mirrors the
  `rates.yaml` schema only because L3's preferred input format isn't decided yet;
  it's tagged `schema: provisional-rates-mirror` and nothing downstream should
  assume it's stable. See [L2 rate-flattening § Provisional by
  design](L2-rate-flattening.md#provisional-by-design).
- **The L2↔L3 edge is genuinely bidirectional.** Placement needs L2's counts, but
  good rates need spatial reality (which patches, routing cost) — a back-edge the
  feed-forward order can't carry. That tension is the subject of the next section.

## A chosen balance, not the only one

The decomposition buys tractability by accepting two honest costs.

**Inter-stage suboptimality.** The information dependency is a clean one-way DAG
*except* at L2↔L3: rates are chosen blind to whether L3 can place them cheaply, so
the global optimum can need joint reasoning the pipeline doesn't do. Relatedly, the
true objective — execution time — is only *realized* at L4 but is ~entirely
*determined* by L1/L2, so every stage optimizes a **proxy** for it (L2 a modeled
time, L3 area/routing as a stand-in for placement-induced delay). The design's bet
is that the upstream surrogates capture *enough* of the long-range coupling that a
late problem rarely needs an early fix.

**The hedge.** Where the bet leaks, fplan re-admits the deferred coupling
deliberately rather than dissolving the stage boundary: the [supply-curve
feedback](L2-patch-selection.md) and the [facility-area lens](L2-area-viz.md) feed
spatial reality back into the next L2 solve, and a [temporal-decomposition
exploration](https://github.com/jiggloo/fplan/issues/56) would selectively
re-introduce causal time *inside* L2 once the cheaper dimensions have made it
affordable. The arc is coherent: **defer temporal-causal reasoning by default;
re-admit it, locally and on purpose, only where the surrogate proves insufficient.**

**Not the only balance.** This is *a* well-chosen balance, not a proven optimum.
Other TAS-generation methods would re-strike it — a monolithic joint solve (exact
but intractable at scale), a time-windowed decomposition (co-optimizes aspects per
era, hard to stitch across eras), or a learned/end-to-end policy (which hits the
*same* long-horizon credit-assignment problem and tends to rediscover these forces
as temporal abstraction + goal decomposition). None of these were benchmarked here;
the point is that any good method must balance the same three forces, because the
*problem* hands them to you — fplan's choice is to make that balance **explicit and
engineered**, one force per stage boundary.

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
docs as they're built. The open
[temporal-decomposition exploration](https://github.com/jiggloo/fplan/issues/56)
tracks scaling the L2 solve along the step axis.
