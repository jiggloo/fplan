"""Goal-state input contract shared across planning levels.

A `GoalState` describes a target world-state the planner should reach.
It's intentionally general: the same shape describes a full victory
condition (e.g. satellite launched), a speedrun mini-goal (e.g.
`steel-axe` tech researched), or an arbitrary intermediate checkpoint
useful for experimenting with different tech orders.

Every planning level (L1 tech order, future L2+ production phases, ...)
reads a `GoalState`. Don't add per-level input types — extend this one.
A scenario YAML may also carry an `initial_state` / `checkpoints` block
for L2+; this loader reads only the goal keys and ignores the rest.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # avoid heavy import at module load time
    from fplan.model import GameModel


# Tuple-of-tuples (rather than dict) keeps GoalState frozen-hashable and
# round-trips cleanly through YAML. Consumers iterate with
# `for item, count in goal.items_produced: ...`.
ItemCounts = tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class GoalState:
    name: str = ""
    techs_researched: tuple[str, ...] = ()
    items_produced: ItemCounts = ()
    rocket_launches: ItemCounts = ()
    # Source path for traceability in output artifacts; not used by logic.
    source: str = ""

    def as_dict(self) -> dict:
        """Render back to a YAML-friendly dict (for embedding in outputs)."""
        d: dict = {}
        if self.name:
            d["name"] = self.name
        if self.techs_researched:
            d["techs_researched"] = list(self.techs_researched)
        if self.items_produced:
            d["items_produced"] = {k: v for k, v in self.items_produced}
        if self.rocket_launches:
            named = {k: v for k, v in self.rocket_launches if k}
            bare = sum(v for k, v in self.rocket_launches if not k)
            if named and bare:
                # Rare mixed case: keep the mapping but record the
                # payload-less count under the conventional `_count` key.
                d["rocket_launches"] = {"_count": bare, **named}
            elif named:
                d["rocket_launches"] = named
            else:
                d["rocket_launches"] = bare
        return d


def _coerce_counts(
    raw, field_name: str, *, allow_bare_count: bool = False
) -> ItemCounts:
    """YAML lets users write either a mapping ({beacon: 20}) or a list
    of pairs ([[beacon, 20]]); accept both, normalize to tuple-of-tuples
    sorted by name for deterministic output.

    When `allow_bare_count` is True (used for `rocket_launches`), a bare
    integer/float at the top level is also accepted and stored under the
    empty-string sentinel name: `1` → `(("", 1.0),)`. The sentinel means
    "N entries with no specific identity" — for rocket_launches that
    reads as "N payload-less launches"; the closure still pulls in the
    rocket-silo tech because the field is non-empty.
    """
    if raw is None:
        return ()
    if isinstance(raw, bool):
        # bool is a subclass of int; reject early to avoid `True → 1.0`.
        raise ValueError(f"{field_name}: expected mapping/list, got bool")
    if isinstance(raw, (int, float)):
        if not allow_bare_count:
            raise ValueError(
                f"{field_name}: bare count not allowed here; "
                "expected mapping or list of [name, count] pairs"
            )
        return (("", float(raw)),)
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for entry in raw:
            if isinstance(entry, dict) and len(entry) == 1:
                pairs.extend(entry.items())
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((entry[0], entry[1]))
            else:
                raise ValueError(
                    f"{field_name}: unrecognized entry {entry!r}; "
                    "expected mapping or [name, count] pair"
                )
    else:
        raise ValueError(
            f"{field_name}: expected mapping or list, got {type(raw).__name__}"
        )
    out: list[tuple[str, float]] = []
    for name, count in pairs:
        if not isinstance(name, str):
            raise ValueError(f"{field_name}: item name must be string, got {name!r}")
        out.append((name, float(count)))
    out.sort(key=lambda p: p[0])
    return tuple(out)


def from_dict(d: dict, source: str = "") -> GoalState:
    """Construct a GoalState from a parsed YAML/JSON dict."""
    techs = d.get("techs_researched") or ()
    if not isinstance(techs, (list, tuple)):
        raise ValueError("techs_researched: expected list of tech names")
    return GoalState(
        name=str(d.get("name", "") or ""),
        techs_researched=tuple(str(t) for t in techs),
        items_produced=_coerce_counts(d.get("items_produced"), "items_produced"),
        rocket_launches=_coerce_counts(
            d.get("rocket_launches"),
            "rocket_launches",
            allow_bare_count=True,
        ),
        source=source,
    )


def load(path: str | Path) -> GoalState:
    """Load a GoalState from a YAML file."""
    p = Path(path)
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping")
    return from_dict(data, source=str(p))


def required_techs(goal: GoalState, model: GameModel) -> set[str]:
    """Tech-closure for the goal state.

    Includes every explicitly-listed tech, plus the unlocking techs for
    every item in `items_produced` and `rocket_launches` (and the
    rocket-silo tech itself if any rockets are launched). Caller is
    responsible for taking the transitive prerequisite closure on top
    of this set — that part lives in `tech_order` so the L1 driver can
    reuse it with or without a goal state.

    NOTE: when an item is unlocked by multiple alternative recipes
    (different unlocking techs), the union is taken. That's
    deliberately conservative — pinning a specific route is the user's
    job, done by listing the desired tech explicitly in
    `techs_researched`. Future ordering methods may use L2 metrics to
    pick the cheapest route automatically.
    """
    techs: set[str] = set(goal.techs_researched)
    for item, _count in goal.items_produced:
        techs.update(_techs_for_item(item, model))
    for payload, _count in goal.rocket_launches:
        if payload:  # skip the empty-string sentinel for payload-less launches
            techs.update(_techs_for_item(payload, model))
    if goal.rocket_launches:
        techs.update(_techs_for_item("rocket-silo", model))
    return techs


def _techs_for_item(item_name: str, model: GameModel) -> Iterable[str]:
    """Unlocking-tech set for an item; empty if it's a starter item."""
    return model.unlocking_techs_for(item_name)
