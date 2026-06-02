# Repository structure & conventions

This document is the source of truth for the repository's layout and the
conventions the incremental migration follows. It is intentionally about
*structure and rules*, not features.

## Directory roles

| Path | Role | Tracked in git? |
|---|---|---|
| `src/fplan/` | The installable Python package. | yes |
| `src/fplan/resources/` | Resources the installed package needs at runtime (shipped in the wheel). | yes |
| `docs/` | In-repo documentation. Travels with the clone. | yes |
| `tests/` | Test suite. | yes |
| `scenarios/` | Your problem descriptions (authored source of truth). | yes |
| `tech-orders/` | Your curated, validated/hand-edited tech-orders. | yes |
| `maps/` | Generated map data. Regenerable from Factorio save files. | no (contents) |
| `runs/` | Per-run output. | no (contents) |
| `examples/scenarios/` | Example problem descriptions. | yes |
| `examples/tech-orders/` | Example tech-orders. | yes |
| `examples/maps/` | Example map(s) — lets the examples run without Factorio. | yes |
| `examples/runs/` | Example run(s): the `manifest.yaml` is tracked; generated stage artifacts are not. | manifest only |

## The committed-vs-ephemeral rule

The single organizing principle:

> **Authored or curated inputs are tracked. Generated and regenerable
> artifacts are not.**

Applied per artifact type:

- **Problem descriptions** (`scenarios/`) — authored → tracked everywhere.
- **Tech-orders** (`tech-orders/`) — curated/validated, rarely regenerated in
  practice → tracked everywhere. (A freshly *generated* tech-order is an
  artifact; a *kept, hand-edited* one is an input. Keep the ones you commit on
  the tracked side.)
- **Maps** (`maps/`) — regenerable from save files → a cache, not tracked. The
  one exception is the canonical example map under `examples/maps/`, committed
  so the examples can run without a Factorio install.
- **Runs** (`runs/`) — always output → never tracked. The exception is
  `examples/runs/`: each example run's **`manifest.yaml`** is committed as
  reference material (it binds a scenario + tech-order + map and is stable),
  while the **stage artifacts** a run generates (`rates`/`layout`/`execution`)
  stay ignored — they're regenerable output. So an example run is a tracked
  manifest plus ignored, reproducible results.

Rationale: generated artifacts never pollute git history or diffs, while the
inputs worth versioning are kept. Because the artifacts are regenerable, losing
the ignored directories' contents costs nothing.

### Why the ephemeral directories still appear in a fresh clone

The fully-ephemeral directories (`maps/`, `runs/`) each keep a tracked
`.gitkeep`, and `.gitignore` ignores only the *contents* (`/maps/*` with
`!/maps/.gitkeep`). `examples/runs/` needs no `.gitkeep` — its committed
example `manifest.yaml` already keeps the directory present. So the intended
input and output locations are visible immediately after cloning —
discoverable without reading any docs — yet generated files inside them stay
untracked and `git status` stays clean after a run.

## Examples and running in place

`examples/` mirrors the working layout (`scenarios/`, `tech-orders/`, `maps/`,
`runs/`). It holds curated reference material to learn from, and is positioned
so a later run can use it *in place* — reading the example inputs and writing
its (ignored) stage artifacts under `examples/runs/<name>/`, alongside the
committed manifest — without copying files and without owning Factorio. The mechanism for selecting where the tool reads and writes (a
working-directory concept) is part of the CLI; the CLI skeleton now exists
(bare `fplan` reports the working directory it would operate from), but the
working-directory / run-directory *resolution* is not wired up yet.

## Versioning

The version has a single source of truth: the `__version__` literal in
`src/fplan/_version.py`.

- The build backend reads it (`[tool.hatch.version]` in `pyproject.toml`, with
  `dynamic = ["version"]`), so the distribution metadata derives from it.
- The package re-exports it as `fplan.__version__` (`src/fplan/__init__.py`),
  which works both when installed and when run from a source checkout.
- Any other consumer (release tooling, docs) must read it from this one place —
  no second, separately-maintained copy.
