"""Tests for the `map` domain logic and the `from-save` / `show` commands.

Everything pure (clustering, artifact I/O, the extract helpers) is covered here
against a captured raw-dump fixture. The headless subprocess driver
(`extract._run`) is exercised only by manual integration runs against a real
save, and is excluded from coverage.
"""

from __future__ import annotations

import base64
import copy
import json
import struct
import zlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fplan import factorio
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.map import artifact, cluster, exchange, extract

runner = CliRunner()

FIXTURE = Path(__file__).parent / "fixtures" / "map_extract_raw.json"


@pytest.fixture
def raw_dump() -> dict:
    return json.loads(FIXTURE.read_text())


# --------------------------------------------------------------------------- #
# cluster.py (pure post-processing)
# --------------------------------------------------------------------------- #


def test_postprocess_sorts_and_clusters(raw_dump: dict) -> None:
    data = cluster.postprocess(copy.deepcopy(raw_dump))
    # Patches and water sorted ascending by distance.
    dists = [p["distance"] for p in data["patches"]]
    assert dists == sorted(dists)
    # Oil spots got a cluster id and oil_clusters were derived.
    assert data["oil_clusters"]
    assert all("cluster" in s for s in data["oil_spots"])
    assert {c["id"] for c in data["oil_clusters"]} == set(
        s["cluster"] for s in data["oil_spots"]
    )


def test_postprocess_is_deterministic(raw_dump: dict) -> None:
    a = cluster.postprocess(copy.deepcopy(raw_dump))["oil_clusters"]
    b = cluster.postprocess(copy.deepcopy(raw_dump))["oil_clusters"]
    assert a == b


def test_cluster_oil_empty() -> None:
    assert cluster.cluster_oil([]) == []


def test_cluster_oil_explicit_k() -> None:
    spots = [{"x": float(i), "y": 0.0, "amount": 3000} for i in range(6)]
    clusters = cluster.cluster_oil(spots, k=2)
    assert len(clusters) == 2
    assert clusters[0]["total_yield_pct"] == pytest.approx(
        clusters[0]["total_amount"] / cluster.OIL_YIELD_PER_PCT, rel=1e-6
    )


def test_cluster_oil_two_spots() -> None:
    spots = [{"x": 0.0, "y": 0.0, "amount": 3000}, {"x": 9.0, "y": 0.0, "amount": 6000}]
    clusters = cluster.cluster_oil(spots)  # n <= 2 path
    assert len(clusters) == 2


def test_silhouette_single_cluster_is_worst() -> None:
    import numpy as np

    pts = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    labels = np.zeros(3, dtype=int)  # all one cluster -> undefined, returns -1
    assert cluster._silhouette(pts, labels) == -1.0


def test_postprocess_handles_missing_sections() -> None:
    # A dump with no patches/oil/water must not raise.
    out = cluster.postprocess({"seed": 1})
    assert out == {"seed": 1}


# --------------------------------------------------------------------------- #
# artifact.py (I/O + summary)
# --------------------------------------------------------------------------- #


def test_write_then_load_round_trips(tmp_path: Path, raw_dump: dict) -> None:
    data = cluster.postprocess(copy.deepcopy(raw_dump))
    out = tmp_path / "sub" / "map.yaml"
    artifact.write_yaml(data, out)  # creates parent dirs
    loaded = artifact.load_artifact(out)
    assert loaded["seed"] == data["seed"]
    assert len(loaded["oil_clusters"]) == len(data["oil_clusters"])


