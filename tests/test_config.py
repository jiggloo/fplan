"""Tests for the config reader/writer and the Factorio-locating module."""

from __future__ import annotations

from pathlib import Path

import pytest

from fplan import config as cfg
from fplan import factorio

# --------------------------------------------------------------------------- #
# config.py
# --------------------------------------------------------------------------- #


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_load_absent_default_returns_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conf = cfg.load_config()
    assert conf.source is None
    assert not conf.present
    assert conf.data_dir is None and conf.binary is None


def test_load_default_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / cfg.DEFAULT_CONFIG_NAME, cfg.render_config("/d", "/b"))
    conf = cfg.load_config()
    assert conf.present and conf.source == tmp_path / cfg.DEFAULT_CONFIG_NAME
    assert conf.data_dir == Path("/d") and conf.binary == Path("/b")


def test_blank_values_are_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / cfg.DEFAULT_CONFIG_NAME, cfg.render_config(None, None))
    conf = cfg.load_config()
    assert conf.present
    assert conf.data_dir is None and conf.binary is None


def test_user_home_is_expanded(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", cfg.render_config("~/x", None))
    conf = cfg.load_config(p)
    assert conf.data_dir == Path.home() / "x"


def test_explicit_config_file_missing_raises() -> None:
    with pytest.raises(cfg.ConfigError, match="not found"):
        cfg.load_config(Path("/no/such/file.yaml"))


def test_unparsable_yaml_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "factorio: [unbalanced\n")
    with pytest.raises(cfg.ConfigError, match="could not read"):
        cfg.load_config(p)


def test_unreadable_path_raises_config_error(tmp_path: Path) -> None:
    # A directory passed as --config-file: exists, but read_text raises OSError.
    with pytest.raises(cfg.ConfigError, match="could not read"):
        cfg.load_config(tmp_path)


def test_non_string_scalar_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "factorio:\n  data_dir: false\n")
    with pytest.raises(cfg.ConfigError, match="must be a string path"):
        cfg.load_config(p)


def test_whitespace_value_is_unset(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", 'factorio:\n  data_dir: "   "\n')
    assert cfg.load_config(p).data_dir is None


def test_render_config_resists_quote_newline_injection(tmp_path: Path) -> None:
    # A path with a quote + newline + a fake `binary:` line must not corrupt the
    # file or inject a key.
    nasty = '/x"y\n  binary: "/tmp/evil"\n#'
    p = _write(tmp_path / "c.yaml", cfg.render_config(nasty, "/legit"))
    conf = cfg.load_config(p)
    assert conf.data_dir == Path(nasty)
    assert conf.binary == Path("/legit")


def test_non_mapping_top_level_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "- just\n- a\n- list\n")
    with pytest.raises(cfg.ConfigError, match="must be a mapping"):
        cfg.load_config(p)


def test_factorio_not_mapping_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "factorio: not-a-mapping\n")
    with pytest.raises(cfg.ConfigError, match="'factorio' must be a mapping"):
        cfg.load_config(p)


