"""``python -m fplan.model`` — load the configured Factorio data and summarize it.

A manual smoke / integration check: it needs a real Factorio data directory
(config ``data_dir`` / ``fplan init``), which CI does not have, so this entry
point is excluded from the coverage gate. See the README Testing section.
"""

from __future__ import annotations

import sys

from fplan import config as cfg
from fplan.model import load_model


def main() -> int:
    try:
        data_dir = cfg.require_data_dir(cfg.load_config())
        model = load_model(data_dir=data_dir)
    except (cfg.ConfigError, OSError, UnicodeDecodeError) as exc:
        # Never leak a traceback. require_data_dir only checks the dir exists, so
        # a config pointed at a non-Factorio directory (e.g. the install root
        # instead of its `data` dir) reaches the loader and raises
        # FileNotFoundError — surface it cleanly per the stream convention.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    crafting = sum(1 for r in model.recipes.values() if r.kind == "crafting")
    mining = sum(1 for r in model.recipes.values() if r.kind == "mining")
    pumping = sum(1 for r in model.recipes.values() if r.kind == "pumping")
    print(f"data_dir: {data_dir}")
    print(
        f"items={len(model.items)}  "
        f"recipes={len(model.recipes)} "
        f"({crafting} crafting, {mining} mining, {pumping} pumping)  "
        f"buildings={len(model.buildings)}  "
        f"technologies={len(model.technologies)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
