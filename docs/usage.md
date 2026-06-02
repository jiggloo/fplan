# Usage reference

The full reference for the `fplan` command-line interface: how to invoke it,
how to configure it, and per-command examples. The [README](../README.md) is the
starting point; this document is where the detail lives and grows as commands
are implemented.

## Table of Contents

- [Invoking the CLI](#invoking-the-cli)
- [The command tree](#the-command-tree)
- [Configuration](#configuration)
- [Commands](#commands) (alphabetical by group)
  - [`map`](#map)

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

The command groups (alphabetical for lookup; the `Level` column shows where each
sits in the L1 → L4 planning pipeline):

| Group / command | Level | Purpose |
|---|---|---|
| `execution` | L4 | Step generation (TAS-generator input) |
| `full-run` | — | Run the whole L1 → L4 chain |
| `init` | — | Create the config file |
| `inspect` | — | Inspect the game model (tech / item / recipe) |
| `layout` | L3 | Spatial placement |
| `map` | — | Map artifact generation and inspection |
| `rates` | L2 | Production-rate solving |
| `tech-order` | L1 | Technology research ordering |

The surface is complete, but the stages are being migrated incrementally. A
command that isn't built yet prints a clear notice and exits with a reserved
code rather than failing cryptically — two codes distinguish the states so
scripts can tell them apart:

| Exit code | Meaning |
|---|---|
| `70` | Exists in the source project (factorio_explore) but not yet ported |
| `71` | Planned but not yet implemented |

Commands that take a side-effecting action (write an artifact, create the config
file, run a stage) also accept `--dry-run`, which reports what would happen
without doing it.

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

## Commands

Per-group command reference, in alphabetical order.

### `map`

A *map artifact* is a single self-describing YAML bundle (seed, map-gen
settings, probe radius, resource patches, oil fields, water bodies, tree count)
describing the world around spawn, so a map can be reproduced and inspected from
the file alone.

#### `map from-save`

Turn a Factorio save into a map artifact. The output path is given explicitly
with `--out` (required — there is no implicit naming):

```bash
.venv/bin/fplan map from-save ~/Downloads/MySave.zip --out maps/world.yaml
.venv/bin/fplan map from-save MySave.zip --out maps/world.yaml --dry-run
```

It runs Factorio headless with a bundled extraction mod, so it needs the
configured Factorio **executable** (`fplan init`, or `binary:` in the config).
Notes:

- **The original save is never modified.** It's copied first, because headless
  Factorio autosaves on exit.
- **`--out` is required** and is written verbatim (no `maps/<name>` defaulting).
  `maps/` is the conventional, git-ignored location for these artifacts.
- **Existing output is not clobbered silently.** If the `--out` file already
  exists you're asked to confirm the overwrite; in a non-interactive session the
  command refuses (remove the file or choose another path). This check happens
  before Factorio runs.
- As with `init`, the headless interaction is only verified on macOS; on
  Windows/Linux it warns.

#### `map show`

Print a text summary of an artifact:

```bash
.venv/bin/fplan map show maps/world.yaml
```

```
seed=1063559207  radius=512 tiles
resources: 51 patches across 5 types
  coal: 8 patches, 9616 tiles total; nearest 29.1 tiles away (1201 tiles)
  copper-ore: 11 patches, 11663 tiles total; nearest 55.9 tiles away (1143 tiles)
  iron-ore: 12 patches, 11095 tiles total; nearest 47.8 tiles away (8242 tiles)
  stone: 17 patches, 1720 tiles total; nearest 19.1 tiles away (33 tiles)
  uranium-ore: 3 patches, 1636 tiles total; nearest 220.4 tiles away (974 tiles)
oil: 36 spots in 3 fields; nearest field 201.0 tiles away; avg yield 1074%/spot
water: 30 bodies; nearest 49.7 tiles away
trees: 4244
```

Each resource line gives the patch count, total tiles of that ore, and the
distance to (plus size of) the nearest patch of that type. The oil line gives
the nearest field's distance, the field count, and the average per-spot pumpjack
yield. All distances are tiles from spawn.

#### `map from-string`

Building an artifact from a Factorio map-exchange string is planned but not yet
implemented — `fplan map from-string` currently exits with code `71`.
