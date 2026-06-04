"""Headless-Factorio orchestration for ``map from-save``.

Runs Factorio with the bundled ``l3-map-extract`` mod against a *copy* of a
save, waits for the mod's sentinel file, then SIGTERMs Factorio (its Lua API
has no exit()) and reads back the JSON dump. Carries over the hard-won
safeguards from the upstream ``l3_map.py`` — each of these will bite anyone who
skips it:

  1. The source save is *always* copied to a tempdir first. ``--start-server``
     autosaves on SIGTERM, which would otherwise overwrite the user's file.
     (This is not hypothetical: it has clobbered a save before.)
  2. ``auto_pause: false`` — headless Factorio pauses with no players, so the
     mod's ``on_tick`` handler would never fire.
  3. The mod's dump is written to Factorio's per-OS ``script-output`` directory,
     not next to the save, so results are read from there.

The pure helpers below (mod materialization, output clearing, result reading)
are unit-tested; :func:`extract` itself drives a real subprocess and is covered
only by manual integration runs.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import signal
import subprocess
import time
from pathlib import Path

from fplan import factorio

MOD_RESOURCE = "l3-map-extract"
OUTPUT_NAME = "l3_map.json"
DONE_NAME = "l3_map.done"
ERROR_NAME = "l3_map.error"

# Minimal headless-server config. The only field we truly need is auto_pause;
# the rest are required-or-defaulted by Factorio 1.1's parser. Carried over
# verbatim from the verified l3_map.py.
SERVER_SETTINGS = {
    "name": "fplan-map-probe",
    "description": "",
    "tags": [],
    "max_players": 0,
    "visibility": {"public": False, "lan": False},
    "username": "",
    "password": "",
    "token": "",
    "game_password": "",
    "require_user_verification": False,
    "max_upload_in_kilobytes_per_second": 0,
    "max_upload_slots": 5,
    "minimum_latency_in_ticks": 0,
    "ignore_player_limit_for_returning_players": False,
    "allow_commands": "true",
    "autosave_interval": 999999,
    "autosave_slots": 0,
    "afk_autokick_interval": 0,
    "auto_pause": False,
    "only_admins_can_pause_the_game": True,
    "autosave_only_on_server": True,
    "non_blocking_saving": False,
}


class ExtractError(Exception):
    """The headless extraction failed (bad input, timeout, mod or Factorio error)."""


def _resolve_script_output() -> Path:
    """Factorio's ``script-output`` dir for this platform, or raise."""
    platform = factorio.current_platform()
    script_output = (
        factorio.script_output_dir(platform) if platform is not None else None
    )
    if script_output is None:
        raise ExtractError(
            "cannot locate Factorio's script-output directory on this platform; "
            "map extraction is unsupported here"
        )
    return script_output


def _materialize_mod(mods_dir: Path, *, exchange_string: str | None = None) -> None:
    """Write the bundled mod into ``mods_dir`` with an enabling mod-list.json.

    Copied out of the package (rather than pointed at in place) so it works from
    an installed wheel and so Factorio's writes never touch read-only package
    files.

    When ``exchange_string`` is given (the ``from-string`` path), it is also
    written as a ``map_exchange_string.lua`` data file the mod ``require``s and
    parses to generate the probed surface. It is embedded as a long-bracket Lua
    *string literal* — never executed as code, and base64 + the ``>>>``/``<<<``
    markers can't contain the ``]==]`` close sequence. When ``None`` (the
    ``from-save`` path) the file is absent and behavior is unchanged.
    """
    src = importlib.resources.files("fplan") / "resources" / MOD_RESOURCE
    info = json.loads((src / "info.json").read_text())
    dest = mods_dir / f"{info['name']}_{info['version']}"
    dest.mkdir(parents=True)
    for fname in ("control.lua", "info.json"):
        (dest / fname).write_text((src / fname).read_text())
    if exchange_string is not None:
        (dest / "map_exchange_string.lua").write_text(
            f"return [==[\n{exchange_string}\n]==]\n"
        )
    (mods_dir / "mod-list.json").write_text(
        json.dumps(
            {
                "mods": [
                    {"name": "base", "enabled": True},
                    {"name": info["name"], "enabled": True},
                ]
            }
        )
    )


def _clear_previous_output(script_output: Path) -> None:
    for name in (DONE_NAME, OUTPUT_NAME, ERROR_NAME):
        f = script_output / name
        if f.exists():
            f.unlink()


