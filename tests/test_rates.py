"""Tests for `fplan rates solve` (run-aware). The SCIP solve is mocked — CI
exercises the run/manifest plumbing, instance build, dry-run summary, error
paths, and manifest growth; the real optimize is a manual integration test."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fplan import run as run_mod
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.cli import rates as rates_cli
from fplan.model import GameModel, build_game_data, load_model

runner = CliRunner()
MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture(scope="module")
def model() -> GameModel:
    return load_model(raw=build_game_data(json.loads(MODEL_FIXTURE.read_text())))


@pytest.fixture
def use_fixture_model(monkeypatch, model: GameModel):
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: model)


def _make_run(tmp_path: Path, name: str = "r", *, with_map: bool = True) -> None:
    """Write scenario/order/map files and a run manifest binding them (cwd-relative)."""
    (tmp_path / "scn.yaml").write_text("name: t\ntechs_researched: [automation]\n")
    (tmp_path / "order.yaml").write_text(
        yaml.safe_dump({"method": "forward", "layers": [["automation"]]})
    )
    (tmp_path / "map.yaml").write_text("patches: []\n")
    run_mod.save(
        run_mod.run_dir(name),
        run_mod.Manifest.new(
            name,
            scenario="scn.yaml",
            tech_order="order.yaml",
            map_path="map.yaml",
            created="t0",
        ),
    )


def _fake_solve(objective=245.0, status="optimal"):
    def _solve(inst, model, **kw):
        sol = types.SimpleNamespace(
            objective=objective, status=status, solve_time_s=1.5, seed=None
        )
        return sol, None, None

    return _solve


def _fake_solve_by_seed(objectives: dict[int, float | None], status: str = "optimal"):
    """Solve stub mapping seed → objective (None ⇒ infeasible). Unknown seed raises."""

    def _solve(inst, model, *, seed, **kw):
        if seed not in objectives:
            raise RuntimeError(f"no objective mapped for seed {seed}")
        obj = objectives[seed]
        st = status if obj is not None else "infeasible"
        sol = types.SimpleNamespace(
            objective=obj, status=st, solve_time_s=1.0, seed=None
        )
        return sol, None, None

    return _solve


def _fake_solve_any(status: str = "optimal"):
    """Solve stub that accepts any seed; objective derived from the seed."""

    def _solve(inst, model, *, seed, **kw):
        sol = types.SimpleNamespace(
            objective=200.0 + (seed % 50), status=status, solve_time_s=1.0, seed=None
        )
        return sol, None, None

    return _solve


def _fake_write(inst, sol, model, path) -> None:
    """Write a real (stub) candidate file so promotion's copy has something to read."""
    Path(path).write_text(f"seed: {sol.seed}\nstub: true\n")


def _patch_solve(monkeypatch, solve_fn) -> None:
    from fplan.l2 import solve as l2_solve

    monkeypatch.setattr(l2_solve, "solve", solve_fn)
    monkeypatch.setattr(l2_solve, "write_solution", _fake_write)


def test_solve_run_not_found(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "nope"])
    assert r.exit_code == 1 and "not found" in (r.stdout + (r.stderr or ""))


