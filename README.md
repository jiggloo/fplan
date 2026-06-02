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

Requires Python 3.11+. Clone, then install into an isolated environment:

```bash
git clone https://github.com/jiggloo/fplan.git
cd fplan
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

The CLI is the primary interface. After installing into the virtualenv (above),
invoke it via `.venv/bin/fplan` (or activate the venv and type `fplan`):

```bash
.venv/bin/fplan                 # working directory + config status
.venv/bin/fplan --help          # the full command tree
.venv/bin/fplan init            # create the config file
```

The command tree mirrors the planning pipeline — `tech-order` (L1), `rates`
(L2), `map`, `layout` (L3), `execution` (L4), plus `inspect`, `init`, and `run`
(which manages whole L2→L4 executions). The surface is complete; the stages are
being filled in incrementally, and an un-built command prints a clear notice
with a reserved exit code rather than failing cryptically.

**See [docs/usage.md](docs/usage.md) for the full command reference** —
invocation, configuration, exit codes, and per-command examples.

## Concepts

A few words fplan uses in a specific way. They map directly onto the
[top-level directories](#repository-layout), so knowing them makes the layout
self-explanatory:

- **scenario** — the *problem*: the world you start from and the world you
  want, as a `GoalState` (techs to research, items to produce, rockets to
  launch) plus an optional `initial_state` (what exists at t₀). An authored
  **input**. → `scenarios/`
- **tech-order** — the *research plan*: the order techs get researched in,
  layered. It's L1's **output** (or hand-authored), built from a scenario and
  consumed by L2. It records a lightweight reference to the scenario it came
  from, not the scenario's content. → `tech-orders/`
- **map** — the *environment*: resources, water, and oil around spawn, derived
  from a Factorio seed/save. An **input**, orthogonal to the scenario. →
  `maps/`
- **run** — one *execution* of the L2→L4 pipeline. A run binds a scenario, a
  tech-order, and a map in `runs/<name>/`, described by a `manifest.yaml`; as the
  L2–L4 stages land it will apply their solver settings and collect the per-level
  outputs there. → `runs/`

The shape of it: scenario, tech-order, and map are **reusable inputs** that
exist on their own (one scenario → many tech-orders → many runs); a **run** is
the thing that ties a specific combination together and produces results.
**L1 (the tech-order) is an input to a run, not part of it** — a run is L2→L4.

## Testing

Install the development dependencies and run the suite:

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

### Manual integration tests

Some functionality needs a real Factorio installation and can't run in CI, so
the automated suite covers the pure logic and these steps cover the rest. Run
them by hand after changes that touch the loaders; configure the relevant path
first with `fplan init` (see
[Configuration](docs/usage.md#configuration)) — the model-load step needs
`data_dir`, the map step needs `binary`.

- **Game model load** — parse the installed Factorio prototype data and print a
  summary (item/recipe/building/technology counts):

  ```bash
  .venv/bin/python -m fplan.model
  ```

  Confirm it succeeds and the counts look sane (e.g. hundreds of recipes/items).
  The automated tests exercise the model *cleaning* against a small captured
  prototype fixture; this step exercises the live Lua load that fixture stands
  in for.

- **Map extraction** — run a headless extraction against a save and confirm the
  artifact (and that the source save is untouched):

  ```bash
  .venv/bin/fplan map from-save path/to/save.zip --out maps/save.yaml
  .venv/bin/fplan map show maps/save.yaml
  ```

- **L2 solve** — the SCIP optimize needs the full model and is a per-seed primal
  coin flip, so it's exercised here rather than in CI (the automated tests cover
  the solver-*neutral* L2 layer — config, scenario, instance build, deployment —
  against the fixture). Solve the committed **steelaxe** example run in place
  (the quickest smoke):

  ```bash
  cd examples
  ../.venv/bin/fplan --config-file ../.fplan-config.yaml \
      rates solve steelaxe --seed 1 --time-limit-s 120
  ../.venv/bin/fplan --config-file ../.fplan-config.yaml run show steelaxe
  ```

  Confirm it reports a feasible `t_FINAL` and writes `rates.yaml`; `run show
  steelaxe` then lists `rates.yaml` under artifacts, and
  `runs/steelaxe/manifest.yaml` has gained an `l2:` block
  (mode/seed/objective_s/status/solve_time_s/config). (The committed `fishminer`
  run binds the full `default-victory` campaign — solvable the same way, but
  larger and may need several seeds to land an incumbent.)

## Development

Install the dev toolchain (above) and the pre-commit hooks:

```bash
.venv/bin/pre-commit install
```

Run the checks locally — these mirror what CI enforces:

```bash
.venv/bin/ruff check .        # lint
.venv/bin/ruff format .       # format
.venv/bin/mypy                # type-check
.venv/bin/pytest              # tests + coverage
```

Pre-commit runs the hygiene, lint/format, and secret-detection checks on each
commit; CI re-runs them (plus the test matrix and a build check) as the
authoritative gate.

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
- [Repository structure & conventions](docs/structure.md)
- [Stage enrichment](docs/stage-enrichment.md) — why per-stage knowledge (e.g.
  L2 deployment) enriches downward and never lives in the base model layer

## License

[MIT](LICENSE)
