"""Tests for the shared effective-settings logging helper."""

from __future__ import annotations

from fplan.cli._log import echo_settings


def test_echo_settings_marks_defaults(capsys) -> None:
    echo_settings([("a", "1", True), ("b", "2", False)])
    out = capsys.readouterr().out
    assert out.strip() == "settings: a=1 (default)  ·  b=2"


def test_echo_settings_empty_is_noop(capsys) -> None:
    echo_settings([])
    assert capsys.readouterr().out == ""