def test_solve_bad_manifest(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    d = run_mod.run_dir("r")
    d.mkdir(parents=True)
    (d / run_mod.MANIFEST_NAME).write_text("- bad\n")
    assert runner.invoke(app, ["rates", "solve", "r"]).exit_code == 1


def test_solve_manifest_missing_binding(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    d = run_mod.run_dir("r")
    d.mkdir(parents=True)
    (d / run_mod.MANIFEST_NAME).write_text("run: r\ninputs: {}\n")
    r = runner.invoke(app, ["rates", "solve", "r"])
    assert r.exit_code == 1 and "lacks a scenario" in (r.stdout + (r.stderr or ""))


def test_solve_input_file_missing(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    (tmp_path / "scn.yaml").unlink()
    r = runner.invoke(app, ["rates", "solve", "r"])
    assert r.exit_code == 1 and "scenario not found" in (r.stdout + (r.stderr or ""))


def test_solve_map_missing(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    (tmp_path / "map.yaml").unlink()
    r = runner.invoke(app, ["rates", "solve", "r"])
    assert r.exit_code == 1 and "map not found" in (r.stdout + (r.stderr or ""))


def test_solve_unknown_mode(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--mode", "bogus"])
    assert r.exit_code == 2 and "unknown mode" in (r.stdout + (r.stderr or ""))


def test_solve_l2_config_missing(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--l2-config", "gone.yaml"])
    assert r.exit_code == 1 and "L2 config not found" in (r.stdout + (r.stderr or ""))


def test_solve_dry_run(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--dry-run"])
    assert r.exit_code == 0 and "dry run" in r.stdout and "Scenario:" in r.stdout
    assert not (run_mod.run_dir("r") / "rates.yaml").exists()


def test_solve_success_grows_manifest(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import solve as l2_solve

    monkeypatch.setattr(l2_solve, "solve", _fake_solve())
    monkeypatch.setattr(l2_solve, "write_solution", lambda *a, **k: None)
    r = runner.invoke(app, ["rates", "solve", "r", "--seed", "7", "--force"])
    assert r.exit_code == 0 and "t_FINAL = 245.0" in r.stdout
    m = run_mod.load(run_mod.run_dir("r"))
    assert m.extra["l2"]["mode"] == "experimental" and m.extra["l2"]["seed"] == 7
    assert m.extra["l2"]["config"] == "default"


def test_solve_records_config_ref(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    (tmp_path / "tune.yaml").write_text("caps: {burner_drill: 9.0}\n")
    from fplan.l2 import solve as l2_solve

    monkeypatch.setattr(l2_solve, "solve", _fake_solve())
    monkeypatch.setattr(l2_solve, "write_solution", lambda *a, **k: None)
    r = runner.invoke(
        app, ["rates", "solve", "r", "--force", "--l2-config", "tune.yaml"]
    )
    assert r.exit_code == 0
    ref = run_mod.load(run_mod.run_dir("r")).extra["l2"]["config"]
    assert ref["path"] == "tune.yaml" and "sha256" in ref


def test_solve_infeasible_exits_nonzero(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import solve as l2_solve

    monkeypatch.setattr(
        l2_solve, "solve", _fake_solve(objective=None, status="infeasible")
    )
    r = runner.invoke(app, ["rates", "solve", "r", "--force"])
    assert r.exit_code == 1 and "no feasible incumbent" in r.stdout
    # No l2 block written on infeasible.
    assert "l2" not in run_mod.load(run_mod.run_dir("r")).extra


def test_solve_overwrite_guard_without_force(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    (run_mod.run_dir("r") / "rates.yaml").write_text("old\n")
    # No --force, non-interactive (CliRunner) → refuse rather than clobber.
    r = runner.invoke(app, ["rates", "solve", "r"])
    assert r.exit_code == 1 and "already exists" in (r.stdout + (r.stderr or ""))


def test_solve_random_seed_when_omitted(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import solve as l2_solve

    monkeypatch.setattr(l2_solve, "solve", _fake_solve())
    monkeypatch.setattr(l2_solve, "write_solution", lambda *a, **k: None)
    r = runner.invoke(app, ["rates", "solve", "r", "--force"])  # no --seed
    assert r.exit_code == 0 and "SCIP seed:" in r.stdout
    assert isinstance(run_mod.load(run_mod.run_dir("r")).extra["l2"]["seed"], int)


def test_solve_solver_exception_is_clean(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import solve as l2_solve

    def boom(*a, **k):
        raise RuntimeError("SCIP exploded")

    monkeypatch.setattr(l2_solve, "solve", boom)
    r = runner.invoke(app, ["rates", "solve", "r", "--force"])
    assert r.exit_code == 1 and "solve failed" in (r.stdout + (r.stderr or ""))
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_solve_write_failure_is_fatal(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import solve as l2_solve

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(l2_solve, "solve", _fake_solve())
    monkeypatch.setattr(l2_solve, "write_solution", boom)
    r = runner.invoke(app, ["rates", "solve", "r", "--force"])
    assert r.exit_code == 1 and "could not write" in (r.stdout + (r.stderr or ""))


# --- multi-seed search -----------------------------------------------------


def test_seed_spec_count() -> None:
    from fplan.cli import rates as rates_cli

    assert rates_cli._seed_spec("5") == 5
    assert rates_cli._seed_spec(" 3 ") == 3


def test_seed_spec_list_and_dedupe() -> None:
    from fplan.cli import rates as rates_cli

    assert rates_cli._seed_spec("[1,2,3]") == [1, 2, 3]
    assert rates_cli._seed_spec("[ 1, 2 , 2, 1 ]") == [1, 2]


@pytest.mark.parametrize(
    "bad",
    [
        "abc",
        "0",
        "-1",
        "[]",
        "[1,x]",
        "[1,2",
        "1.5",
        "3000000000",  # count > SEED_MAX (would blow up random.sample)
        "[0]",  # explicit seed below range
        "[2147483648]",  # explicit seed above SEED_MAX
    ],
)
def test_seed_spec_bad_syntax(bad: str) -> None:
    from fplan.cli import rates as rates_cli

    with pytest.raises(ValueError):
        rates_cli._seed_spec(bad)


def test_seed_and_seeds_mutually_exclusive(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--seed", "1", "--seeds", "3"])
    assert r.exit_code == 2 and "mutually exclusive" in (r.stdout + (r.stderr or ""))


def test_out_and_seeds_mutually_exclusive(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "2", "--out", "x.yaml"])
    assert r.exit_code == 2 and "--out cannot be combined" in (
        r.stdout + (r.stderr or "")
    )


def test_search_bad_seeds_syntax(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "nope"])
    assert r.exit_code == 2 and "bad --seeds" in (r.stdout + (r.stderr or ""))


def test_search_oversized_count_is_clean_usage_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # A count beyond the seed population must be a clean exit-2, never a raw
    # traceback from random.sample (the path runs before run lookup).
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "3000000000"])
    assert r.exit_code == 2 and "exceeds" in (r.stdout + (r.stderr or ""))
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_jobs_without_seeds_is_usage_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--jobs", "4"])
    assert r.exit_code == 2 and "only apply to a --seeds search" in (
        r.stdout + (r.stderr or "")
    )


def test_quiet_solver_without_seeds_is_usage_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--quiet-solver"])
    assert r.exit_code == 2 and "only apply to a --seeds search" in (
        r.stdout + (r.stderr or "")
    )


def test_search_dry_run(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "3", "--dry-run"])
    assert r.exit_code == 0 and "would solve 3 seed" in r.stdout
    assert not (run_mod.run_dir("r") / "rates-search").exists()


def test_search_force_promotes_best(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 250.0, 2: 240.0, 3: 260.0}))
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2,3]", "--jobs", "1", "--force"]
    )
    assert r.exit_code == 0 and "★ best" in r.stdout
    rd = run_mod.run_dir("r")
    search = rd / "rates-search"
    assert (search / "seed-1.yaml").exists()
    assert (search / "seed-2.yaml").exists()
    assert (search / "summary.yaml").exists()
    # Best (seed 2, t_FINAL=240) promoted; rates.yaml is a copy of its candidate.
    assert (rd / "rates.yaml").read_text() == (search / "seed-2.yaml").read_text()
    m = run_mod.load(rd)
    assert m.extra["l2"]["seed"] == 2 and m.extra["l2"]["objective_s"] == 240.0
    assert m.extra["l2"]["search"]["seeds"] == [1, 2, 3]
    assert m.extra["l2"]["search"]["promoted_from"] == "rates-search/seed-2.yaml"
    summary = yaml.safe_load((search / "summary.yaml").read_text())
    assert summary["best_seed"] == 2 and summary["seeds"] == [1, 2, 3]
    assert len(summary["candidates"]) == 3


def test_search_count_runs_n_random_seeds(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    _patch_solve(monkeypatch, _fake_solve_any())
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "3", "--jobs", "1", "--force"]
    )
    assert r.exit_code == 0
    summary = yaml.safe_load(
        (run_mod.run_dir("r") / "rates-search" / "summary.yaml").read_text()
    )
    assert len(summary["seeds"]) == 3 and len(set(summary["seeds"])) == 3
    assert len(summary["candidates"]) == 3
    assert run_mod.load(run_mod.run_dir("r")).extra["l2"]["seed"] in summary["seeds"]


def test_search_all_infeasible_exits_nonzero(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: None, 2: None}))
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2]", "--jobs", "1", "--force"]
    )
    assert r.exit_code == 1 and "no feasible incumbent" in r.stdout
    rd = run_mod.run_dir("r")
    assert (rd / "rates-search" / "summary.yaml").exists()
    assert not (rd / "rates.yaml").exists()
    assert "l2" not in run_mod.load(rd).extra


def test_search_one_seed_errors_continues(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)

    def _solve(inst, model, *, seed, **kw):
        if seed == 2:
            raise RuntimeError("boom")
        obj = 250.0 if seed == 1 else 230.0
        return (
            types.SimpleNamespace(
                objective=obj, status="optimal", solve_time_s=1.0, seed=None
            ),
            None,
            None,
        )

    _patch_solve(monkeypatch, _solve)
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2,3]", "--jobs", "1", "--force"]
    )
    assert r.exit_code == 0
    rd = run_mod.run_dir("r")
    assert not (rd / "rates-search" / "seed-2.yaml").exists()
    summary = yaml.safe_load((rd / "rates-search" / "summary.yaml").read_text())
    statuses = {c["seed"]: c["status"] for c in summary["candidates"]}
    assert statuses[2] == "error"
    # Best feasible is seed 3 (230).
    assert run_mod.load(rd).extra["l2"]["seed"] == 3


