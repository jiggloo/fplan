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

GROUPS = ["tech-order", "rates", "map", "layout", "inspect", "execution", "run"]

# (argv, expected exit code) for every stub leaf in the tree. `run`'s
# create/clone/show are real commands with required args, so they're exercised
# in test_run.py rather than here; `run full` requires a name positional (so it
# can't be invoked bare) and is covered there too.
LEAVES = [
    (["tech-order", "viz"], EXIT_NOT_IMPLEMENTED),
    (["map", "from-string"], EXIT_NOT_IMPLEMENTED),
    (["layout", "place"], EXIT_NOT_MIGRATED),
    (["layout", "post"], EXIT_NOT_IMPLEMENTED),
    (["layout", "viz"], EXIT_NOT_MIGRATED),
    (["execution", "generate"], EXIT_NOT_IMPLEMENTED),
    (["execution", "viz"], EXIT_NOT_IMPLEMENTED),
]

# Commands that accept --dry-run. (`rates solve`/`rates post` also accept it but
# need a run argument, so they're exercised in test_rates.py instead.)
DRY_RUN_OK = [
    ["map", "from-string"],
    ["layout", "place"],
    ["layout", "post"],
    ["execution", "generate"],
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


# Each unbuilt command's --help must state its status, so the boundary of "what
# works today" is visible in the tool itself (no separate capability matrix to
# keep in sync). Drift-guarded: every stub in LEAVES is checked.
STUB_TAGS = {
    EXIT_NOT_MIGRATED: "(pending migration)",
    EXIT_NOT_IMPLEMENTED: "(not implemented)",
}


@pytest.mark.parametrize("argv, expected_code", LEAVES)
def test_stub_help_states_its_status(argv: list[str], expected_code: int) -> None:
    result = runner.invoke(app, [*argv, "--help"])
    assert result.exit_code == 0
    assert STUB_TAGS[expected_code] in result.stdout


@pytest.mark.parametrize("argv", DRY_RUN_OK)
def test_dry_run_flag_is_accepted(argv: list[str]) -> None:
    # --dry-run is a recognized option (not a usage error / exit code 2).
    result = runner.invoke(app, [*argv, "--dry-run"])
    assert result.exit_code != 2
