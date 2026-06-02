"""L1 — technology research ordering.

Takes a `GoalState`, computes the transitive tech-prereq closure, and emits a
layered topological ordering. Ordering methods are pluggable — see `METHODS`:

  - `forward`   (default) — research order: layer 0 = techs with no prereqs in
    the required set; goal asks sit in the last layer(s). Each tech at the
    earliest layer it can be researched (ASAP).
  - `from-goal` — distance-from-goal order: layer 0 = the explicit asks; layer N
    = their N-th depth of prereqs. Backward planning (each tech ALAP).
  - `balanced`  — each tech at the midpoint of its slack window `[ASAP, ALAP]`:
    critical-chain (zero-slack) techs pinned, deferrable techs settle in the
    middle.

`verify_order` checks an existing order: every tech real and unique, the set
equals the goal's required closure, and the linearized order respects all
prerequisites.

Adding a new method: implement an `OrderingMethod` and register it in `METHODS`
(don't branch the driver). Future methods are expected to consume L2 metric
outputs — keep the signature flexible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from toposort import toposort

from fplan import goals
from fplan.model import GameModel, Technology, format_research_trigger


@dataclass(frozen=True)
class OrderResult:
    layers: tuple[tuple[str, ...], ...]
    notes: dict[str, Any] = field(default_factory=dict)


OrderingMethod = Callable[
    [dict[str, Technology], set[str], goals.GoalState],
    OrderResult,
]


def _closure(techs: dict[str, Technology], leaves: set[str]) -> set[str]:
    """Leaves plus every transitive prerequisite. Unknown leaves raise; an
    unknown prerequisite is kept (as a name) but not expanded — with
    fully-loaded vanilla data every prereq is a real tech, so this only
    surfaces on partial/mod loads."""
    for leaf in leaves:
        if leaf not in techs:
            raise KeyError(f"unknown technology: {leaf!r}")
    seen: set[str] = set()
    stack = list(leaves)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        t = techs.get(name)
        if t is None:
            continue
        stack.extend(t.prerequisites)
    return seen


def required_set(
    techs: dict[str, Technology], goal: goals.GoalState, model: GameModel
) -> set[str]:
    """Full tech-closure for a goal state: explicit asks + item-derived
    unlocking techs + transitive prereqs."""
    return _closure(techs, set(goals.required_techs(goal, model)))


def forward_order(
    techs: dict[str, Technology], required: set[str], _goal: goals.GoalState
) -> OrderResult:
    graph = {
        name: {p for p in techs[name].prerequisites if p in required}
        for name in required
    }
    layers = tuple(tuple(sorted(layer)) for layer in toposort(graph))
    return OrderResult(layers=layers)


def from_goal_order(
    techs: dict[str, Technology], required: set[str], _goal: goals.GoalState
) -> OrderResult:
    # Reversed edges: tech -> techs that depend on it. With this graph,
    # toposort emits "leaf goals" first (layer 0) and foundation techs
    # last. Works for multi-goal closures naturally: any tech with no
    # in-required dependents lands in layer 0.
    graph: dict[str, set[str]] = {name: set() for name in required}
    for name in required:
        for p in techs[name].prerequisites:
            if p in required:
                graph[p].add(name)
    layers = tuple(tuple(sorted(layer)) for layer in toposort(graph))
    return OrderResult(layers=layers)


def balanced_order(
    techs: dict[str, Technology], required: set[str], _goal: goals.GoalState
) -> OrderResult:
    """ASAP/ALAP-balanced order — the midpoint of each tech's slack window.

    For every tech compute two layers from the prereq DAG alone (no
    external input):
      - ASAP = forward longest-path layer (earliest the prereqs allow);
      - ALAP = `L_max − from-goal-depth` (latest the goal allows without
        lengthening the critical path).
    `slack = ALAP − ASAP`. Zero-slack techs are the critical chain and
    are pinned where forward and from-goal already agree. Positive-slack
    techs settle at the *midpoint* `(ASAP+ALAP)//2` — neither rushed to
    the front (forward) nor deferred to the back (from-goal). Layers are
    compacted to be contiguous; within a layer, ASAP order keeps every
    prerequisite ahead of its dependent (a prereq always has strictly
    smaller ASAP), with the tech name breaking ties.
    """
    fwd_graph = {
        name: {p for p in techs[name].prerequisites if p in required}
        for name in required
    }
    fwd_layers = list(toposort(fwd_graph))
    asap = {t: i for i, layer in enumerate(fwd_layers) for t in layer}
    l_max = len(fwd_layers) - 1

    rev_graph: dict[str, set[str]] = {name: set() for name in required}
    for name in required:
        for p in techs[name].prerequisites:
            if p in required:
                rev_graph[p].add(name)
    fg_layers = list(toposort(rev_graph))
    fg_depth = {t: i for i, layer in enumerate(fg_layers) for t in layer}
    alap = {t: l_max - fg_depth[t] for t in required}

    bal = {t: (asap[t] + alap[t]) // 2 for t in required}
    used = sorted(set(bal.values()))
    remap = {v: i for i, v in enumerate(used)}
    buckets: dict[int, list[str]] = {}
    for t in required:
        buckets.setdefault(remap[bal[t]], []).append(t)
    layers = tuple(
        tuple(sorted(buckets[i], key=lambda t: (asap[t], t))) for i in range(len(used))
    )

    crit = [
        t for t in sorted(required, key=lambda t: (asap[t], t)) if asap[t] == alap[t]
    ]
    notes = {
        "method_detail": "balanced: midpoint of ASAP (forward) / "
        "ALAP (from-goal) slack window",
        "critical_chain_zero_slack": crit,
    }
    return OrderResult(layers=layers, notes=notes)


METHODS: dict[str, OrderingMethod] = {
    "forward": forward_order,
    "from-goal": from_goal_order,
    "balanced": balanced_order,
}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    info: tuple[str, ...] = ()


def verify_order(
    techs: dict[str, Technology],
    model: GameModel,
    layers: list[list[str]],
    goal: goals.GoalState,
) -> VerifyResult:
    """Check that a layered tech order is a valid research plan for `goal`.

    Validity (errors → invalid):
      - every tech is a real, non-duplicated technology;
      - the set of techs equals the goal's required closure (nothing
        required is missing);
      - the *linearized* order — layers concatenated, within-layer order
        preserved, which is exactly what L2 consumes one-tech-per-step —
        places every tech strictly after all its in-set prerequisites.
    Non-fatal (warnings): techs the goal doesn't require (non-minimal
    order), and prereq/dependent pairs sharing a layer (legal as long as
    the linear order, checked above, keeps the prereq first).
    """
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []
    flat = [t for layer in layers for t in layer]

    unknown = [t for t in flat if t not in techs]
    if unknown:
        errors.append(f"unknown techs (not in model): {sorted(set(unknown))}")

    seen: set[str] = set()
    dups_set: set[str] = set()
    for t in flat:
        if t in seen:
            dups_set.add(t)
        seen.add(t)
    dups = sorted(dups_set)
    if dups:
        errors.append(f"duplicate techs: {dups}")

    try:
        required = required_set(techs, goal, model)
    except KeyError as e:
        errors.append(f"goal references unknown tech: {e}")
        required = set()
    present = set(flat)
    missing = sorted(required - present)
    extra = sorted(present - required)
    if missing:
        errors.append(f"missing required techs ({len(missing)}): {missing}")
    if extra:
        warnings.append(f"extra techs not required by goal ({len(extra)}): {extra}")

    # Linearized prerequisite ordering — the authoritative check for L2.
    pos = {t: i for i, t in enumerate(flat)}
    for i, t in enumerate(flat):
        if t not in techs:
            continue
        for p in techs[t].prerequisites:
            if p in present and pos[p] >= i:
                errors.append(
                    f"order: {t} (pos {i}) is not preceded by its "
                    f"prerequisite {p} (pos {pos[p]})"
                )

    # Same-layer prereq pairs: legal (linear order decides) but worth noting.
    layer_of = {t: li for li, layer in enumerate(layers) for t in layer}
    same_layer = sorted(
        f"{p} → {t} (both in layer {layer_of[t]})"
        for t in flat
        if t in techs
        for p in techs[t].prerequisites
        if p in present and layer_of.get(p) == layer_of.get(t)
    )
    if same_layer:
        warnings.append(
            f"prereq/dependent pairs sharing a layer ({len(same_layer)}): {same_layer}"
        )

    info.append(
        f"{len(flat)} techs across {len(layers)} layers; "
        f"goal {goal.name or '(unnamed)'!r} requires {len(required)}"
    )
    return VerifyResult(
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        info=tuple(info),
    )


# ---------------------------------------------------------------------------
# Formatting + artifact I/O
# ---------------------------------------------------------------------------

_DIRECTION = {
    "forward": "research order (layer 0 = earliest, last layer = goal asks)",
    "from-goal": (
        "distance from goal (layer 0 = goal asks, higher = deeper foundations)"
    ),
    "balanced": "ASAP/ALAP-balanced (slack-window midpoint; critical chain pinned)",
}


def format_tech(t: Technology) -> str:
    star = " *" if t.essential else ""
    trigger = format_research_trigger(t.research_trigger)
    if trigger:
        cost = f"trigger: {trigger}"
    elif t.ingredients:
        packs = ", ".join(f"{n}x{c}" for n, c in t.ingredients)
        count = t.count if t.count is not None else "?"
        cost = f"{count} × ({packs})"
    else:
        cost = "—"
    return f"{t.name}{star}  [{cost}]"


def format_layers(
    result: OrderResult,
    techs: dict[str, Technology],
    goal: goals.GoalState,
    method: str,
) -> str:
    """The human-readable layered view (the same shape the upstream CLI printed)."""
    total = sum(len(layer) for layer in result.layers)
    direction = _DIRECTION.get(method, method)
    label = goal.name or "(unnamed goal)"
    lines = [
        f"Goal {label!r}: {total} techs across {len(result.layers)} "
        f"layers — {direction}",
        "",
    ]
    for i, layer in enumerate(result.layers):
        tag = "  ← goal asks" if method == "from-goal" and i == 0 else ""
        plural = "s" if len(layer) != 1 else ""
        lines.append(f"── Layer {i}  ({len(layer)} tech{plural}){tag}")
        for name in layer:
            lines.append(f"   {format_tech(techs[name])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_payload(result: OrderResult, goal: goals.GoalState, method: str) -> dict:
    """The L1 output YAML document. The embedded `goal` block is what
    `verify` reads back; the schema is consumed by L2."""
    payload: dict = {
        "level": 1,
        "produced_by": "fplan tech-order build",
        "method": method,
        "goal": goal.as_dict(),
        "tech_count": sum(len(layer) for layer in result.layers),
        "layer_count": len(result.layers),
        "layers": [list(layer) for layer in result.layers],
    }
    if result.notes:
        payload["notes"] = result.notes
    return payload