def test_load_artifact_unparsable(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("just: [unbalanced\n")
    with pytest.raises(artifact.ArtifactError, match="could not read"):
        artifact.load_artifact(p)


def test_load_artifact_not_a_mapping(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(artifact.ArtifactError, match="not a map artifact"):
        artifact.load_artifact(p)


def test_summarize_breaks_down_by_resource(raw_dump: dict) -> None:
    data = cluster.postprocess(copy.deepcopy(raw_dump))
    text = artifact.summarize(data)
    assert f"seed={data['seed']}" in text
    # Per-type breakdown: each resource gets a line with count + total tiles +
    # nearest-patch distance and size.
    for resource in {p["resource"] for p in data["patches"]}:
        assert f"{resource}:" in text
    assert "tiles total" in text
    assert "tiles away" in text  # distances are labelled in tiles
    # Oil: nearest field + field count + average yield.
    assert "fields;" in text and "nearest field" in text and "%/spot" in text
    # Water: body count + nearest distance.
    assert "bodies; nearest" in text and "trees:" in text


def test_summarize_resource_line_is_accurate(raw_dump: dict) -> None:
    data = cluster.postprocess(copy.deepcopy(raw_dump))
    # Pick a resource and verify the numbers in its line.
    iron = [p for p in data["patches"] if p["resource"] == "iron-ore"]
    nearest = min(iron, key=lambda p: p["distance"])
    total = sum(p["tile_count"] for p in iron)
    text = artifact.summarize(data)
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("iron-ore:"))
    assert f"{len(iron)} patch" in line
    assert f"{total} tiles total" in line
    assert f"{nearest['distance']:.1f} tiles away" in line
    assert f"({nearest['tile_count']} tiles)" in line


def test_summarize_singular_plural() -> None:
    data = {
        "seed": 1,
        "radius": 10,
        "patches": [{"resource": "coal", "tile_count": 5, "distance": 3.0}],
        "oil_spots": [{"x": 0.0, "y": 0.0, "amount": 3000}],
        "oil_clusters": [{"distance": 1.0, "spot_count": 1, "total_yield_pct": 100.0}],
        "water_patches": [{"distance": 2.0}],
        "water_min_distance": 2.0,
        "tree_count": 1,
    }
    text = artifact.summarize(data)
    assert "1 patch," in text  # singular, not "1 patchs"
    assert "1 spot in 1 field" in text
    assert "1 body;" in text


def test_summarize_oil_without_clusters() -> None:
    # A dump with oil spots but no oil_clusters (unclustered) still summarizes.
    text = artifact.summarize(
        {"seed": 1, "radius": 10, "oil_spots": [{"x": 0.0, "y": 0.0, "amount": 3000}]}
    )
    assert "oil: 1 spot" in text
    assert "field" not in text  # no field/yield line without clusters


def test_summarize_handles_missing_water() -> None:
    text = artifact.summarize({"seed": 1, "radius": 10})
    assert "nearest n/a" in text


# --------------------------------------------------------------------------- #
# extract.py (pure helpers; the subprocess driver is integration-only)
# --------------------------------------------------------------------------- #


def test_materialize_mod_writes_enabled_mod(tmp_path: Path) -> None:
    mods = tmp_path / "mods"
    mods.mkdir()
    extract._materialize_mod(mods)
    assert (mods / "l3-map-extract_0.1.0" / "control.lua").exists()
    assert (mods / "l3-map-extract_0.1.0" / "info.json").exists()
    mod_list = json.loads((mods / "mod-list.json").read_text())
    names = {m["name"]: m["enabled"] for m in mod_list["mods"]}
    assert names == {"base": True, "l3-map-extract": True}


def test_clear_previous_output(tmp_path: Path) -> None:
    for name in (extract.DONE_NAME, extract.OUTPUT_NAME, extract.ERROR_NAME):
        (tmp_path / name).write_text("stale")
    extract._clear_previous_output(tmp_path)
    assert not any(
        (tmp_path / n).exists()
        for n in (extract.DONE_NAME, extract.OUTPUT_NAME, extract.ERROR_NAME)
    )


def test_read_result_success(tmp_path: Path) -> None:
    (tmp_path / extract.OUTPUT_NAME).write_text(json.dumps({"seed": 7}))
    assert extract._read_result(tmp_path) == {"seed": 7}


def test_read_result_mod_error(tmp_path: Path) -> None:
    (tmp_path / extract.ERROR_NAME).write_text("boom")
    with pytest.raises(extract.ExtractError, match="extract mod errored: boom"):
        extract._read_result(tmp_path)


def test_read_result_missing_output(tmp_path: Path) -> None:
    with pytest.raises(extract.ExtractError, match="no JSON output"):
        extract._read_result(tmp_path)


def test_read_result_malformed_json(tmp_path: Path) -> None:
    # A truncated/garbled dump must surface as ExtractError, not a raw traceback.
    (tmp_path / extract.OUTPUT_NAME).write_text('{"seed": 1, "patches": [')
    with pytest.raises(extract.ExtractError, match="unreadable"):
        extract._read_result(tmp_path)


def test_resolve_script_output_macos(monkeypatch) -> None:
    monkeypatch.setattr(factorio, "current_platform", lambda: "darwin")
    out = extract._resolve_script_output()
    assert out.name == "script-output"


def test_resolve_script_output_unknown_platform(monkeypatch) -> None:
    monkeypatch.setattr(factorio, "current_platform", lambda: None)
    with pytest.raises(extract.ExtractError, match="unsupported here"):
        extract._resolve_script_output()


def test_extract_missing_save_raises(tmp_path: Path) -> None:
    with pytest.raises(extract.ExtractError, match="save file not found"):
        extract.extract(save=tmp_path / "nope.zip", binary=tmp_path / "bin")


def test_extract_orchestration(tmp_path: Path, monkeypatch, raw_dump: dict) -> None:
    # Cover the orchestration glue without launching Factorio: stub the
    # subprocess driver to drop the fixture where _read_result will find it.
    save = tmp_path / "save.zip"
    save.write_text("x")
    script_out = tmp_path / "script-output"
    script_out.mkdir()
    monkeypatch.setattr(extract, "_resolve_script_output", lambda: script_out)

    def fake_run(*, save, binary, script_output, timeout_s):
        (script_output / extract.OUTPUT_NAME).write_text(json.dumps(raw_dump))

    monkeypatch.setattr(extract, "_run", fake_run)
    data = extract.extract(save=save, binary=tmp_path / "factorio")
    assert data["seed"] == raw_dump["seed"]


def test_extract_from_string_orchestration(
    tmp_path: Path, monkeypatch, raw_dump: dict
) -> None:
    # Same glue check for the from-string path: stub the subprocess driver.
    script_out = tmp_path / "script-output"
    script_out.mkdir()
    monkeypatch.setattr(extract, "_resolve_script_output", lambda: script_out)

    def fake_run(*, exchange_string, binary, script_output, timeout_s):
        (script_output / extract.OUTPUT_NAME).write_text(json.dumps(raw_dump))

    monkeypatch.setattr(extract, "_run_from_string", fake_run)
    data = extract.extract_from_string(
        exchange_string=">>>ABC<<<", binary=tmp_path / "factorio"
    )
    assert data["seed"] == raw_dump["seed"]


# --------------------------------------------------------------------------- #
# CLI: from-save / show
# --------------------------------------------------------------------------- #


def test_from_save_requires_out(tmp_path: Path, monkeypatch) -> None:
    # --out is mandatory; omitting it is a usage error (exit 2), not a run.
    monkeypatch.chdir(tmp_path)
    save = tmp_path / "save.zip"
    save.write_text("x")
    result = runner.invoke(app, ["map", "from-save", str(save)])
    assert result.exit_code == 2


def test_from_save_missing_save_is_fatal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["map", "from-save", str(tmp_path / "missing.zip"), "--out", "o.yaml"]
    )
    assert result.exit_code == 1


