-- L3 map-extract mod.
--
-- On tick 1: forces a square of chunks centered on spawn to generate, walks
-- resources and water tiles, dumps a JSON snapshot via game.write_file, then
-- writes a sentinel file. The orchestrator (l3_map.py) polls for the sentinel
-- and SIGTERMs Factorio once it appears (Factorio Lua has no exit()).

local RADIUS = 512  -- tiles from (0,0); change here if a wider sweep is needed.

local function floor_key(p)
  return math.floor(p.x) .. ":" .. math.floor(p.y)
end

-- 8-connected flood-fill grouping over a list of grid-aligned objects (each
-- with a `.position`). Returns a list of member-lists. Done in-memory off a
-- single find_* call so there are no per-tile API hits. Works for both dense
-- resource entities and water tiles since grid keys are floored integers.
local function flood_fill_groups(items)
  local at = {}
  for _, e in ipairs(items) do at[floor_key(e.position)] = e end

  local visited, groups = {}, {}
  for _, seed in ipairs(items) do
    local sk = floor_key(seed.position)
    if not visited[sk] then
      local stack = { {math.floor(seed.position.x), math.floor(seed.position.y)} }
      local members = {}
      while #stack > 0 do
        local p = table.remove(stack)
        local pk = p[1] .. ":" .. p[2]
        if at[pk] and not visited[pk] then
          visited[pk] = true
          members[#members + 1] = at[pk]
          for dx = -1, 1 do
            for dy = -1, 1 do
              if not (dx == 0 and dy == 0) then
                stack[#stack + 1] = { p[1] + dx, p[2] + dy }
              end
            end
          end
        end
      end
      if #members > 0 then groups[#groups + 1] = members end
    end
  end
  return groups
end

-- Resource patches: entity positions are already tile centers (x.5); sum the
-- per-tile `amount` into the patch total.
local function cluster_patches(entities)
  local patches = {}
  for _, members in ipairs(flood_fill_groups(entities)) do
    local sx, sy, total = 0, 0, 0
    local min_x, max_x = math.huge, -math.huge
    local min_y, max_y = math.huge, -math.huge
    for _, m in ipairs(members) do
      sx = sx + m.position.x
      sy = sy + m.position.y
      total = total + m.amount
      if m.position.x < min_x then min_x = m.position.x end
      if m.position.x > max_x then max_x = m.position.x end
      if m.position.y < min_y then min_y = m.position.y end
      if m.position.y > max_y then max_y = m.position.y end
    end
    local n = #members
    local cx, cy = sx / n, sy / n
    patches[#patches + 1] = {
      tile_count = n,
      total_amount = total,
      centroid_x = cx,
      centroid_y = cy,
      distance = math.sqrt(cx * cx + cy * cy),
      min_x = min_x, max_x = max_x,
      min_y = min_y, max_y = max_y,
    }
  end
  return patches
end

-- Water patches: tile.position is the top-left corner, so centers are +0.5.
-- No `amount` on tiles — water bodies are described by tile_count + centroid,
-- mirroring the resource-patch shape for downstream symmetry.
local function cluster_water(tiles)
  local patches = {}
  for _, members in ipairs(flood_fill_groups(tiles)) do
    local sx, sy = 0, 0
    local min_x, max_x = math.huge, -math.huge
    local min_y, max_y = math.huge, -math.huge
    for _, m in ipairs(members) do
      local cx, cy = m.position.x + 0.5, m.position.y + 0.5
      sx = sx + cx
      sy = sy + cy
      if cx < min_x then min_x = cx end
      if cx > max_x then max_x = cx end
      if cy < min_y then min_y = cy end
      if cy > max_y then max_y = cy end
    end
    local n = #members
    local cx, cy = sx / n, sy / n
    patches[#patches + 1] = {
      tile_count = n,
      centroid_x = cx,
      centroid_y = cy,
      distance = math.sqrt(cx * cx + cy * cy),
      min_x = min_x, max_x = max_x,
      min_y = min_y, max_y = max_y,
    }
  end
  return patches
end

local function dump()
  local s = game.surfaces[1]

  -- Force the AOI to fully generate before we read it. Without this,
  -- find_entities_filtered only sees already-charted chunks.
  local cr = math.ceil(RADIUS / 32)
  for cx = -cr, cr do
    for cy = -cr, cr do
      s.request_to_generate_chunks({ cx * 32 + 16, cy * 32 + 16 }, 0)
    end
  end
  s.force_generate_chunk_requests()

  local area = { { -RADIUS, -RADIUS }, { RADIUS, RADIUS } }
  local all = s.find_entities_filtered { type = "resource", area = area }

  local by_name = {}
  for _, e in ipairs(all) do
    by_name[e.name] = by_name[e.name] or {}
    table.insert(by_name[e.name], e)
  end

  local out = {
    seed = s.map_gen_settings.seed,
    map_gen_settings = s.map_gen_settings,
    radius = RADIUS,
    patches = {},
    oil_spots = {},
  }

  for name, list in pairs(by_name) do
    -- Crude-oil is one entity per spot with per-spot yield — list individually.
    -- Everything else is dense tile-grid ore and gets clustered into patches.
    if name == "crude-oil" then
      for _, e in ipairs(list) do
        out.oil_spots[#out.oil_spots + 1] = {
          resource = name,
          x = e.position.x,
          y = e.position.y,
          amount = e.amount,
          distance = math.sqrt(e.position.x ^ 2 + e.position.y ^ 2),
        }
      end
    else
      for _, p in ipairs(cluster_patches(list)) do
        p.resource = name
        out.patches[#out.patches + 1] = p
      end
    end
  end

  -- Water: cluster contiguous tiles into bodies (centroid + tile_count, same
  -- shape as resource patches) for offshore-pump placement, and keep the single
  -- nearest-tile distance scalar for cheap "is water close?" checks.
  local water = s.find_tiles_filtered {
    name = { "water", "deepwater", "water-shallow", "water-mud", "water-green", "deepwater-green" },
    area = area,
  }
  local best = math.huge
  for _, t in ipairs(water) do
    -- tile.position is the top-left corner; tile center is +0.5.
    local cx, cy = t.position.x + 0.5, t.position.y + 0.5
    local d = math.sqrt(cx * cx + cy * cy)
    if d < best then best = d end
  end
  out.water_min_distance = (best < math.huge) and best or nil
  out.water_patches = cluster_water(water)

  -- Trees: a single count within the probed radius. Tree positions are L3's
  -- clear-cutting concern, not L2's; the count is enough to gauge wood supply
  -- and how much terrain needs clearing.
  out.tree_count = s.count_entities_filtered { type = "tree", area = area }

  game.write_file("l3_map.json", game.table_to_json(out))
  -- Sentinel: orchestrator polls for this and then SIGTERMs us.
  game.write_file("l3_map.done", "1")
end

-- Local Lua flag (not `global`) — resets on every game-load, which is what
-- we want: dump exactly once per Factorio process, regardless of whether the
-- save started at tick 0 or tick 286518.
local dumped = false

script.on_event(defines.events.on_tick, function(event)
  if not dumped then
    dumped = true
    local ok, err = pcall(dump)
    if not ok then
      game.write_file("l3_map.error", tostring(err))
    end
    game.write_file("l3_map.done", "1")
  end
end)
