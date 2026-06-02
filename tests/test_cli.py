"""Surface tests for the CLI skeleton.

These assert the command tree is navigable (help renders at every level) and
that every un-built leaf exits with the reserved stub code — not that any stage
does real work yet.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from fplan.cli import app
from fplan.cli._stub import EXIT_NOT_IMPLEMENTED, EXIT_NOT_MIGRATED

runner = CliRunner()

GROUPS = ["tech-order", "rates", "map", "layout", "inspect", "execution"]

# (argv, expected exit code) for every leaf command in the tree.
LEAVES = [
    (["tech-order", "build"], EXIT_NOT_MIGRATED),
    (["tech-order", "verify"], EXIT_NOT_MIGRATED),
    (["tech-order", "viz"], EXIT_NOT_IMPLEMENTED),
    (["rates", "solve"], EXIT_NOT_MIGRATED),
    (["rates", "post"], EXIT_NOT_MIGRATED),
    (["rates", "viz"], EXIT_NOT_MIGRATED),
    (["map", "from-string"], EXIT_NOT_IMPLEMENTED),
    (["map", "from-save"], EXIT_NOT_MIGRATED),
    (["map", "show"], EXIT_NOT_IMPLEMENTED),
    (["layout", "place"], EXIT_NOT_MIGRATED),
    (["layout", "post"], EXIT_NOT_IMPLEMENTED),
    (["layout", "viz"], EXIT_NOT_MIGRATED),
    (["inspect", "tech"], EXIT_NOT_MIGRATED),
    (["inspect", "item"], EXIT_NOT_MIGRATED),
    (["inspect", "recipe"], EXIT_NOT_MIGRATED),
    (["execution", "generate"], EXIT_NOT_IMPLEMENTED),
    (["execution", "viz"], EXIT_NOT_IMPLEMENTED),
    (["full-run"], EXIT_NOT_IMPLEMENTED),
]

# Commands that accept --dry-run.
DRY_RUN_OK = [
    ["tech-order", "build"],
    ["rates", "solve"],
    ["rates", "post"],
    ["map", "from-string"],
    ["map", "from-save"],
    ["layout", "place"],
    ["layout", "post"],
    ["execution", "generate"],
    ["full-run"],
    ["init"],
]


def test_bare_invocation_prints_working_directory() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "working directory" in result.stdout


def test_version_flag_prints_version() -> None:
    from fplan import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_root_help_lists_every_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in GROUPS:
        assert group in result.stdout


@pytest.mark.parametrize("group", GROUPS)
def test_group_help_renders(group: str) -> None:
    result = runner.invoke(app, [group, "--help"])
    assert result.exit_code == 0


@pytest.mark.parametrize("argv, expected_code", LEAVES)
def test_leaf_help_renders(argv: list[str], expected_code: int) -> None:
    result = runner.invoke(app, [*argv, "--help"])
    assert result.exit_code == 0


@pytest.mark.parametrize("argv, expected_code", LEAVES)
def test_leaf_exits_with_reserved_stub_code(
    argv: list[str], expected_code: int
) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == expected_code


@pytest.mark.parametrize("argv", DRY_RUN_OK)
def test_dry_run_flag_is_accepted(argv: list[str]) -> None:
    # --dry-run is a recognized option (not a usage error / exit code 2).
    result = runner.invoke(app, [*argv, "--dry-run"])
    assert result.exit_code != 2