def test_from_save_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save = tmp_path / "save.zip"
    save.write_text("x")
    result = runner.invoke(
        app, ["map", "from-save", str(save), "--out", "out.yaml", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "Would extract" in result.stdout
    assert "out.yaml" in result.stdout
    assert not (tmp_path / "out.yaml").exists()


def test_from_save_dry_run_reports_overwrite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    save = tmp_path / "save.zip"
    save.write_text("x")
    (tmp_path / "out.yaml").write_text("old")
    result = runner.invoke(
        app, ["map", "from-save", str(save), "--out", "out.yaml", "--dry-run"]
    )
    assert "overwriting" in result.stdout
    assert (tmp_path / "out.yaml").read_text() == "old"  # dry run touches nothing


def test_from_save_warns_on_untested_platform(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(factorio, "current_platform", lambda: "linux")
    save = tmp_path / "save.zip"
    save.write_text("x")
    result = runner.invoke(
        app, ["map", "from-save", str(save), "--out", "o.yaml", "--dry-run"]
    )
    assert "untested on Linux" in result.stdout


def test_from_save_no_binary_is_fatal(tmp_path: Path, monkeypatch) -> None:
    # No config in cwd -> no binary configured -> fatal before running Factorio.
    monkeypatch.chdir(tmp_path)
    save = tmp_path / "save.zip"
    save.write_text("x")
    result = runner.invoke(app, ["map", "from-save", str(save), "--out", "o.yaml"])
    assert result.exit_code == 1


def _config_with_binary(tmp_path: Path) -> Path:
    from fplan import config as cfg

    binary = tmp_path / "factorio"
    binary.touch()
    conf = tmp_path / "c.yaml"
    conf.write_text(cfg.render_config(None, str(binary)))
    return conf


def test_from_save_real_run_writes_artifact(
    tmp_path: Path, monkeypatch, raw_dump: dict
) -> None:
    # Cover the run glue (binary resolve -> extract -> postprocess -> write ->
    # summarize) without launching Factorio.
    monkeypatch.chdir(tmp_path)
    conf = _config_with_binary(tmp_path)
    save = tmp_path / "MySave.zip"
    save.write_text("x")
    monkeypatch.setattr(extract, "extract", lambda **kw: copy.deepcopy(raw_dump))
    result = runner.invoke(
        app,
        ["--config-file", str(conf), "map", "from-save", str(save), "--out", "w.yaml"],
    )
    assert result.exit_code == 0
    assert (tmp_path / "w.yaml").exists()
    assert f"seed={raw_dump['seed']}" in result.stdout


def test_from_save_extract_error_is_fatal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conf = _config_with_binary(tmp_path)
    save = tmp_path / "s.zip"
    save.write_text("x")

    def boom(**kw):
        raise extract.ExtractError("no dump within 180s")

    monkeypatch.setattr(extract, "extract", boom)
    result = runner.invoke(
        app,
        ["--config-file", str(conf), "map", "from-save", str(save), "--out", "o.yaml"],
    )
    assert result.exit_code == 1


def test_from_save_refuses_overwrite_noninteractive(
    tmp_path: Path, monkeypatch
) -> None:
    # Existing --out + non-interactive (CliRunner) -> fatal, file untouched,
    # before Factorio is ever consulted.
    monkeypatch.chdir(tmp_path)
    save = tmp_path / "save.zip"
    save.write_text("x")
    out = tmp_path / "out.yaml"
    out.write_text("old")
    result = runner.invoke(app, ["map", "from-save", str(save), "--out", str(out)])
    assert result.exit_code == 1
    assert out.read_text() == "old"


def test_from_save_overwrite_declined(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    save = tmp_path / "save.zip"
    save.write_text("x")
    out = tmp_path / "out.yaml"
    out.write_text("old")
    result = runner.invoke(
        app, ["map", "from-save", str(save), "--out", str(out)], input="n\n"
    )
    assert result.exit_code == 0
    assert "Aborted" in result.stdout
    assert out.read_text() == "old"


def test_from_save_overwrite_confirmed(
    tmp_path: Path, monkeypatch, raw_dump: dict
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    conf = _config_with_binary(tmp_path)
    save = tmp_path / "save.zip"
    save.write_text("x")
    out = tmp_path / "out.yaml"
    out.write_text("old")
    monkeypatch.setattr(extract, "extract", lambda **kw: copy.deepcopy(raw_dump))
    result = runner.invoke(
        app,
        ["--config-file", str(conf), "map", "from-save", str(save), "--out", str(out)],
        input="y\n",
    )
    assert result.exit_code == 0
    assert artifact.load_artifact(out)["seed"] == raw_dump["seed"]


def test_show_summarizes_artifact(tmp_path: Path, raw_dump: dict) -> None:
    data = cluster.postprocess(copy.deepcopy(raw_dump))
    out = tmp_path / "m.yaml"
    artifact.write_yaml(data, out)
    result = runner.invoke(app, ["map", "show", str(out)])
    assert result.exit_code == 0
    assert f"seed={data['seed']}" in result.stdout


def test_show_bad_artifact_is_fatal(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- not\n- a mapping\n")
    result = runner.invoke(app, ["map", "show", str(p)])
    assert result.exit_code == 1


# --------------------------------------------------------------------------- #
# exchange.py (pure ingestion + validation)
# --------------------------------------------------------------------------- #


def _make_exchange_string(
    version: tuple[int, int, int, int] = (1, 1, 92, 0), payload: bytes = b"\x00" * 16
) -> str:
    """Build a minimal-but-valid map-exchange string for tests."""
    blob = struct.pack("<4H", *version) + payload
    body = base64.b64encode(zlib.compress(blob)).decode()
    return f">>>{body}<<<"


def test_parse_exchange_string_round_trips() -> None:
    parsed = exchange.parse_exchange_string(_make_exchange_string())
    assert parsed.version == (1, 1, 92, 0)
    assert parsed.version_label == "1.1.92"
    assert parsed.raw.startswith(">>>") and parsed.raw.endswith("<<<")


def test_parse_exchange_string_tolerates_whitespace_and_wrapping() -> None:
    s = _make_exchange_string()
    body = s[3:-3]
    wrapped = ">>>\n" + body[:10] + "\n" + body[10:] + "\n<<<"
    parsed = exchange.parse_exchange_string("  \n" + wrapped + "  ")
    assert parsed.version_label == "1.1.92"
    # The normalized body has no interior whitespace.
    assert " " not in parsed.raw and "\n" not in parsed.raw


@pytest.mark.parametrize(
    "text, needle",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("no markers here", "markers"),
        (">>><<<", "markers"),
        (">>>    <<<", "between the markers"),
        (">>>!!!not base64!!!<<<", "base64"),
        (">>>" + base64.b64encode(b"not zlib data").decode() + "<<<", "decompress"),
        (
            ">>>" + base64.b64encode(zlib.compress(b"\x01\x02")).decode() + "<<<",
            "version header",
        ),
    ],
)
def test_parse_exchange_string_errors(text: str, needle: str) -> None:
    with pytest.raises(exchange.ExchangeError, match=needle):
        exchange.parse_exchange_string(text)


def test_resolve_source_stdin() -> None:
    text, source = exchange.resolve_source(
        Path("-"), is_interactive=lambda: False, read_stdin=lambda: "PASTED"
    )
    assert (text, source) == ("PASTED", "stdin")


def test_resolve_source_file(tmp_path: Path) -> None:
    f = tmp_path / "exch.txt"
    f.write_text("FROM-FILE")
    text, source = exchange.resolve_source(f, is_interactive=lambda: False)
    assert (text, source) == ("FROM-FILE", "file")


def test_resolve_source_missing_file(tmp_path: Path) -> None:
    with pytest.raises(exchange.ExchangeError, match="file not found"):
        exchange.resolve_source(tmp_path / "nope.txt", is_interactive=lambda: False)


def test_resolve_source_unreadable_file(tmp_path: Path) -> None:
    # A directory exists() but read_text() raises OSError -> clean ExchangeError.
    with pytest.raises(exchange.ExchangeError, match="could not read"):
        exchange.resolve_source(tmp_path, is_interactive=lambda: False)


def test_resolve_source_interactive_prompts() -> None:
    seen = []

    def prompt(msg: str) -> str:
        seen.append(msg)
        return "TYPED"

    text, source = exchange.resolve_source(
        None, is_interactive=lambda: True, prompt=prompt
    )
    assert (text, source) == ("TYPED", "interactive")
    assert seen == ["Paste the map-exchange string: "]


def test_resolve_source_non_tty_without_from_is_error() -> None:
    with pytest.raises(exchange.ExchangeError, match="not a TTY"):
        exchange.resolve_source(None, is_interactive=lambda: False)


def test_materialize_mod_embeds_exchange_string(tmp_path: Path) -> None:
    mods = tmp_path / "mods"
    mods.mkdir()
    extract._materialize_mod(mods, exchange_string=">>>ABC<<<")
    embedded = mods / "l3-map-extract_0.1.0" / "map_exchange_string.lua"
    assert embedded.exists()
    assert embedded.read_text() == "return [==[\n>>>ABC<<<\n]==]\n"


def test_materialize_mod_omits_exchange_string_by_default(tmp_path: Path) -> None:
    # from-save path: the embedded data file must be absent (behavior unchanged).
    mods = tmp_path / "mods"
    mods.mkdir()
    extract._materialize_mod(mods)
    assert not (mods / "l3-map-extract_0.1.0" / "map_exchange_string.lua").exists()


# --------------------------------------------------------------------------- #
# CLI: from-string
# --------------------------------------------------------------------------- #

_VALID_STRING = _make_exchange_string()


def _string_file(tmp_path: Path) -> Path:
    f = tmp_path / "exch.txt"
    f.write_text(_VALID_STRING)
    return f


def test_from_string_requires_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["map", "from-string", "--from", str(_string_file(tmp_path))]
    )
    assert result.exit_code == 2  # --out is mandatory


def test_from_string_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "map",
            "from-string",
            "--from",
            str(_string_file(tmp_path)),
            "--out",
            "out.yaml",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "Would generate" in result.stdout
    assert "source=file" in result.stdout
    assert "1.1.92" in result.stdout
    assert not (tmp_path / "out.yaml").exists()


def test_from_string_stdin_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["map", "from-string", "--from", "-", "--out", "o.yaml", "--dry-run"],
        input=_VALID_STRING,
    )
    assert result.exit_code == 0
    assert "source=stdin" in result.stdout


def test_from_string_interactive_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    result = runner.invoke(
        app,
        ["map", "from-string", "--out", "o.yaml", "--dry-run"],
        input=_VALID_STRING + "\n",
    )
    assert result.exit_code == 0
    assert "source=interactive" in result.stdout


def test_from_string_malformed_is_fatal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    def boom(**kw):
        raise AssertionError("extract must not run on a malformed string")

    monkeypatch.setattr(extract, "extract_from_string", boom)
    result = runner.invoke(
        app,
        ["map", "from-string", "--from", "-", "--out", "o.yaml"],
        input="not a map-exchange string",
    )
    assert result.exit_code == 1
    assert not (tmp_path / "o.yaml").exists()


def test_from_string_non_tty_without_from_is_fatal(tmp_path: Path, monkeypatch) -> None:
    # CliRunner's stdin is non-interactive; a bare invocation can't prompt.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["map", "from-string", "--out", "o.yaml"])
    assert result.exit_code == 1


def test_from_string_real_run_writes_artifact(
    tmp_path: Path, monkeypatch, raw_dump: dict
) -> None:
    monkeypatch.chdir(tmp_path)
    conf = _config_with_binary(tmp_path)
    monkeypatch.setattr(
        extract, "extract_from_string", lambda **kw: copy.deepcopy(raw_dump)
    )
    result = runner.invoke(
        app,
        [
            "--config-file",
            str(conf),
            "map",
            "from-string",
            "--from",
            str(_string_file(tmp_path)),
            "--out",
            "w.yaml",
        ],
    )
    assert result.exit_code == 0
    assert (tmp_path / "w.yaml").exists()
    assert f"seed={raw_dump['seed']}" in result.stdout


def test_from_string_extract_error_is_fatal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    conf = _config_with_binary(tmp_path)

    def boom(**kw):
        raise extract.ExtractError("no dump within 180s")

    monkeypatch.setattr(extract, "extract_from_string", boom)
    result = runner.invoke(
        app,
        [
            "--config-file",
            str(conf),
            "map",
            "from-string",
            "--from",
            str(_string_file(tmp_path)),
            "--out",
            "o.yaml",
        ],
    )
    assert result.exit_code == 1


def test_from_string_refuses_overwrite_noninteractive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def boom(**kw):
        raise AssertionError("must not run Factorio when refusing to overwrite")

    monkeypatch.setattr(extract, "extract_from_string", boom)
    out = tmp_path / "out.yaml"
    out.write_text("old")
    result = runner.invoke(
        app,
        [
            "map",
            "from-string",
            "--from",
            str(_string_file(tmp_path)),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 1
    assert out.read_text() == "old"


def test_from_string_overwrite_confirmed(
    tmp_path: Path, monkeypatch, raw_dump: dict
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "_stdin_is_interactive", lambda: True)
    conf = _config_with_binary(tmp_path)
    out = tmp_path / "out.yaml"
    out.write_text("old")
    monkeypatch.setattr(
        extract, "extract_from_string", lambda **kw: copy.deepcopy(raw_dump)
    )
    result = runner.invoke(
        app,
        [
            "--config-file",
            str(conf),
            "map",
            "from-string",
            "--from",
            str(_string_file(tmp_path)),
            "--out",
            str(out),
        ],
        input="y\n",
    )
    assert result.exit_code == 0
    assert artifact.load_artifact(out)["seed"] == raw_dump["seed"]
