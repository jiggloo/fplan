"""Tests for the `map` domain logic and the `from-save` / `show` commands.

Everything pure (clustering, artifact I/O, the extract helpers) is covered here
against a captured raw-dump fixture. The headless subprocess driver
(`extract._run`) is exercised only by manual integration runs against a real
save, and is excluded from coverage.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fplan import factorio
from fplan.cli import app
from fplan.cli import main as cli_main
from fplan.map import artifact, cluster, extract

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


def test_summarize_mentions_counts(raw_dump: dict) -> None:
    data = cluster.postprocess(copy.deepcopy(raw_dump))
    text = artifact.summarize(data)
    assert f"seed={data['seed']}" in text
    assert "oil spots" in text and "water bodies" in text and "trees" in text


def test_summarize_handles_missing_water() -> None:
    text = artifact.summarize({"seed": 1, "radius": 10})
    assert "nearest at n/a" in text


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
