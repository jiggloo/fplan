"""Tests for file references (fplan.refs), the run manifest domain
(fplan.run), and the `run create/clone/show/full` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fplan import refs
from fplan import run as run_mod
from fplan.cli import app

runner = CliRunner()


# --------------------------------------------------------------------------- #
# refs.py
# --------------------------------------------------------------------------- #


def test_file_ref_and_is_current(tmp_path: Path) -> None:
    p = tmp_path / "f.yaml"
    p.write_text("a: 1\n")
    ref = refs.file_ref(p, name="demo")
    assert ref["name"] == "demo" and ref["path"] == str(p)
    assert ref["sha256"] == refs.sha256_of(p)
    assert refs.is_current(ref) is True
    # No name → no name key.
    assert "name" not in refs.file_ref(p)
    # Drift → False; missing file → None; incomplete ref → None.
    p.write_text("a: 2\n")
    assert refs.is_current(ref) is False
    p.unlink()
    assert refs.is_current(ref) is None
    assert refs.is_current({"path": "x"}) is None
    # A non-mapping ref (e.g. a hand-edited manifest) is "can't tell", not a crash.
    assert refs.is_current("just-a-string") is None


# --------------------------------------------------------------------------- #
# run.py — manifest domain
# --------------------------------------------------------------------------- #


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    scn = tmp_path / "s.yaml"
    scn.write_text("name: s\n")
    order = tmp_path / "o.yaml"
    order.write_text("level: 1\n")
    mp = tmp_path / "m.yaml"
    mp.write_text("seed: 1\n")
    return scn, order, mp


def test_manifest_new_clone_roundtrip(tmp_path: Path) -> None:
    scn, order, mp = _inputs(tmp_path)
    m = run_mod.Manifest.new(
        "r1", scenario=scn, tech_order=order, map_path=mp, created="t0"
    )
    assert set(m.inputs) == {"scenario", "tech-order", "map"}
    # Clone keeps the input bindings with a fresh identity.
    c = m.cloned("r3", created="t1")
    assert c.run == "r3" and c.inputs == m.inputs and c.created == "t1"


def test_manifest_preserves_unknown_keys(tmp_path: Path) -> None:
    # A newer manifest with stage settings round-trips without losing them.
    directory = tmp_path / "runs" / "r"
    directory.mkdir(parents=True)
    raw = {
        "fplan_version": "9.9.9",
        "run": "r",
        "created": "t",
        "inputs": {"scenario": {"path": "s.yaml", "sha256": "h"}},
        "l2": {"mode": "experimental", "seed": 7},  # unknown to this version
    }
    (directory / run_mod.MANIFEST_NAME).write_text(yaml.safe_dump(raw))
    m = run_mod.load(directory)
    assert m.extra == {"l2": {"mode": "experimental", "seed": 7}}
    run_mod.save(directory, m)
    assert yaml.safe_load((directory / run_mod.MANIFEST_NAME).read_text()) == raw


def test_load_non_mapping_manifest_raises(tmp_path: Path) -> None:
    directory = tmp_path / "runs" / "r"
    directory.mkdir(parents=True)
    (directory / run_mod.MANIFEST_NAME).write_text("- not a mapping\n")
    with pytest.raises(ValueError):
        run_mod.load(directory)


def test_from_dict_rejects_non_mapping_inputs() -> None:
    # A manifest whose `inputs` is a list would crash show/clone downstream.
    with pytest.raises(ValueError):
        run_mod.Manifest.from_dict({"run": "r", "inputs": ["bad"]})


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "/abs", "../escape"])
def test_run_dir_rejects_unsafe_names(bad) -> None:
    with pytest.raises(ValueError):
        run_mod.run_dir(bad)


def test_run_dir_accepts_plain_name() -> None:
    assert run_mod.run_dir("ok") == run_mod.RUNS_DIR / "ok"


def test_cloned_stamps_current_version(tmp_path: Path) -> None:
    from fplan import __version__

    scn, order, mp = _inputs(tmp_path)
    src = run_mod.Manifest.new(
        "r1", scenario=scn, tech_order=order, map_path=mp, created="t0", version="0.0.1"
    )
    assert src.cloned("r2", created="t1").fplan_version == __version__


def test_stage_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "runs" / "r"
    directory.mkdir(parents=True)
    (directory / run_mod.MANIFEST_NAME).write_text("run: r\n")
    assert run_mod.stage_artifacts(directory) == []
    (directory / "rates.yaml").write_text("x: 1\n")
    assert run_mod.stage_artifacts(directory) == ["rates.yaml"]


# --------------------------------------------------------------------------- #
# CLI — run create / clone / show / full
# --------------------------------------------------------------------------- #


def _create(
    name: str = "r1", *extra: str, scenario="s.yaml", tech_order="o.yaml", map="m.yaml"
):
    """Invoke `run create` with all inputs by default; pass a kwarg None to omit
    it, or `*extra` for trailing flags like --dry-run."""
    args = ["run", "create", name]
    if scenario is not None:
        args += ["--scenario", scenario]
    if tech_order is not None:
        args += ["--tech-order", tech_order]
    if map is not None:
        args += ["--map", map]
    return runner.invoke(app, [*args, *extra])


def test_create_writes_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    assert _create("r1").exit_code == 0
    m = run_mod.load(run_mod.run_dir("r1"))
    assert set(m.inputs) == {"scenario", "tech-order", "map"} and m.created


def test_create_requires_map(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    # Missing the required --map option is a usage error (exit 2).
    assert _create("r1", map=None).exit_code == 2


@pytest.mark.parametrize("missing", ["s.yaml", "o.yaml", "m.yaml"])
def test_create_missing_input_is_fatal(tmp_path, monkeypatch, missing) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    (tmp_path / missing).unlink()
    r = _create("r1")
    assert r.exit_code == 1 and "not found" in (r.stdout + (r.stderr or ""))


def test_create_refuses_existing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    assert _create("r1").exit_code == 0
    r = _create("r1")
    assert r.exit_code == 1 and "already exists" in (r.stdout + (r.stderr or ""))


def test_create_dry_run_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    r = _create("r1", "--dry-run")
    assert r.exit_code == 0 and "dry run" in r.stdout
    assert not run_mod.run_dir("r1").exists()


def test_create_save_failure_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(run_mod, "save", boom)
    r = _create("r1")
    assert r.exit_code == 1 and "could not create run" in (r.stdout + (r.stderr or ""))


def test_create_rejects_unsafe_name(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    r = _create("../../escaped")
    assert r.exit_code == 2 and "invalid run name" in (r.stdout + (r.stderr or ""))
    # Nothing was written outside runs/.
    assert not (tmp_path.parent.parent / "escaped").exists()


def test_show_malformed_inputs_is_clean_error(tmp_path, monkeypatch) -> None:
    # `inputs` as a non-mapping must be a clean exit, not a raw traceback.
    monkeypatch.chdir(tmp_path)
    directory = run_mod.run_dir("r1")
    directory.mkdir(parents=True)
    (directory / run_mod.MANIFEST_NAME).write_text("run: r1\ninputs:\n- bad\n")
    r = runner.invoke(app, ["run", "show", "r1"])
    assert r.exit_code == 1
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_clone_malformed_inputs_is_clean_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    directory = run_mod.run_dir("r1")
    directory.mkdir(parents=True)
    (directory / run_mod.MANIFEST_NAME).write_text("run: r1\ninputs:\n- bad\n")
    r = runner.invoke(app, ["run", "clone", "r1", "r2"])
    assert r.exit_code == 1
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_clone_copies_inputs_drops_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    _create("r1")
    # Simulate a stage artifact from a prior solve.
    (run_mod.run_dir("r1") / "rates.yaml").write_text("x: 1\n")
    r = runner.invoke(app, ["run", "clone", "r1", "r2"])
    assert r.exit_code == 0
    assert (
        run_mod.load(run_mod.run_dir("r2")).inputs
        == run_mod.load(run_mod.run_dir("r1")).inputs
    )
    assert run_mod.stage_artifacts(run_mod.run_dir("r2")) == []  # artifact not copied


def test_clone_source_missing_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["run", "clone", "nope", "r2"])
    assert r.exit_code == 1 and "not found" in (r.stdout + (r.stderr or ""))


def test_clone_dest_exists_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    _create("r1")
    _create("r2")
    r = runner.invoke(app, ["run", "clone", "r1", "r2"])
    assert r.exit_code == 1 and "already exists" in (r.stdout + (r.stderr or ""))


def test_clone_dry_run_and_bad_source(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    _create("r1")
    dry = runner.invoke(app, ["run", "clone", "r1", "r2", "--dry-run"])
    assert dry.exit_code == 0 and "dry run" in dry.stdout
    assert not run_mod.run_dir("r2").exists()
    # Malformed source manifest → clean error.
    (run_mod.run_dir("r1") / run_mod.MANIFEST_NAME).write_text("- bad\n")
    assert runner.invoke(app, ["run", "clone", "r1", "r3"]).exit_code == 1


def test_clone_save_failure_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    _create("r1")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(run_mod, "save", boom)
    r = runner.invoke(app, ["run", "clone", "r1", "r2"])
    assert r.exit_code == 1 and "could not create run" in (r.stdout + (r.stderr or ""))


def test_show_reports_status_and_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    scn, order, mp = _inputs(tmp_path)
    _create("r1")
    # scenario current, tech-order changed, map missing.
    order.write_text("level: 2\n")
    mp.unlink()
    (run_mod.run_dir("r1") / "rates.yaml").write_text("x: 1\n")
    r = runner.invoke(app, ["run", "show", "r1"])
    assert r.exit_code == 0
    out = r.stdout
    assert "✓ current" in out and "⚠ changed" in out and "✗ missing" in out
    assert "rates.yaml" in out and "run: r1" in out


def test_show_missing_run_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["run", "show", "nope"])
    assert r.exit_code == 1 and "not found" in (r.stdout + (r.stderr or ""))


def test_show_bad_manifest_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    directory = run_mod.run_dir("r1")
    directory.mkdir(parents=True)
    (directory / run_mod.MANIFEST_NAME).write_text("- bad\n")
    assert runner.invoke(app, ["run", "show", "r1"]).exit_code == 1


def test_show_tolerates_missing_input_key(tmp_path, monkeypatch) -> None:
    # A manifest lacking an input (e.g. hand-edited) is shown, not crashed.
    monkeypatch.chdir(tmp_path)
    directory = run_mod.run_dir("r1")
    directory.mkdir(parents=True)
    (directory / run_mod.MANIFEST_NAME).write_text(
        "run: r1\ninputs:\n  scenario: {path: s.yaml, sha256: x}\n"
    )
    r = runner.invoke(app, ["run", "show", "r1"])
    assert r.exit_code == 0
    assert "scenario:" in r.stdout and "map:" not in r.stdout


def test_show_no_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _inputs(tmp_path)
    _create("r1")
    r = runner.invoke(app, ["run", "show", "r1"])
    assert r.exit_code == 0 and "(none yet)" in r.stdout


def test_run_full_is_stub() -> None:
    r = runner.invoke(app, ["run", "full", "anything"])
    assert r.exit_code == 71 and "not implemented" in r.stdout
