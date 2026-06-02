"""The Factorio model layer: raw prototype loading + the cleaned game model.

Two stages:

- `fplan.model.data` — the *raw* layer: runs Factorio's Lua prototype files
  (via ``lupa``) into a `GameData` of nested dicts + parsed technologies.
- `fplan.model.game` — the *clean* layer: turns that into a typed, recipe-centric
  `GameModel` (items, recipes, buildings, technologies) ready for the planning
  stages.

Everything downstream (L1 tech-order, L2 rates, L3 layout, `inspect`) builds on
`GameModel`. Load one with ``load_model(data_dir=...)`` — the data directory
comes from config (`fplan init`), not a hardcoded path.

Run ``python -m fplan.model`` for a one-line summary against the configured
install (a manual smoke check; see the README Testing section).
"""

from __future__ import annotations

from fplan.model.data import (
    GameData,
    Technology,
    build_game_data,
    format_research_trigger,
    load,
)
from fplan.model.game import (
    Building,
    Facility,
    GameModel,
    Item,
    Recipe,
    RecipeRun,
    Stack,
    load_model,
    parse_energy,
)

__all__ = [
    "Building",
    "Facility",
    "GameData",
    "GameModel",
    "Item",
    "Recipe",
    "RecipeRun",
    "Stack",
    "Technology",
    "build_game_data",
    "format_research_trigger",
    "load",
    "load_model",
    "parse_energy",
]
