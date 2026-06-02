# fplan

Factorio production and placement planner.

> **Status: early scaffolding.** The project is being assembled incrementally —
> this repository currently establishes the structure, conventions, and
> packaging baseline that later work builds on. No planning functionality is
> wired up yet.

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

The CLI is the primary interface. Installing the package puts `fplan` on your
PATH. Run it with no arguments to see the working directory it operates from,
and `--help` to explore the command tree:

```bash
fplan                 # prints the working directory
fplan --help          # the full command tree
fplan tech-order --help
```

The command tree mirrors the planning pipeline — `tech-order` (L1), `rates`
(L2), `map`, `layout` (L3), `execution` (L4), plus `inspect`, `init`, and
`full-run`. The surface is complete, but the stages are being migrated
incrementally: a command that isn't ported yet prints a clear notice and exits
with a reserved code rather than failing cryptically.

The package version is also importable directly:

```bash
.venv/bin/python -c "import fplan; print(fplan.__version__)"
```

## Testing

Install the development dependencies and run the suite:

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

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
    └── runs/           output when running the examples      (ignored)
```

The committed-vs-ephemeral split is the core convention — see
[Repository structure & conventions](docs/structure.md) for the rule and the
reasoning. In short: **authored/curated inputs are tracked; generated and
regenerable artifacts are not.** The ephemeral directories are still visible in
a fresh clone (each keeps a `.gitkeep`) so the intended layout is obvious
without reading the docs; their generated contents are ignored.

## Documentation

- [Repository structure & conventions](docs/structure.md)

## License

[MIT](LICENSE)