def test_search_noninteractive_skips_promotion(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0, 2: 250.0}))
    # CliRunner has no tty → non-interactive; without --force, leave candidates.
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2]", "--jobs", "1"])
    assert r.exit_code == 0 and "non-interactive" in r.stdout
    rd = run_mod.run_dir("r")
    assert (rd / "rates-search" / "seed-1.yaml").exists()
    assert not (rd / "rates.yaml").exists()
    assert "l2" not in run_mod.load(rd).extra


def test_search_interactive_promote_yes(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0, 2: 250.0}))
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2]", "--jobs", "1"], input="y\n"
    )
    assert r.exit_code == 0
    assert run_mod.load(run_mod.run_dir("r")).extra["l2"]["seed"] == 1


def test_search_interactive_promote_declined(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0, 2: 250.0}))
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2]", "--jobs", "1"], input="n\n"
    )
    assert r.exit_code == 0 and "Not promoted" in r.stdout
    rd = run_mod.run_dir("r")
    assert not (rd / "rates.yaml").exists()
    assert "l2" not in run_mod.load(rd).extra


def test_search_interactive_overwrite_declined(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    rd = run_mod.run_dir("r")
    (rd / "rates.yaml").write_text("OLD\n")
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0}))
    # Promote yes, then decline the overwrite of the existing rates.yaml.
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1]", "--jobs", "1"], input="y\nn\n"
    )
    assert r.exit_code == 0 and "kept" in r.stdout
    assert (rd / "rates.yaml").read_text() == "OLD\n"
    assert "l2" not in run_mod.load(rd).extra


