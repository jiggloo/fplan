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


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "[]", "[1,x]", "[1,2", "1.5"])
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2,3]", "--force"])
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "3", "--force"])
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2]", "--force"])
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2,3]", "--force"])
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2]"])
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2]"], input="y\n")
    assert r.exit_code == 0
    assert run_mod.load(run_mod.run_dir("r")).extra["l2"]["seed"] == 1


def test_search_interactive_promote_declined(
    tmp_path, monkeypatch, use_fixture_model
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    _patch_solve(monkeypatch, _fake_solve_by_seed({1: 240.0, 2: 250.0}))
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1,2]"], input="n\n")
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
    r = runner.invoke(app, ["rates", "solve", "r", "--seeds", "[1]"], input="y\nn\n")
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
