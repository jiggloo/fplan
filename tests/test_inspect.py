"""Tests for `inspect` (tech/item/recipe detail + list/filter), against the
model fixture."""

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


def test_inspect_tech_filter_shows_details(use_fixture_model) -> None:
    # --filter shows full detail for each match, not just names, so a search and
    # an inspect are one call.
    result = runner.invoke(app, ["inspect", "tech", "--filter", "science"])
    assert result.exit_code == 0
    out = result.stdout
    assert "logistic-science-pack" in out
    assert "prerequisites:" in out  # detail rows, not a bare name list
    # Every name line names a matching tech (detail rows are indented, names are
    # the only column-0 lines).
    names = [ln for ln in out.splitlines() if ln and not ln.startswith(" ")]
    assert names and all("science" in ln for ln in names)


def test_inspect_tech_filter_no_match(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech", "--filter", "zzzzz"])
    assert result.exit_code == 0
    assert "no technologies match" in result.stdout


def test_inspect_tech_bare_lists_all(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "tech"])
    assert result.exit_code == 0
    assert "rocket-silo" in result.stdout.split()
    assert len(result.stdout.split()) == 38  # the fixture's full tech set


def test_inspect_item_detail(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "item", "iron-plate"])
    assert result.exit_code == 0
    out = result.stdout
    assert "iron-plate" in out
    assert "stack size:" in out
    assert "produced by:" in out
    assert "consumed by:" in out  # gears/circuits consume iron-plate
    assert "unlocked by:" in out


def test_inspect_item_unknown_is_fatal(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "item", "no-such-item"])
    assert result.exit_code == 1


def test_inspect_item_bare_lists_all(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "item"])
    assert result.exit_code == 0
    assert "iron-plate" in result.stdout.split()


def test_inspect_item_filter_shows_details(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "item", "--filter", "ore"])
    assert result.exit_code == 0
    out = result.stdout
    assert "iron-ore" in out
    assert "produced by:" in out  # detail rows, not a bare name list


def test_inspect_recipe_detail(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "recipe", "iron-plate"])
    assert result.exit_code == 0
    out = result.stdout
    assert "iron-plate" in out
    assert "ingredients:" in out
    assert "outputs:" in out
    assert "iron-ore" in out  # the ingredient
    assert "made in:" in out
    assert "unlocked by:" in out


def test_inspect_recipe_kind_marker(use_fixture_model) -> None:
    # A non-crafting recipe is flagged with its kind on the name line.
    result = runner.invoke(app, ["inspect", "recipe", "mine/iron-ore"])
    assert result.exit_code == 0
    assert "*(mining)*" in result.stdout


def test_inspect_recipe_unknown_is_fatal(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "recipe", "no-such-recipe"])
    assert result.exit_code == 1


def test_inspect_recipe_filter_shows_details(use_fixture_model) -> None:
    result = runner.invoke(app, ["inspect", "recipe", "--filter", "oil"])
    assert result.exit_code == 0
    out = result.stdout
    assert "ingredients:" in out  # detail rows for each oil recipe
    names = [ln for ln in out.splitlines() if ln and not ln.startswith(" ")]
    assert names and all("oil" in ln for ln in names)