def test_out_export_single_leaves_manifest(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    _patch_solve(monkeypatch, _fake_solve())
    out = tmp_path / "export.yaml"
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seed", "7", "--out", str(out), "--force"]
    )
    assert r.exit_code == 0 and "manifest not updated" in r.stdout
    assert out.exists()
    rd = run_mod.run_dir("r")
    assert not (rd / "rates.yaml").exists()
    assert "l2" not in run_mod.load(rd).extra


class _InlineExecutor:
    """In-process stand-in for ProcessPoolExecutor (see test_l2_search)."""

    def __init__(self, max_workers=None, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args, **kw):
        import concurrent.futures as cf

        fut: cf.Future = cf.Future()
        try:
            fut.set_result(fn(*args, **kw))
        except Exception as exc:
            fut.set_exception(exc)
        return fut


def test_search_parallel_promotes_best(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import search as l2_search

    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 250.0, 2: 240.0}))
    # Run the parallel branch in-process so the mocked solve applies.
    monkeypatch.setattr(l2_search, "ProcessPoolExecutor", _InlineExecutor)
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2]", "--jobs", "2", "--force"]
    )
    assert r.exit_code == 0 and "in parallel" in r.stdout
    rd = run_mod.run_dir("r")
    search = rd / "rates-search"
    assert (search / "seed-1.yaml").exists() and (search / "seed-2.yaml").exists()
    # Per-seed logs are written and their paths announced for monitoring.
    assert (search / "seed-1.log").exists() and (search / "seed-2.log").exists()
    assert "seed-1.log" in r.stdout and "tail -f" in r.stdout
    assert run_mod.load(rd).extra["l2"]["seed"] == 2  # best (t_FINAL=240)
    summary = yaml.safe_load((search / "summary.yaml").read_text())
    assert summary["jobs"] == 2
    # Full SCIP progress by default in parallel; candidates record their log.
    assert summary["solver"]["verbose"] is True
    assert {c["seed"]: c["log"] for c in summary["candidates"]}[2] == "seed-2.log"
    # Promotion provenance: the manifest search block records jobs + summary.
    sblock = run_mod.load(rd).extra["l2"]["search"]
    assert sblock["jobs"] == 2
    assert sblock["summary"] == "rates-search/summary.yaml"


