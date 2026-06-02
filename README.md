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
invoke it via `.venv/bin/fplan` — run it with no arguments to see the working
directory it operates from, and `--help` to explore the command tree:

```bash
.venv/bin/fplan                 # prints the working directory
.venv/bin/fplan --help          # the full command tree
.venv/bin/fplan tech-order --help
```

(Activate the virtualenv with `source .venv/bin/activate` if you'd rather type
`fplan` directly.)

The command tree mirrors the planning pipeline — `tech-order` (L1), `rates`
(L2), `map`, `layout` (L3), `execution` (L4), plus `inspect`, `init`, and
`full-run`. The surface is complete, but the stages are being migrated
incrementally: a command that isn't built yet prints a clear notice and exits
with a reserved code rather than failing cryptically. Two codes distinguish the
states — **70** (exists in the source project but not yet ported) and **71**
(planned but not yet implemented) — so scripts can tell them apart.
Artifact-producing commands also accept `--dry-run`.

The package version is also importable directly:

```bash
.venv/bin/python -c "import fplan; print(fplan.__version__)"
```

## Configuration

fplan reads `.fplan-config.yaml` from the current working directory. It mainly
records where Factorio is installed (its data directory and executable), which
the planning stages need. Generate it with:

```bash
.venv/bin/fplan init
```

`init` asks before scanning the known install locations for your OS, fills in
what it finds, and otherwise writes a template for you to complete (see
[`.fplan-config.example.yaml`](.fplan-config.example.yaml) for the format). It
never overwrites an existing file — delete it to regenerate. Auto-detection is
only verified on macOS today; on Windows/Linux it warns and you should check the
paths it writes.

Commands that require Factorio treat a missing or invalid config as a fatal
error (message to stderr, non-zero exit); `init` and bare `fplan` only warn (to
stdout) and continue. No stage requires it yet — the planning stages are stubs.

CLI arguments will take precedence over config-file values once commands expose
such options; there is no environment-variable support. `--config-file PATH`
selects a file other than the default. `.fplan-config.yaml` is git-ignored; the
committed `.example` file is the documentation.

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
