"""Load raw Factorio prototype data by running the game's Lua prototype files
in a stubbed environment via ``lupa``.

This is the *raw* layer: it produces a `GameData` of nested dicts straight from
the game's own data, plus parsed `Technology` records. The cleaning into a
typed, usable model lives in `fplan.model.game`.

Only the vanilla `base` mod is loaded; this is enough for the rocket-silo
victory tech tree. The data directory is supplied by the caller (from config) —
there is no hardcoded install path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import lupa

# Files we run from base/prototypes/. Order matters only loosely — items
# referenced from recipes/technologies are just strings, so we don't need
# real linking at load time.
PROTOTYPE_FILES = (
    "item.lua",
    "fluid.lua",
    "recipe.lua",
    "technology.lua",
    "entity/entities.lua",  # assemblers, furnaces, boilers, generators, ...
    "entity/mining-drill.lua",  # burner + electric drills, pumpjack (2.0)
    "entity/resources.lua",  # iron-ore, copper-ore, coal, stone, crude-oil, ...
)


# Lua harness:
#   - `data:extend(list)` collects prototypes by type+name into `data.raw`.
#   - `util` returns a function from any field lookup, so calls like
#     `util.technology_icon_constant_speed(...)` just return {}.
#   - `require` returns an empty table; the prototype files only use
#     require results for icons/sounds, none of which affect prerequisites.
#   - `defines` mirrors `util` so any `defines.x.y` lookup is harmless.
LUA_HARNESS = r"""
data = { raw = {} }
function data:extend(list)
  -- Some prototype files pass the result of a function call directly;
  -- if the helper returned nil or a non-table, just no-op rather than
  -- crash. Same for individual entries inside the list.
  if type(list) ~= "table" then return end
  for _, t in ipairs(list) do
    if type(t) == "table" and t.type and t.name then
      self.raw[t.type] = self.raw[t.type] or {}
      self.raw[t.type][t.name] = t
    end
  end
end

-- Treat a stub as the number 1 inside arithmetic, so expressions like
-- `tile_width * 32` (where tile_width came from a graphics helper that
-- returned a stub) evaluate without crashing. The resulting numbers
-- only end up in fields we don't read (sprite sizes, animation frame
-- counts, etc.) — the real fields we care about (crafting_speed,
-- energy_usage, mining_time) are written as plain literals.
local function _num(x) return type(x) == "number" and x or 1 end

local function stub_table()
  local t = {}
  local mt = {
    __index = function(_, _) return stub_table() end,
    __call  = function(_, ...) return stub_table() end,
    __len   = function(_) return 0 end,
    __add   = function(a, b) return _num(a) + _num(b) end,
    __sub   = function(a, b) return _num(a) - _num(b) end,
    __mul   = function(a, b) return _num(a) * _num(b) end,
    __div   = function(a, b) return _num(a) / _num(b) end,
    __mod   = function(a, b) return _num(a) % _num(b) end,
    __unm   = function(_)    return -1 end,
    __concat = function(a, b)
      return (type(a) == "string" and a or "") .. (type(b) == "string" and b or "")
    end,
  }
  setmetatable(t, mt)
  return t
end

util = stub_table()
defines = stub_table()
mods = {}

-- Unit globals defined in core/lualib/util.lua. We hoist them here so
-- expressions like `2 * kg` in item.lua evaluate without running core.
gram, grams = 1, 1
kg = 1000
tons = 1000 * 1000
second = 60
minute = 60 * 60
hour = 60 * 60 * 60
meter = 1
kilometer = 1000

function require(_) return stub_table() end

