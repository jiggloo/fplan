"""Tests for `fplan init`, the bare-`fplan` config consumer, and the require helper."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from fplan import config as cfg
from fplan import factorio
from fplan.cli import app
from fplan.cli import main as cli_main

runner = CliRunner()


@pytest.fixture
def interactive(monkeypatch):
    """Force the prompt path on, regardless of the test runner's stdin."""
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)


# --------------------------------------------------------------------------- #
# bare `fplan` config consumer
# --------------------------------------------------------------------------- #


def test_bare_reports_no_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "config: none found" in result.stdout


def test_bare_reports_unset_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / cfg.DEFAULT_CONFIG_NAME).write_text(cfg.render_config(None, None))
    result = runner.invoke(app, [])
    assert "(unset)" in result.stdout


def test_bare_reports_ok_and_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / cfg.DEFAULT_CONFIG_NAME).write_text(
        cfg.render_config(str(tmp_path / "data"), str(tmp_path / "no-binary"))
    )
    result = runner.invoke(app, [])
    assert "[ok]" in result.stdout
    assert "[MISSING]" in result.stdout


def test_bare_config_file_option(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "custom.yaml"
    other.write_text(cfg.render_config("/x", "/y"))
    result = runner.invoke(app, ["--config-file", str(other)])
    assert str(other) in result.stdout


def test_bare_config_error_is_a_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--config-file", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 0
    assert "config: warning" in result.stdout


# --------------------------------------------------------------------------- #
# `fplan init`
# --------------------------------------------------------------------------- #


def test_init_no_overwrite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / cfg.DEFAULT_CONFIG_NAME
    existing.write_text("keep me\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "already exists" in result.stdout
    assert existing.read_text() == "keep me\n"


def test_init_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--dry-run"])
    assert "Would create" in result.stdout
    assert not (tmp_path / cfg.DEFAULT_CONFIG_NAME).exists()


def test_init_non_interactive_writes_template(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: False)
    result = runner.invoke(app, ["init"])
    assert "template" in result.stdout
    assert (tmp_path / cfg.DEFAULT_CONFIG_NAME).exists()
    conf = cfg.load_config()
    assert conf.data_dir is None


def test_init_declined_writes_template(tmp_path, monkeypatch, interactive) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"], input="n\n")
    assert "skipping the system scan" in result.stdout
    assert (tmp_path / cfg.DEFAULT_CONFIG_NAME).exists()


def test_init_detected(tmp_path, monkeypatch, interactive) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    install = factorio.FactorioInstall(tmp_path / "data", tmp_path / "factorio")
    monkeypatch.setattr(factorio, "detect", lambda platform: install)
    result = runner.invoke(app, ["init"], input="y\n")
    assert "detected Factorio paths" in result.stdout
    conf = cfg.load_config()
    assert conf.data_dir == tmp_path / "data"


def test_init_not_found_blank_prompt_writes_template(
    tmp_path, monkeypatch, interactive
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(factorio, "detect", lambda platform: None)
    result = runner.invoke(app, ["init"], input="y\n\n")
    assert "template" in result.stdout


def test_init_not_found_manual_path_unverified(
    tmp_path, monkeypatch, interactive
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(factorio, "detect", lambda platform: None)
    monkeypatch.setattr(factorio, "current_platform", lambda: "darwin")
    result = runner.invoke(app, ["init"], input=f"y\n{tmp_path / 'bogus'}\n")
    assert "verify them" in result.stdout
    conf = cfg.load_config()
    assert conf.data_dir is not None  # candidate written even though it doesn't exist


def test_init_untested_platform_warns(tmp_path, monkeypatch, interactive) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(factorio, "current_platform", lambda: "linux")
    monkeypatch.setattr(factorio, "detect", lambda platform: None)
    result = runner.invoke(app, ["init"], input="n\n")
    assert "untested on Linux" in result.stdout


def test_init_unrecognized_platform(tmp_path, monkeypatch, interactive) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(factorio, "current_platform", lambda: None)
    result = runner.invoke(app, ["init"])
    assert "unrecognized platform" in result.stdout
    assert (tmp_path / cfg.DEFAULT_CONFIG_NAME).exists()


def test_init_write_failure_is_fatal(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: False)
    target = tmp_path / "missing-dir" / "config.yaml"  # parent absent -> write fails
    result = runner.invoke(app, ["--config-file", str(target), "init"])
    assert result.exit_code == 1
    assert not target.exists()


def test_init_honors_config_file_target(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: False)
    target = tmp_path / "elsewhere.yaml"
    result = runner.invoke(app, ["--config-file", str(target), "init"])
    assert result.exit_code == 0
    assert target.exists()


# --------------------------------------------------------------------------- #
# require helper (fatal-to-stderr)
# --------------------------------------------------------------------------- #


def test_stdin_interactivity_helper_returns_bool() -> None:
    assert isinstance(cli_main._stdin_is_interactive(), bool)


def test_require_helper_ok(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    config = tmp_path / "c.yaml"
    config.write_text(cfg.render_config(str(tmp_path / "data"), None))
    assert cli_main.factorio_data_dir_or_exit(config) == tmp_path / "data"


def test_require_helper_fatal(tmp_path, capsys) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(cfg.render_config(None, None))
    with pytest.raises(typer.Exit) as excinfo:
        cli_main.factorio_data_dir_or_exit(config)
    assert excinfo.value.exit_code == 1
    assert "error:" in capsys.readouterr().err