def test_search_parallel_quiet_solver(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    from fplan.l2 import search as l2_search

    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0, 2: 250.0}))
    monkeypatch.setattr(l2_search, "ProcessPoolExecutor", _InlineExecutor)
    r = runner.invoke(
        app,
        [
            "rates",
            "solve",
            "r",
            "--seeds",
            "[1,2]",
            "--jobs",
            "2",
            "--quiet-solver",
            "--force",
        ],
    )
    assert r.exit_code == 0
    # Banner reflects silencing; no tail -f guidance toward near-empty logs.
    assert "silenced" in r.stdout and "tail -f" not in r.stdout
    summary = yaml.safe_load(
        (run_mod.run_dir("r") / "rates-search" / "summary.yaml").read_text()
    )
    assert summary["solver"]["verbose"] is False  # SCIP silenced → near-empty logs


def test_search_mixed_feasible_and_infeasible(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    # Seed 2 finds no incumbent (infeasible, not an error) — the motivating case.
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0, 2: None, 3: 250.0}))
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2,3]", "--jobs", "1", "--force"]
    )
    assert r.exit_code == 0
    rd = run_mod.run_dir("r")
    assert run_mod.load(rd).extra["l2"]["seed"] == 1  # best feasible
    summary = yaml.safe_load((rd / "rates-search" / "summary.yaml").read_text())
    byseed = {c["seed"]: c for c in summary["candidates"]}
    assert byseed[2]["objective_s"] is None and byseed[2]["status"] == "infeasible"
    assert "error" not in byseed[2]  # infeasible is not an error
    assert (rd / "rates.yaml").exists()


