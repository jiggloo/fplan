"""Scenario input contract for L2+.

A `Scenario` bundles two things that share one YAML file:

  - an `InitialState` — the world at t₀ (items present, techs already
    researched). Read by L2+; L1 ignores it.
  - a `GoalState`     — the target world-state (see goals.py). Read by
    every level.

Backward compatibility: existing top-level keys (`name`,
`techs_researched`, `items_produced`, `rocket_launches`) still form
the goal, and an optional `initial_state:` block adds the L2+ inputs.
L1 keeps using `goals.load(path)` and ignores the extra block; L2+
uses `scenario.load(path)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fplan import goals
from fplan.goals import GoalState, ItemCounts


@dataclass(frozen=True)
class InitialState:
    """Items in the world and techs already researched at t₀.

    `items` covers everything — hand-placed buildings, raw stacks,
    crafted intermediates — without distinguishing inventory from
    deployed. L2 treats every building here as immediately
    contributing capacity (placement delay is an L3 concern).
    """

    items: ItemCounts = ()
    techs_researched: tuple[str, ...] = ()
    # In-game time at which this initial state holds. NOT necessarily 0:
    # a scenario seeded from a played save starts at whatever clock time
    # that snapshot was taken (default-victory's reference snapshot is
    # 3:12 = 192 s, the time to hand-craft the seeded burner/furnace base).
    # Purely a display offset — L2 solves in time relative to this t₀; the
    # visualizer shifts the timeline by it. Does not change the solve.
    timestamp_s: float = 0.0

    def as_dict(self) -> dict:
        d: dict = {}
        if self.items:
            d["items"] = {k: v for k, v in self.items}
        if self.techs_researched:
            d["techs_researched"] = list(self.techs_researched)
        if self.timestamp_s:
            d["timestamp_s"] = self.timestamp_s
        return d


# --- Checkpoints --------------------------------------------------------
#
# A `Checkpoint` is an intermediate state-predicate that must hold at a
# specific step boundary inside L2's run, not just at the final goal.
# Today supports one trigger kind ("before_action"); the structure is
# deliberately open so new kinds (after_tech, at_step_index, ...) can
# slot in without breaking existing scenarios. Linear-only constraints
# in v0 — these are just additional `item[name, b] >= bound` lower
# bounds, no MIP needed.
#
# Motivating use: force `rocket-silo >= 1` and `beacon >= 20` at the
# boundary immediately before the launch action, so the LP relaxation
# can't fractionalize the silo to 0.374 (the LP-cheat bug found while
# auditing the default-victory output).


@dataclass(frozen=True)
class CheckpointTrigger:
    """How to resolve a checkpoint to a step boundary.

    Kinds (v0):
      - "before_recipe": a new step is carved out at the latest point
        the named recipe was previously available. In all earlier
        steps the recipe becomes forbidden; in the new step it's
        allowed. The checkpoint's `requires` apply at the boundary
        at the START of the new step. Use this when a constraint
        needs to gate a specific atomic recipe — finer-grained than
        gating an "action" and lets items it produces (e.g. launch
        ingredients) follow via natural item-flow constraints.

    Future kinds will add fields without breaking existing ones.
    """

    kind: str
    recipe: str = ""


@dataclass(frozen=True)
class CheckpointRequires:
    """State predicate evaluated at the resolved boundary.

    v0 supports `items` — per-item lower bound. Future extensions
    (items_upper, forbidden_recipes, etc.) slot in as additional fields.
    """

    items: ItemCounts = ()


@dataclass(frozen=True)
class Checkpoint:
    name: str
    trigger: CheckpointTrigger
    requires: CheckpointRequires


@dataclass(frozen=True)
class Scenario:
    name: str = ""
    initial: InitialState = field(default_factory=InitialState)
    goal: GoalState = field(default_factory=GoalState)
    checkpoints: tuple[Checkpoint, ...] = ()
    source: str = ""


def _parse_timestamp(raw, field_name: str) -> float:
    """Parse an initial-state timestamp into seconds.

    Accepts a number (seconds) or a human-friendly "M:SS" / "MM:SS"
    string (e.g. "3:12" -> 192.0). Absent -> 0.0 (t₀ at the origin).
    """
    if raw is None:
        return 0.0
    if isinstance(raw, bool):  # guard: bool is an int subclass
        raise ValueError(f"{field_name}: expected number or 'M:SS' string")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if ":" in s:
            parts = s.split(":")
            if len(parts) != 2:
                raise ValueError(f"{field_name}: expected 'M:SS', got {raw!r}")
            return float(int(parts[0])) * 60.0 + float(parts[1])
        return float(s)
    raise ValueError(
        f"{field_name}: expected number or 'M:SS' string, got {type(raw).__name__}"
    )


def _initial_from_dict(raw, field_name: str) -> InitialState:
    if raw is None:
        return InitialState()
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name}: expected mapping, got {type(raw).__name__}")
    items = goals._coerce_counts(raw.get("items"), f"{field_name}.items")
    techs = raw.get("techs_researched") or ()
    if not isinstance(techs, (list, tuple)):
        raise ValueError(f"{field_name}.techs_researched: expected list of tech names")
    return InitialState(
        items=items,
        techs_researched=tuple(str(t) for t in techs),
        # `timestamp` (human "M:SS" or seconds) is the authored key;
        # `timestamp_s` (seconds float) is what as_dict round-trips.
        timestamp_s=_parse_timestamp(
            raw.get("timestamp", raw.get("timestamp_s")),
            f"{field_name}.timestamp",
        ),
    )


_VALID_TRIGGER_KINDS = {"before_recipe"}


def _checkpoints_from_list(raw, field_name: str) -> tuple[Checkpoint, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{field_name}: expected list, got {type(raw).__name__}")
    out: list[Checkpoint] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{field_name}[{idx}]: expected mapping")
        name = str(entry.get("name", f"checkpoint-{idx}") or f"checkpoint-{idx}")
        trig_raw = entry.get("trigger")
        if not isinstance(trig_raw, dict):
            raise ValueError(f"{field_name}[{idx}].trigger: expected mapping")
        kind = str(trig_raw.get("kind", ""))
        if kind not in _VALID_TRIGGER_KINDS:
            raise ValueError(
                f"{field_name}[{idx}].trigger.kind: {kind!r} not in "
                f"{sorted(_VALID_TRIGGER_KINDS)}"
            )
        recipe = str(trig_raw.get("recipe", ""))
        if kind == "before_recipe" and not recipe:
            raise ValueError(
                f"{field_name}[{idx}].trigger.recipe: required for kind=before_recipe"
            )
        req_raw = entry.get("requires") or {}
        if not isinstance(req_raw, dict):
            raise ValueError(f"{field_name}[{idx}].requires: expected mapping")
        items = goals._coerce_counts(
            req_raw.get("items"), f"{field_name}[{idx}].requires.items"
        )
        out.append(
            Checkpoint(
                name=name,
                trigger=CheckpointTrigger(kind=kind, recipe=recipe),
                requires=CheckpointRequires(items=items),
            )
        )
    return tuple(out)


def from_dict(d: dict, source: str = "") -> Scenario:
    """Build a Scenario from a parsed YAML dict.

    Top-level keys not in {initial_state, checkpoints} are fed to
    `goals.from_dict`, so files stay backward-compatible with L1.
    """
    initial = _initial_from_dict(d.get("initial_state"), "initial_state")
    checkpoints = _checkpoints_from_list(d.get("checkpoints"), "checkpoints")
    goal_dict = {
        k: v for k, v in d.items() if k not in ("initial_state", "checkpoints")
    }
    goal = goals.from_dict(goal_dict, source=source)
    return Scenario(
        name=str(d.get("name", "") or ""),
        initial=initial,
        goal=goal,
        checkpoints=checkpoints,
        source=source,
    )


def load(path: str | Path) -> Scenario:
    p = Path(path)
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping")
    return from_dict(data, source=str(p))
