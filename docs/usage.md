# Usage reference

The full reference for the `fplan` command-line interface: how to invoke it,
how to configure it, and per-command examples. The [README](../README.md) is the
starting point; this document is where the detail lives and grows as commands
are implemented.

## Invoking the CLI

The CLI is fplan's primary interface. After installing into the virtualenv (see
the README's *Install* section), invoke it via `.venv/bin/fplan`:

```bash
.venv/bin/fplan                 # working directory + resolved config status
.venv/bin/fplan --help          # the full command tree
.venv/bin/fplan --version       # the installed version
.venv/bin/fplan map --help      # help for any group or command
```

Run with no arguments, `fplan` prints the working directory it operates from and
the status of the config it found — a quick "where am I, what's set up" check.

(Activate the virtualenv with `source .venv/bin/activate` if you'd rather type
`fplan` directly.)

The package version is also importable directly:

```bash
.venv/bin/python -c "import fplan; print(fplan.__version__)"
```

## The command tree

The command tree mirrors the planning pipeline:

| Group | Level | Purpose |
|---|---|---|
| `tech-order` | L1 | Technology research ordering |
| `rates` | L2 | Production-rate solving |
| `map` | — | Map artifact generation and inspection |
| `layout` | L3 | Spatial placement |
| `execution` | L4 | Step generation (TAS-generator input) |
| `inspect` | — | Inspect the game model (tech / item / recipe) |
| `init` | — | Create the config file |
| `full-run` | — | Run the whole L1 → L4 chain |

The surface is complete, but the stages are being migrated incrementally. A
command that isn't built yet prints a clear notice and exits with a reserved
code rather than failing cryptically — two codes distinguish the states so
scripts can tell them apart:

| Exit code | Meaning |
|---|---|
| `70` | Exists in the source project (factorio_explore) but not yet ported |
| `71` | Planned but not yet implemented |

Artifact-producing commands also accept `--dry-run`, which reports what would
happen without doing it.

## Configuration

fplan reads `.fplan-config.yaml` from the current working directory. It mainly
records where Factorio is installed — its data directory (prototype files, for
commands that load the game model) and its executable (for commands that run
Factorio headless). Generate it with:

```bash
.venv/bin/fplan init
```

`init` asks before scanning the known install locations for your OS, fills in
what it finds, and otherwise writes a template for you to complete (see
[`.fplan-config.example.yaml`](../.fplan-config.example.yaml) for the format). It
never overwrites an existing file — delete it to regenerate. Auto-detection is
only verified on macOS today; on Windows/Linux it warns and you should check the
paths it writes.

Selecting a non-default config file:

```bash
.venv/bin/fplan --config-file /path/to/config.yaml map show maps/MySave.yaml
```

`--config-file` is a global option, so it precedes the subcommand.

**Require vs. warn.** Commands that *require* Factorio treat a missing or
invalid config as a fatal error (message to stderr, non-zero exit). `init` and
bare `fplan` only warn (to stdout) and continue.

There is no environment-variable support; CLI arguments take precedence over
config-file values where commands expose such options. `.fplan-config.yaml` is
git-ignored; the committed `.example` file is the documentation.

## `map` — map artifacts

A *map artifact* is a single self-describing YAML bundle (seed, map-gen
settings, resource patches, oil fields, water bodies, tree count) describing the
world around spawn, so a map can be reproduced and inspected from the file alone.

### `map from-save`

Turn a Factorio save into a map artifact:

```bash
.venv/bin/fplan map from-save ~/Downloads/MySave.zip      # -> maps/MySave.yaml
.venv/bin/fplan map from-save MySave.zip --out world.yaml # custom output path
.venv/bin/fplan map from-save MySave.zip --dry-run        # show plan, run nothing
```

It runs Factorio headless with a bundled extraction mod, so it needs the
configured Factorio **executable** (`fplan init`, or `binary:` in the config).
Notes:

- **The original save is never modified.** It's copied first, because headless
  Factorio autosaves on exit.
- Artifacts default to the regenerable `maps/` directory; `--out` overrides.
- As with `init`, the headless interaction is only verified on macOS; on
  Windows/Linux it warns.

### `map show`

Print a text summary of an artifact:

```bash
.venv/bin/fplan map show maps/MySave.yaml
```

```
seed=1063559207  radius=512
  51 solid resource patches
  36 oil spots in 3 fields
  30 water bodies (nearest at 49.7)
  4244 trees
```