def test_missing_factorio_block_is_empty(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", "other: 1\n")
    conf = cfg.load_config(p)
    assert conf.present and conf.data_dir is None


def test_require_data_dir_unset_raises(tmp_path: Path) -> None:
    conf = cfg.FplanConfig(data_dir=None, binary=None, source=tmp_path / "c")
    with pytest.raises(cfg.ConfigError, match="no Factorio data directory"):
        cfg.require_data_dir(conf)


def test_require_data_dir_missing_path_raises(tmp_path: Path) -> None:
    conf = cfg.FplanConfig(data_dir=tmp_path / "nope", binary=None, source=None)
    with pytest.raises(cfg.ConfigError, match="does not exist"):
        cfg.require_data_dir(conf)


def test_require_data_dir_ok(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    conf = cfg.FplanConfig(data_dir=tmp_path / "data", binary=None, source=None)
    assert cfg.require_data_dir(conf) == tmp_path / "data"


def test_require_binary_unset_raises(tmp_path: Path) -> None:
    conf = cfg.FplanConfig(data_dir=None, binary=None, source=tmp_path / "c")
    with pytest.raises(cfg.ConfigError, match="no Factorio executable"):
        cfg.require_binary(conf)


def test_require_binary_missing_path_raises(tmp_path: Path) -> None:
    conf = cfg.FplanConfig(data_dir=None, binary=tmp_path / "nope", source=None)
    with pytest.raises(cfg.ConfigError, match="does not exist"):
        cfg.require_binary(conf)


def test_require_binary_directory_raises(tmp_path: Path) -> None:
    # A config pointing the binary at a directory gets a clear ConfigError
    # rather than a downstream Popen OSError/traceback.
    conf = cfg.FplanConfig(data_dir=None, binary=tmp_path, source=None)
    with pytest.raises(cfg.ConfigError, match="is not a file"):
        cfg.require_binary(conf)


def test_require_binary_ok_resolves(tmp_path: Path) -> None:
    binary = tmp_path / "factorio"
    binary.touch()
    conf = cfg.FplanConfig(data_dir=None, binary=binary, source=None)
    assert cfg.require_binary(conf) == binary.resolve()


def test_render_config_round_trips(tmp_path: Path) -> None:
    p = _write(tmp_path / "c.yaml", cfg.render_config("/a/b", "/c/d"))
    conf = cfg.load_config(p)
    assert conf.data_dir == Path("/a/b") and conf.binary == Path("/c/d")


# --------------------------------------------------------------------------- #
# factorio.py
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sysplat, expected",
    [
        ("darwin", "darwin"),
        ("win32", "win32"),
        ("linux", "linux"),
        ("linux2", "linux"),
        ("freebsd13", None),
    ],
)
def test_current_platform(monkeypatch, sysplat: str, expected: str | None) -> None:
    monkeypatch.setattr(factorio.sys, "platform", sysplat)
    assert factorio.current_platform() == expected


def test_platform_label() -> None:
    assert factorio.platform_label("darwin") == "macOS"
    assert factorio.platform_label("mystery") == "mystery"


def test_is_untested() -> None:
    assert not factorio.is_untested("darwin")
    assert factorio.is_untested("linux")
    assert factorio.is_untested("win32")


def test_default_root() -> None:
    assert "Factorio" in factorio.default_root("darwin")
    assert factorio.default_root("mystery") == ""


def test_derive_from_root_picks_existing_layout(tmp_path: Path) -> None:
    # Lay out a macOS-style bundle so the first layout matches.
    data = tmp_path / "factorio.app" / "Contents" / "data"
    data.mkdir(parents=True)
    install = factorio.derive_from_root(tmp_path, "darwin")
    assert install.data_dir == data


def test_derive_from_root_falls_back_to_primary(tmp_path: Path) -> None:
    install = factorio.derive_from_root(tmp_path, "darwin")
    assert install.data_dir == tmp_path / "factorio.app" / "Contents" / "data"


def test_derive_from_root_unknown_platform_uses_fallback(tmp_path: Path) -> None:
    install = factorio.derive_from_root(tmp_path, "mystery")
    assert install.data_dir == tmp_path / "data"
    assert install.binary == tmp_path / "bin" / "x64" / "factorio"


def test_detect_finds_an_install(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "Factorio"
    (root / "factorio.app" / "Contents" / "data").mkdir(parents=True)
    (root / "factorio.app" / "Contents" / "MacOS").mkdir(parents=True)
    (root / "factorio.app" / "Contents" / "MacOS" / "factorio").touch()
    monkeypatch.setitem(factorio._KNOWN_ROOTS, "darwin", [str(root)])
    install = factorio.detect("darwin")
    assert install is not None and install.data_dir.exists()


def test_detect_returns_none_when_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(factorio._KNOWN_ROOTS, "darwin", [str(tmp_path / "absent")])
    assert factorio.detect("darwin") is None


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_script_output_dir_known_platforms(platform: str) -> None:
    out = factorio.script_output_dir(platform)
    assert out is not None and out.name == "script-output"


def test_script_output_dir_unknown_platform() -> None:
    assert factorio.script_output_dir("mystery") is None