def test_search_clears_prior_search_residue(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    _patch_solve(monkeypatch, _fake_solve_any())
    rd = run_mod.run_dir("r")
    # First search over {1,2,3}, then a second over {4,5} into the same run.
    runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[1,2,3]", "--jobs", "1", "--force"]
    )
    r = runner.invoke(
        app, ["rates", "solve", "r", "--seeds", "[4,5]", "--jobs", "1", "--force"]
    )
    assert r.exit_code == 0
    search = rd / "rates-search"
    present = {p.name for p in search.glob("seed-*.yaml")}
    assert present == {"seed-4.yaml", "seed-5.yaml"}  # 1/2/3 cleared
    summary = yaml.safe_load((search / "summary.yaml").read_text())
    assert summary["seeds"] == [4, 5]


# --- rates post ------------------------------------------------------------

# A minimal solved-rates doc: 100 widgets built in step 0, consumed in step 1.
POST_RATES = {
    "scenario": "t",
    "mode": "lower-bound",
    "l1_method": "forward",
    "initial_time_s": 0.0,
    "steps": [
        {
            "label": "s0",
            "duration_s": 10.0,
            "items": [
                {
                    "name": "widget",
                    "produced": 100.0,
                    "production_rate_per_s": 10.0,
                    "consumption_rate_per_s": 0.0,
                    "consumed": 0.0,
                    "count_start": 0.0,
                    "count_end": 100.0,
                }
            ],
        },
        {
            "label": "s1",
            "duration_s": 10.0,
            "items": [
                {
                    "name": "widget",
                    "produced": 0.0,
                    "production_rate_per_s": 0.0,
                    "consumption_rate_per_s": 10.0,
                    "consumed": 100.0,
                    "count_start": 100.0,
                    "count_end": 0.0,
                }
            ],
        },
    ],
}


def _make_run_with_rates(tmp_path: Path, name: str = "r") -> Path:
    _make_run(tmp_path, name)
    rd = run_mod.run_dir(name)
    (rd / "rates.yaml").write_text(yaml.safe_dump(POST_RATES))
    return rd


def test_post_run_not_found(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["rates", "post", "nope"])
    assert r.exit_code == 1 and "not found" in (r.stdout + (r.stderr or ""))


