"""Tests for `inspect tech` (detail + list/filter), against the model fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fplan.cli import app
from fplan.cli import inspect as inspect_cli
from fplan.cli import main as cli_main
from fplan.model import GameModel, Technology, build_game_data, load_model

runner = CliRunner()
MODEL_FIXTURE = Path(__file__).parent / "fixtures" / "model_raw_subset.json"


@pytest.fixture
def use_fixture_model(monkeypatch):
    model: GameModel = load_model(
        raw=build_game_data(json.loads(MODEL_FIXTURE.read_text()))
    )
    monkeypatch.setattr(cli_main, "load_model_or_exit", lambda config_file: model)


def test_tech_detail_trigger_and_no_cost() -> None:
    # The fixture's 1.1 techs all use science-pack units; cover the trigger and
    # no-cost cost branches directly.
    trig = Technology(
        name="t",
        research_trigger={"type": "craft-item", "item": "iron-plate", "count": 50},
    )
    assert "trigger: craft 50 iron-plate" in inspect_cli._tech_detail(trig, {"t": trig})
    plain = Technology(name="p")
    assert "—" in inspect_cli._tech_detail(plain, {"p": plain})


def test_inspect_tech_detail(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech", "oil-processing"])
    assert result.exit_code == 0
    out = result.stdout
    assert "oil-processing" in out
    assert "cost:" in out
    assert "prerequisites:" in out
    assert "unlocks:" in out
    assert "required by:" in out  # other fixture techs depend on it


def test_inspect_tech_unknown_is_fatal(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech", "no-such-tech"])
    assert result.exit_code == 1


def test_inspect_tech_filter_lists_matches(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech", "--filter", "science"])
    assert result.exit_code == 0
    lines = result.stdout.split()
    assert "logistic-science-pack" in lines
    assert all("science" in name for name in lines)


def test_inspect_tech_filter_no_match(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech", "--filter", "zzzzz"])
    assert result.exit_code == 0
    assert "no technologies match" in result.stdout


def test_inspect_tech_bare_lists_all(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech"])
    assert result.exit_code == 0
    assert "rocket-silo" in result.stdout.split()
    assert len(result.stdout.split()) == 38  # the fixture's full tech set
