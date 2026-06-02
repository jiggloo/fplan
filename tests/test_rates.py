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
