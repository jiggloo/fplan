# CLAUDE.md

Guidance for AI agents working in this repo. This file is a **router**, not a
second copy of the docs — it points at the canonical sources and adds only the
things an agent needs that aren't user-facing. Keep it that way: when something
is already documented, link it here instead of restating it.

## Read these first (canonical — don't duplicate their content here)

- **[README.md](README.md)** — what fplan is, install, the Quickstart, the
  Concepts (the L1→L4 pipeline + scenario/tech-order/map/run), the repo layout,
  and the **Status** (what works today vs. stubs). Start here.
- **[docs/usage.md](docs/usage.md)** — the full CLI reference: every command,
  configuration, and the reserved **exit codes**.
- **[docs/structure.md](docs/structure.md)** — the tracked-vs-gitignored rule
  and the single-source versioning scheme.
- **[docs/integration_tests.md](docs/integration_tests.md)** — the manual checks
  that need a real Factorio install (can't run in CI).
- **[docs/stage-enrichment.md](docs/stage-enrichment.md)**,
  **[docs/L2-rate-flattening.md](docs/L2-rate-flattening.md)** — design rationale
  for the model layering and the `rates post` flattening.

## What fplan is (for orientation)

A Factorio production/placement planner, structured as a four-stage pipeline
(L1 `tech-order` → L2 `rates` → L3 `layout` → L4 `execution`). Code is migrated
**incrementally from the private `factorio_explore` repo**; L1 and L2 work today,
L3 and L4 are stubs (see the README Status). It's public-bound — write for an
external reader.

## Binding invariants (don't violate without discussion)

These are cross-cutting rules for *changing the code*, learned/enforced in
review. They're not in the user docs because they're about authorship.

1. **Never print a raw traceback.** Catch and map to the reserved exit codes:
   `1` fatal (message to **stderr**), `2` usage error, `70` not-migrated, `71`
   not-implemented (`fplan.cli._stub`; codes documented in usage.md). Untrusted,
   degenerate input must surface as a clean error, never a crash.
2. **`--from` / user-supplied YAML is untrusted.** Every viz DOM sink must be
   escaped (`esc()` in JS, `_script_safe` for embedded JSON, `html.escape` in
   Python — see `fplan.l2.viz`), and file resolution must stay confined (no
   arbitrary reads). This XSS class was found twice in review — don't reopen it.
3. **Effective-settings contract.** A command with defaulted options prints a
   `settings:` line via `fplan.cli._log.echo_settings`, marking which values are
   defaults — so omitting an optional flag is never opaque.
4. **The capability boundary lives in `--help`, not a docs matrix.** Unbuilt
   commands tag their status (`(pending migration)` / `(not implemented)`);
   a test over the stub registry enforces it (`tests/test_cli.py`). Keep new
   stubs tagged rather than adding a separate "what works" table.
5. **Compose the viz, don't string-surgery it.** Build views through
   `render_html(dataset, *, charts, ...)` in `fplan.l2.viz`; don't reintroduce
   template find-and-replace.
6. **Model invariants.** Recipes are the native unit (items are labels recipes
   output); all numeric values are floats — no integer scaling. See
   `fplan.model`.

## Dev gotchas (these will bite you)

- **Version:** bump the **build** (third) segment of `__version__` in
  `src/fplan/_version.py` per PR, **then** `.venv/bin/pip install -e . --no-deps`
  — otherwise `test_version_matches_installed_metadata` fails on stale editable
  metadata.
- **No Factorio in CI.** Test pure logic against the captured fixture model
  (`tests/fixtures/model_raw_subset.json`, via
  `load_model(raw=build_game_data(...))`); Factorio-dependent steps are manual
  (docs/integration_tests.md). **Tests must not depend on gitignored example
  artifacts** (`examples/runs/*/rates.yaml`) — build hermetic fixtures instead.
- **A real solve grows tracked example manifests.** After exercising an example
  run, `git checkout examples/runs/<run>/manifest.yaml` to discard the
  `l2:`/`post:` block it gained.
- **Gates before pushing:** `.venv/bin/mypy` and `.venv/bin/pytest` (≥80%
  coverage). pre-commit already auto-runs ruff lint/format + secret detection;
  CI is the authoritative gate (adds the 3.11–3.14 matrix and a build check).

## Working conventions

- **`.venv/bin/…` for everything** — don't assume a global Python.
- Changes land as **reviewed PRs off `main`**; keep docs in sync with code
  (update the README Status at stage boundaries; surface capability via `--help`,
  not a maintained table).

## Documentation philosophy (doc work is ongoing)

- **README is the hub / starting point**; reference material that grows with the
  tool lives in `docs/`.
- State each fact **once, at the right altitude**; prefer **outcomes over
  internals** (what the user interacts with, not how it's implemented); **define
  terminology before using it.**

## Don't

- Don't restate README/docs content here — link it (a copy drifts).
- Don't print raw tracebacks, or let untrusted input reach an unescaped sink.
- Don't commit generated artifacts (`maps/`, `runs/`, `viz/`); don't leave a
  real solve's changes in tracked example manifests.
- Don't add a separate capability matrix — the `--help` markers are the source.