def test_post_unknown_method(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run_with_rates(tmp_path)
    r = runner.invoke(app, ["rates", "post", "r", "--method", "bogus"])
    assert r.exit_code == 2 and "unknown method" in (r.stdout + (r.stderr or ""))


def test_post_rates_missing(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)  # manifest but no rates.yaml
    r = runner.invoke(app, ["rates", "post", "r"])
    assert r.exit_code == 1 and "rates file not found" in (r.stdout + (r.stderr or ""))
    assert "rates solve" in (r.stdout + (r.stderr or ""))


def test_post_dry_run_writes_nothing(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    r = runner.invoke(app, ["rates", "post", "r", "--dry-run"])
    assert r.exit_code == 0 and "dry run" in r.stdout
    assert "rates-post.yaml" in r.stdout
    assert not (rd / "rates-post.yaml").exists()
    assert not (rd / "viz").exists()


def test_post_writes_output_viz_and_manifest(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    r = runner.invoke(app, ["rates", "post", "r"])
    assert r.exit_code == 0, r.stdout + (r.stderr or "")

    out = rd / "rates-post.yaml"
    assert out.exists()
    doc = yaml.safe_load(out.read_text())
    assert doc["post"]["method"] == "tube"
    assert doc["post"]["source"] == "rates.yaml"
    # Production flattened (10,0 → 5,5); inventory passes through.
    assert doc["steps"][0]["items"][0]["production_rate_per_s"] == pytest.approx(5.0)

    assert (rd / "viz" / "rates-post-timeline.html").exists()
    # No companion heatmap for the diff view.
    assert not (rd / "viz" / "rates-post-heatmap.html").exists()

    m = run_mod.load(rd)
    assert m.extra["post"]["method"] == "tube"
    assert "summary" in m.extra["post"]


def test_post_no_viz(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz"])
    assert r.exit_code == 0
    assert (rd / "rates-post.yaml").exists()
    assert not (rd / "viz").exists()


@pytest.mark.parametrize("method", ["tube", "chord", "mrp"])
def test_post_methods(tmp_path, monkeypatch, use_fixture_model, method) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    r = runner.invoke(app, ["rates", "post", "r", "--method", method, "--no-viz"])
    assert r.exit_code == 0
    assert (
        yaml.safe_load((rd / "rates-post.yaml").read_text())["post"]["method"] == method
    )


def test_post_from_override_still_writes_canonical(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    cand = rd / "rates-search" / "seed-9.yaml"
    cand.parent.mkdir(parents=True)
    cand.write_text(yaml.safe_dump(POST_RATES))
    r = runner.invoke(app, ["rates", "post", "r", "--from", str(cand), "--no-viz"])
    assert r.exit_code == 0
    out = rd / "rates-post.yaml"
    assert out.exists()
    # source is recorded run-dir-relative (so the overlay resolves under the post
    # file's dir and a crafted ../ traversal can't be stored).
    assert (
        yaml.safe_load(out.read_text())["post"]["source"] == "rates-search/seed-9.yaml"
    )


def test_post_open_invokes_helper(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run_with_rates(tmp_path)
    called: dict = {}
    monkeypatch.setattr(
        rates_cli, "_open_in_browser", lambda p: called.setdefault("p", p)
    )
    r = runner.invoke(app, ["rates", "post", "r", "--open"])
    assert r.exit_code == 0
    assert called["p"].name == "rates-post-timeline.html"


def test_post_open_with_no_viz_notes_noop(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run_with_rates(tmp_path)
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz", "--open"])
    assert r.exit_code == 0
    assert "--open has no effect with --no-viz" in r.stdout


def test_post_viz_failure_still_writes_data(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    # A viz render failure must not lose the data output or fail the command.
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    from fplan.l2 import viz as l2_viz

    def _boom(*a, **k):
        raise ValueError("render exploded")

    monkeypatch.setattr(l2_viz, "render_flatten_html", _boom)
    r = runner.invoke(app, ["rates", "post", "r"])
    assert r.exit_code == 0  # data still written, exit 0
    assert (rd / "rates-post.yaml").exists()
    assert "could not render viz" in (r.stdout + (r.stderr or ""))


def test_post_existing_without_force_refuses(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    (rd / "rates-post.yaml").write_text("pre-existing: true\n")
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz"])  # non-interactive
    assert r.exit_code == 1 and "already exists" in (r.stdout + (r.stderr or ""))
    # Untouched.
    assert yaml.safe_load((rd / "rates-post.yaml").read_text()) == {
        "pre-existing": True
    }


def test_post_force_overwrites(tmp_path, monkeypatch, use_fixture_model) -> None:
    monkeypatch.chdir(tmp_path)
    rd = _make_run_with_rates(tmp_path)
    (rd / "rates-post.yaml").write_text("pre-existing: true\n")
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz", "--force"])
    assert r.exit_code == 0
    assert "post" in yaml.safe_load((rd / "rates-post.yaml").read_text())


def test_post_malformed_yaml_clean_error(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    rd = run_mod.run_dir("r")
    (rd / "rates.yaml").write_text("steps: not-a-list\n")  # str → AttributeError
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz"])
    assert r.exit_code == 1 and "malformed rates YAML" in (r.stdout + (r.stderr or ""))
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_post_not_a_mapping_clean_error(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    rd = run_mod.run_dir("r")
    (rd / "rates.yaml").write_text("- just\n- a\n- list\n")
    r = runner.invoke(app, ["rates", "post", "r", "--no-viz"])
    assert r.exit_code == 1 and "not a valid rates YAML" in (
        r.stdout + (r.stderr or "")
    )
