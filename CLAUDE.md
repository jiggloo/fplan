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
- **[docs/L2-rates-solve.md](docs/L2-rates-solve.md)** — the deepest L2 design
  doc: how `rates solve` works and how to read its output (the model, the
  `rates.yaml` fields, the solver-specific hacks, downstream-feedback
  coefficients, extending the model, and the visualizer reference).
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

Authorship rules for docs — most were learned writing
[`docs/L2-rates-solve.md`](docs/L2-rates-solve.md), and the dedicated
documentation-review agents enforce them. Improving the docs is how new users are
helped, so this is high-value.

- **README is the hub / starting point**; reference material that grows with the
  tool lives in `docs/`.
- **Write directly.** State what a thing *is* in one declarative sentence before
  why it matters. Cut the three reflexes that bloat prose: a framing device
  ("Imagine pointing at a save…"), define-by-negation ("not a blueprint, but…"),
  and abstract-then-concrete restatement. To write directly, name the reader's
  next action, use the project's canonical noun (e.g. "scenario goal," not "your
  goal"), and state where each fact lives instead of hedging.
- **Redundancy = distance.** Before writing a summary / intro / preview line,
  check its nearest equivalent; if it's adjacent (e.g. one ToC-click from the
  section it previews), cut it. State each fact **once, at the right altitude**,
  define it at its natural home, and **link** from elsewhere rather than restate.
- **Outcome-first, top-down, with stop-points.** Lead with the question or
  outcome; add mechanism only after the reader has seen a result; give explicit
  "you can stop here" exits for readers who only need the first layer. Section
  **titles sit at the altitude of their content** — name what's in the section,
  not a meta-question ("Inputs and output," not "What the solve answers"). Prefer
  **outcomes over internals** (what the user interacts with, not how it's
  implemented), and **define terminology before using it.**
- **Don't over-claim; label uncertainty.** In a complex or heuristic system,
  avoid reductive single-cause claims (no "*the* bottleneck" over thousands of
  constraints); mark hacks as temporary / not-by-design; flag what's unverified or
  undecided rather than presenting it as settled; don't assert behavior you
  haven't run.
- **Verify before asserting; anchor to durable landmarks.** Check claims and
  constants against the code/wiki before writing them (this caught "MINLP" →
  nonconvex NLP, and magic numbers like 100 rocket-parts / 25 000-fluid tank / 80
  inventory slots). When pointing into code, prefer stable landmarks (section
  banners, `name=` / identifier prefixes) over line numbers, and concentrate code
  pointers in one reference section that concept sections link into (**concept →
  stable reference → code**) so churn stays localized.
- **Separate the kinds of "why."** When documenting a model, keep domain/scope
  simplifications, solver/tractability hacks, and placeholders for future feedback
  distinct — readers and editors need to know which is which.
- **Cross-references are links.** Inline section/file references should be
  clickable, to keep reading flow.

## Don't

- Don't restate README/docs content here — link it (a copy drifts).
- Don't print raw tracebacks, or let untrusted input reach an unescaped sink.
- Don't commit generated artifacts (`maps/`, `runs/`, `viz/`); don't leave a
  real solve's changes in tracked example manifests.
- Don't add a separate capability matrix — the `--help` markers are the source.