def _read_result(script_output: Path) -> dict:
    """Read the mod's dump after the sentinel fired; raise on a mod/IO error."""
    error_file = script_output / ERROR_NAME
    output_file = script_output / OUTPUT_NAME
    if error_file.exists():
        raise ExtractError(f"extract mod errored: {error_file.read_text().strip()}")
    if not output_file.exists():
        raise ExtractError("sentinel appeared but no JSON output was found")
    try:
        return json.loads(output_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        # A truncated/garbled dump (e.g. disk full mid-write) must surface as a
        # clean `error:` line, not a traceback escaping past from_save's handler.
        raise ExtractError(f"extract output was unreadable: {exc}") from exc


def extract(*, save: Path, binary: Path, timeout_s: float = 180.0) -> dict:
    """Run Factorio headless against a copy of ``save`` and return the raw dump.

    ``binary`` is the (already validated) Factorio executable. Raises
    :class:`ExtractError` on any failure. Never modifies ``save``.
    """
    if not save.exists():
        raise ExtractError(f"save file not found: {save}")
    script_output = _resolve_script_output()
    _run(save=save, binary=binary, script_output=script_output, timeout_s=timeout_s)
    return _read_result(script_output)


def extract_from_string(
    *, exchange_string: str, binary: Path, timeout_s: float = 180.0
) -> dict:
    """Generate a world from a map-exchange string and return the raw probe dump.

    A map-exchange string carries only map-gen settings, so there is no save to
    probe: Factorio first creates a throwaway default map, then the probe mod
    parses the embedded string, generates a surface from its settings, and dumps
    that. ``exchange_string`` is the validated, normalized string (see
    :func:`fplan.map.exchange.parse_exchange_string`); ``binary`` is the
    already-validated Factorio executable. Raises :class:`ExtractError` on any
    failure. The user's files are never touched (the probe save is a temp file).
    """
    script_output = _resolve_script_output()
    _run_from_string(
        exchange_string=exchange_string,
        binary=binary,
        script_output=script_output,
        timeout_s=timeout_s,
    )
    return _read_result(script_output)


def _run(  # pragma: no cover - drives a real Factorio subprocess
    *, save: Path, binary: Path, script_output: Path, timeout_s: float
) -> None:
    import tempfile

    _clear_previous_output(script_output)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        probe = tmp / "probe.zip"
        shutil.copy2(save, probe)  # safeguard #1: never touch the original

        mods_dir = tmp / "mods"
        mods_dir.mkdir()
        _materialize_mod(mods_dir)

        settings_path = tmp / "server-settings.json"
        settings_path.write_text(json.dumps(SERVER_SETTINGS))

        proc = subprocess.Popen(
            [
                str(binary),
                "--start-server",
                str(probe),
                "--server-settings",
                str(settings_path),
                "--mod-directory",
                str(mods_dir),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        done = script_output / DONE_NAME
        try:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if done.exists():
                    break
                if proc.poll() is not None:
                    raise ExtractError(
                        f"Factorio exited (code {proc.returncode}) before producing "
                        "output"
                    )
                time.sleep(0.5)
            else:
                raise ExtractError(f"no dump within {timeout_s:.0f}s")
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def _run_from_string(  # pragma: no cover - drives a real Factorio subprocess
    *, exchange_string: str, binary: Path, script_output: Path, timeout_s: float
) -> None:
    import tempfile

    _clear_previous_output(script_output)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        probe = tmp / "probe.zip"

        # No save exists for a string, so create a throwaway default map first
        # (a plain `--create`, no mod — keep generation clean). The probe mod
        # then regenerates the real surface from the parsed exchange string.
        try:
            create = subprocess.run(
                [str(binary), "--create", str(probe)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractError("Factorio timed out creating a base save") from exc
        if create.returncode != 0 or not probe.exists():
            raise ExtractError(
                f"Factorio could not create a base save (code {create.returncode})"
            )

        mods_dir = tmp / "mods"
        mods_dir.mkdir()
        _materialize_mod(mods_dir, exchange_string=exchange_string)

        settings_path = tmp / "server-settings.json"
        settings_path.write_text(json.dumps(SERVER_SETTINGS))

        proc = subprocess.Popen(
            [
                str(binary),
                "--start-server",
                str(probe),
                "--server-settings",
                str(settings_path),
                "--mod-directory",
                str(mods_dir),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        done = script_output / DONE_NAME
        try:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if done.exists():
                    break
                if proc.poll() is not None:
                    raise ExtractError(
                        f"Factorio exited (code {proc.returncode}) before producing "
                        "output"
                    )
                time.sleep(0.5)
            else:
                raise ExtractError(f"no dump within {timeout_s:.0f}s")
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