-- Anything we forgot becomes a callable stub. Reads of undefined globals
-- (volume_multiplier, sounds.foo, ...) return tables that can be indexed
-- or called arbitrarily without erroring.
setmetatable(_G, {
  __index = function(_, _) return stub_table() end
})
"""


@dataclass
class Technology:
    name: str
    prerequisites: list[str] = field(default_factory=list)
    unlocks_recipes: list[str] = field(default_factory=list)
    ingredients: list[tuple[str, int]] = field(default_factory=list)  # science packs
    count: int | None = None
    time: float | None = None
    essential: bool = False
    # Trigger-based techs (Factorio 2.0) use this instead of `unit`.
    # Shape mirrors the Lua: {"type": "craft-item", "item": "iron-plate", "count": 50}
    research_trigger: dict | None = None
    # Sum of this tech's `laboratory-speed` effect modifiers (e.g. the
    # research-speed-N techs: +0.2, +0.3, …). Lab speed = base × (1 + Σ over
    # completed such techs). 0.0 for techs with no lab-speed effect.
    lab_speed_bonus: float = 0.0


def format_research_trigger(rt: dict | None) -> str | None:
    """Render a research_trigger as a short inline phrase, or None."""
    if not rt:
        return None
    kind = rt.get("type")
    if kind == "craft-item":
        n = rt.get("count", 1)
        return f"craft {n} {rt.get('item', '?')}"
    if kind == "craft-fluid":
        n = rt.get("count", 1)
        return f"craft {n} {rt.get('fluid', '?')}"
    if kind == "mine-entity":
        return f"mine {rt.get('entity', '?')}"
    if kind == "build-entity":
        ent = rt.get("entity") or (
            rt.get("entity") if isinstance(rt.get("entity"), str) else "?"
        )
        # `entity` here can be a string or {name=...} dict.
        if isinstance(rt.get("entity"), dict):
            ent = rt["entity"].get("name", "?")
        return f"build {ent}"
    if kind == "capture-spawner":
        return "capture a spawner"
    if kind == "send-item-to-orbit":
        return f"send {rt.get('item', '?')} to orbit"
    if kind == "create-space-platform":
        return "create a space platform"
    # Fallback: show the type plus any extra fields.
    extras = ", ".join(f"{k}={v}" for k, v in rt.items() if k != "type")
    return f"{kind}({extras})" if extras else str(kind)


@dataclass
class GameData:
    technologies: dict[str, Technology]
    items: dict[str, dict]  # raw lua tables as nested dicts
    recipes: dict[str, dict]
    fluids: dict[str, dict]
    # Full data.raw map keyed by prototype type ("assembling-machine",
    # "furnace", "mining-drill", "boiler", "generator", "solar-panel",
    # "accumulator", "reactor", "lab", "resource", ...). The cleaning
    # layer reads producer/resource entities from here.
    raw: dict[str, dict] = field(default_factory=dict)


def _lua_to_py(obj):  # pragma: no cover - only reachable via the Lua load
    """Convert lupa Lua tables to Python dicts/lists recursively."""
    if lupa.lua_type(obj) == "table":
        # Decide list vs dict: lua arrays have sequential integer keys 1..N.
        keys = list(obj.keys())
        if keys and all(isinstance(k, int) for k in keys):
            return [_lua_to_py(obj[k]) for k in sorted(keys)]
        return {k: _lua_to_py(obj[k]) for k in keys}
    return obj


def _parse_tech(raw: dict) -> Technology:
    prereqs = raw.get("prerequisites") or []
    if isinstance(prereqs, dict):
        prereqs = list(prereqs.values())

    effects = raw.get("effects") or []
    if isinstance(effects, dict):
        effects = list(effects.values())
    unlocks = [
        e["recipe"]
        for e in effects
        if isinstance(e, dict) and e.get("type") == "unlock-recipe" and "recipe" in e
    ]
    lab_speed_bonus = sum(
        float(e.get("modifier") or 0.0)
        for e in effects
        if isinstance(e, dict) and e.get("type") == "laboratory-speed"
    )

    unit = raw.get("unit") or {}
    ingredients_raw = unit.get("ingredients") or []
    if isinstance(ingredients_raw, dict):
        ingredients_raw = list(ingredients_raw.values())
    ingredients: list[tuple[str, int]] = []
    for ing in ingredients_raw:
        # Two shapes in Factorio data: {"science-pack-name", count} (array)
        # or {name=..., amount=...} (dict).
        if isinstance(ing, list) and len(ing) >= 2:
            ingredients.append((ing[0], int(ing[1])))
        elif isinstance(ing, dict):
            name = ing.get("name") or ing.get(1)
            amount = ing.get("amount") or ing.get(2) or 1
            if name:
                ingredients.append((name, int(amount)))

    return Technology(
        name=raw["name"],
        prerequisites=list(prereqs),
        unlocks_recipes=unlocks,
        ingredients=ingredients,
        count=unit.get("count"),
        time=unit.get("time"),
        essential=bool(raw.get("essential")),
        research_trigger=raw.get("research_trigger"),
        lab_speed_bonus=lab_speed_bonus,
    )


def build_game_data(raw: dict) -> GameData:
    """Assemble a `GameData` from a raw ``data.raw`` map.

    Split out from :func:`load` (which runs Lua) so it can be exercised against
    a captured raw fixture without a Factorio install.
    """
    techs = {name: _parse_tech(t) for name, t in (raw.get("technology") or {}).items()}
    return GameData(
        technologies=techs,
        items=raw.get("item") or {},
        recipes=raw.get("recipe") or {},
        fluids=raw.get("fluid") or {},
        raw=raw,
    )


def _load_raw(data_dir: Path) -> dict:  # pragma: no cover - runs Lua over game data
    """Run the prototype Lua files under the harness; return the raw ``data.raw``."""
    proto_dir = Path(data_dir) / "base" / "prototypes"
    if not proto_dir.is_dir():
        raise FileNotFoundError(f"Factorio prototypes not found at {proto_dir}")

    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(LUA_HARNESS)

    for fname in PROTOTYPE_FILES:
        path = proto_dir / fname
        if not path.is_file():
            # Not every file exists in every version (e.g., pumpjack lives
            # elsewhere in some releases). Skip silently.
            continue
        try:
            lua.execute(path.read_text())
        except lupa.LuaError:
            # entities.lua occasionally errors on a late graphics-helper
            # call (~line 15k in 1.1). All structurally important entities
            # are defined earlier and have already been registered into
            # data.raw, so we keep going rather than abort the whole load.
            pass

    return _lua_to_py(lua.globals().data.raw)


def load(data_dir: Path) -> GameData:  # pragma: no cover - integration (runs Lua)
    """Load Factorio prototype data from ``data_dir`` (the game's ``data`` dir)."""
    return build_game_data(_load_raw(data_dir))
