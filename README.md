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

The project is early scaffolding; the only thing wired up so far is the package
version:

```bash
.venv/bin/python -c "import fplan; print(fplan.__version__)"
```

The planning pipeline (and its command-line interface) lands in subsequent
tiers.

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
