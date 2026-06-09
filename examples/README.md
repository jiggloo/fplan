# Examples

Ready-to-run reference inputs that travel with the clone — three worked examples
at increasing size, the one map they share, and the run manifests that bind them.
Use them to learn the pipeline, smoke-test a change, or copy as a starting point
for a plan of your own.

## The three examples

| Example | Goal | Size | Committed inputs |
|---|---|---|---|
| **steelaxe** | research `steel-axe` — the shortest Factorio speedrun category — from a ~3:12 hand-play snapshot | smallest, fastest to solve (a smoke test) | scenario + tech-order + run manifest |
| **fishminer** | launch a rocket via an alternate research route | mid | tech-order + run manifest (**reuses the `default-victory` scenario**) |
| **default-victory** | launch a rocket — full Factorio 1.1 victory, with late-game ergonomics and 20 beacons | largest | scenario + tech-order (no run — you create one) |

`fishminer` is the worked example of the **input-reuse pattern**: it binds the
same `default-victory` *scenario* (the goal) to a different `fishminer`
*tech-order* (the research route), showing how one scenario feeds many tech-orders
and many runs. The nouns — scenario / tech-order / map / run — are defined in the
README [Concepts](../README.md#concepts).

## What's committed vs. generated

The **inputs** are tracked: `scenarios/`, `tech-orders/`, and the shared
`maps/zaspar-wr.yaml` (the resources around spawn for Zaspar's WR seed). A **run**
is mostly generated — only its `manifest.yaml` (the input bindings, recorded by
content hash) is committed; the stage artifacts a solve produces (`rates.yaml`,
`rates-post.yaml`, `rates-search/`, `viz/`) are git-ignored and reappear when you
solve. So a fresh clone shows the two example runs as bare manifests, and running
them fills in the rest. The tracked-vs-generated rule and its rationale are in
[structure.md](../docs/structure.md).

## Running them

The quickest path is the README [Quickstart](../README.md#quickstart): `fplan init
--copy-examples` copies these inputs into your working `scenarios/` /
`tech-orders/` / `maps/`, after which `fplan rates solve steelaxe` solves the
bundled run and `fplan rates viz steelaxe --open` opens its visualization. Start
with **steelaxe** — it's the fastest to solve; `default-victory` is the full
problem and takes longest. To bind your own combination of these inputs into a new
run, see [usage: run](../docs/usage.md#run).
