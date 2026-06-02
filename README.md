# fplan

Factorio production and placement planner.

> **Status: early scaffolding.** The CLI command surface is complete and
> navigable, but most stages are stubs being migrated incrementally — the
> structure, conventions, and packaging baseline are in place ahead of the
> planning logic that fills them in.

fplan is **clone-first**: the documentation and example/reference material live
in the repository and come with a `git clone`. Start with this README, then see
[`docs/`](docs/).

## Install

### Prerequisites

- **Python 3.11+**.
- **A Factorio installation** — fplan reads its game data to compute plans. See
  [Configuration](docs/usage.md#configuration) to point fplan at it.

Clone, then install into an isolated environment:

```bash
git clone https://github.com/jiggloo/fplan.git
cd fplan
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Quickstart

Go from a fresh clone to a solved plan and an interactive visualization. The CLI
is the primary interface; invoke it via `.venv/bin/fplan` (or activate the venv
with `source .venv/bin/activate` and type `fplan`).

After [installing](#install), from the repository root:

```bash
# Detect Factorio and copy the bundled examples into the working directory.
.venv/bin/fplan init --copy-examples

# Solve the steelaxe example's production plan (needs Factorio).
.venv/bin/fplan rates solve steelaxe

# Open an interactive visualization of the result.
.venv/bin/fplan rates viz steelaxe --open
```

`viz` opens a zoomable timeline and a capacity-saturation heatmap (written under
`runs/steelaxe/viz/`).

Orient yourself any time with:

```bash
.venv/bin/fplan                 # where am I, and is Factorio configured?
.venv/bin/fplan --help          # the full command tree
```

**See [docs/usage.md](docs/usage.md) for the full command reference** —
invocation, configuration, exit codes, and per-command examples.

## Concepts

fplan plans a factory in four stages, each feeding the next:

- **L1** (`tech-order`) — the order to research technologies in.
- **L2** (`rates`) — how much of each item to produce, over time.
- **L3** (`layout`) — where to place the machines.
- **L4** (`execution`) — the action steps a TAS generator replays.

Three authored **inputs** feed the stages, and a **run** ties a specific
combination together. These are the words fplan uses precisely (each maps to a
[top-level directory](#repository-layout)):

- **run** — one *execution* of the L2→L4 pipeline: binds a scenario, a
  tech-order, and a map in `runs/<name>/` (described by a `manifest.yaml`) and
  collects each stage's output there. → `runs/`
- **scenario** — the *problem*: the world you start from and the world you want
  — a `GoalState` (techs to research, items to produce, rockets to launch) plus
  an optional `initial_state` (what exists at t₀). → `scenarios/`
- **tech-order** — the *research plan*: the layered order techs are researched
  in. L1's output (or hand-authored); records a reference to the scenario it was
  built from, not the scenario's content. → `tech-orders/`
- **map** — the *environment*: resources, water, and oil around spawn, from a
  Factorio seed/save. Orthogonal to the scenario. → `maps/`

So scenario, tech-order, and map are **reusable inputs** that exist on their own
(one scenario → many tech-orders → many runs); a **run** ties a specific
combination together and produces results. The tech-order (L1) is an *input* to
a run, not part of it — a run is L2→L4.

## Testing

Install the development dependencies and run the suite:

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

### Manual integration tests

Some checks need a real Factorio install and can't run in CI — run them by hand
after changes to the loaders or solver. See
[Manual integration tests](docs/integration_tests.md#manual-integration-tests).

## Development

Install the dev toolchain (above) and the pre-commit hooks:

```bash
.venv/bin/pre-commit install
```

Each commit then auto-runs the hygiene, lint/format (ruff), and secret-detection
checks on the staged files — or run them across the whole repo at any time:

```bash
.venv/bin/pre-commit run --all-files
```

The two checks pre-commit doesn't cover — run them yourself before pushing:

```bash
.venv/bin/mypy       # type-check
.venv/bin/pytest     # tests + coverage
```

CI is the authoritative gate: it re-runs everything above across the Python
3.11–3.14 matrix, plus a packaging build check.

## Repository layout

```
fplan/
├── src/fplan/          Python package (installable)
│   └── resources/      runtime resources shipped with the package
├── docs/               in-repo documentation (travels with the clone)
├── tests/              test suite
│
├── scenarios/          your problem descriptions            (tracked)
├── tech-orders/        your curated / validated tech-orders  (tracked)
├── maps/               generated map data — regenerable      (ignored)
├── runs/               per-run output                        (ignored)
│
└── examples/           reference material to learn from / run in place
    ├── scenarios/      example problem descriptions          (tracked)
    ├── tech-orders/    example tech-orders                   (tracked)
    ├── maps/           example map(s)                        (tracked)
    └── runs/           example run manifest(s)               (manifest tracked,
                                                               artifacts ignored)
```

The committed-vs-ephemeral split is the core convention — see
[Repository structure & conventions](docs/structure.md) for the rule and the
reasoning. In short: **authored/curated inputs are tracked; generated and
regenerable artifacts are not.** The fully-ephemeral directories (`maps/`,
`runs/`) keep a tracked `.gitkeep` so they're visible in a fresh clone;
`examples/runs/` is kept present by its committed example `manifest.yaml` (its
generated stage artifacts stay ignored). Either way the intended layout is
obvious without reading the docs.

## Documentation

- [Usage reference](docs/usage.md) — the full CLI reference
- [Integration tests](docs/integration_tests.md) — manual checks that need a real
  Factorio install (model load, map extraction, L2 solve/viz/post)
- [Repository structure & conventions](docs/structure.md)
- [Stage enrichment](docs/stage-enrichment.md) — why per-stage knowledge (e.g.
  L2 deployment) enriches downward and never lives in the base model layer
- [L2 rate-flattening](docs/L2-rate-flattening.md) — the `rates post` design:
  the causal-tube flattening methods and the diff visualization

## License

[MIT](LICENSE)
