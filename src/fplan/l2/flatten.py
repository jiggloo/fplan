"""L2 rate-flattening — the *current* operation of ``fplan rates post``.

``rates post`` is the L2→L3 post-processing stage (still under development; it
will grow more operations). This module implements its current one: rate-
flattening. Reads an L2 solve output (``rates.yaml``) and, for each produced item,
computes
the smoothest constant-rate-per-segment production schedule that still meets
every deadline. The headline per-item metric is **#revisits** = the number of
distinct constant-rate segments the schedule collapses to — each rate change is
a point where a TAS player must walk back to the assemblers and re-allocate
machines (real player-time, minimized for a WR TAS).

The flattening is bounded by *game causality*: the original solve's
running-total production is a hard UPPER bound — you cannot produce an item
before its tech is researched, before its machines exist, or before its inputs
exist, and the solver already proved that schedule feasible. So the flattened
running-total curve must live inside the tube

    R(t)  <=  P'(t)  <=  P_orig(t)

where R(t) is the running-total requirement (lower bound — never stock out) and
P_orig(t) is the original running-total production (upper bound — never produce
earlier than the solver did). Three rules (``--method``): ``tube`` (the taut
string through that tube, default), ``chord`` (straight chords between
surplus-zero deadlines — a cautionary baseline that can self-stockout), and
``mrp`` (cross-dependency backward demand explosion). See
``docs/L2-rate-flattening.md`` for the full formulation.

This module is the solver-neutral *logic*: it consumes the L2 YAML plus the
game model (needed for the unmet-input diagnostics and the ``mrp`` dependency
graph) and emits a flattened ``rates``-shaped dict plus the diagnostics. The CLI
(``fplan rates post``) and the viz (``fplan.l2.viz``) are the only callers.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass

# Inventory at/below this (units) at a boundary counts as a hard deadline.
EPS_ZERO = 1e-2
# Rates within this (units/s) are treated as equal (no revisit / no deficit).
EPS_RATE = 1e-6
# Units; ignore sub-half-item shortfalls (LP relaxation dust).
EPS_SHORT = 0.5


# --------------------------------------------------------------------------
# Taut string through a vertical-gate channel: the Euclidean shortest path
# from (X[0], y0) to (X[-1], yN) staying within [BOT[k], TOP[k]] at every
# gate X[k]. A shortest path in a corridor turns only at corners, so the
# candidate vertices are the gate corners; with N ~ 48 gates an O(N^3)
# DAG shortest path is exact and trivially fast.
# --------------------------------------------------------------------------
def taut_string(X, BOT, TOP, y0, yN):
    n = len(X)
    if n == 1:
        return [(X[0], y0)]
    verts = [(X[0], y0, 0)]  # (x, y, gate)
    for k in range(1, n - 1):
        verts.append((X[k], BOT[k], k))
        verts.append((X[k], TOP[k], k))
    verts.append((X[-1], yN, n - 1))
    m = len(verts)
    INF = float("inf")

    def valid(ui, wi):
        xu, yu, gu = verts[ui]
        xw, yw, gw = verts[wi]
        if xw == xu:
            # Vertical segment: duplicate timestamps from (consecutive)
            # zero-duration steps. No horizontal extent, so no interior gate can
            # be violated — and guarding here avoids a divide-by-zero.
            return True
        for g in range(gu + 1, gw):
            yy = yu + (yw - yu) * (X[g] - xu) / (xw - xu)
            if yy < BOT[g] - 1e-9 or yy > TOP[g] + 1e-9:
                return False
        return True

    dist = [INF] * m
    prev = [-1] * m
    dist[0] = 0.0
    for wi in range(1, m):
        xw, yw, gw = verts[wi]
        for ui in range(wi):
            xu, yu, gu = verts[ui]
            if gu >= gw or dist[ui] == INF or not valid(ui, wi):
                continue
            d = dist[ui] + ((xw - xu) ** 2 + (yw - yu) ** 2) ** 0.5
            if d < dist[wi] - 1e-12:
                dist[wi] = d
                prev[wi] = ui
    if dist[m - 1] == INF:  # tube infeasible — straight fallback
        return [(X[0], y0), (X[-1], yN)]
    path, i = [], m - 1
    while i != -1:
        x, y, _ = verts[i]
        path.append((x, y))
        i = prev[i]
    path.reverse()
    return path


def _interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    if xs[hi] == xs[lo]:
        return ys[hi]
    t = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] + t * (ys[hi] - ys[lo])


# --------------------------------------------------------------------------
# Per-item timeline reconstruction.
# --------------------------------------------------------------------------
class ItemTrace:
    def __init__(self, name: str, n_steps: int):
        self.name = name
        self.produced = [0.0] * n_steps
        self.orig_rate = [0.0] * n_steps
        self.inv_boundary = [0.0] * (n_steps + 1)
        self._seen = False
        self._first_cs = 0.0

    def observe(self, k: int, it: dict):
        self.produced[k] = float(it.get("produced") or 0.0)
        self.orig_rate[k] = float(it.get("production_rate_per_s") or 0.0)
        if not self._seen:
            self._first_cs = float(it.get("count_start") or 0.0)
            self._seen = True
        self.inv_boundary[k + 1] = float(it.get("count_end") or 0.0)

    def finalize(self, present: list[bool]):
        self.inv_boundary[0] = self._first_cs
        for k in range(len(present)):
            if not present[k]:
                self.inv_boundary[k + 1] = self.inv_boundary[k]

    def running_total(self) -> list[float]:
        P = [0.0] * (len(self.produced) + 1)
        for k, p in enumerate(self.produced):
            P[k + 1] = P[k] + p
        return P


def build_traces(steps):
    n = len(steps)
    traces: dict[str, ItemTrace] = {}
    present: dict[str, list[bool]] = {}
    for k, s in enumerate(steps):
        for it in s.get("items", []):
            name = it["name"]
            tr = traces.get(name)
            if tr is None:
                tr = traces[name] = ItemTrace(name, n)
                present[name] = [False] * n
            tr.observe(k, it)
            present[name][k] = True
    for name, tr in traces.items():
        tr.finalize(present[name])
    return traces


# --------------------------------------------------------------------------
# Flattening.
# --------------------------------------------------------------------------
class FlatResult:
    def __init__(self, name):
        self.name = name
        self.flat_rate: list[float] = []
        self.running_flat: list[float] = []
        self.revisits = 0
        self.orig_segments = 0
        self.self_stockouts = 0
        self.total_units = 0.0
        self.excluded = False


def _segments(rates) -> int:
    segs, prev = 0, None
    for r in rates:
        if prev is None or abs(r - prev) > EPS_RATE:
            segs += 1
        prev = r
    return segs


def flatten_item(tr: ItemTrace, t, method: str, eps_zero: float) -> FlatResult:
    n = len(tr.produced)
    P = tr.running_total()
    floor = [P[k] - tr.inv_boundary[k] for k in range(n + 1)]  # R[k]
    res = FlatResult(tr.name)
    res.total_units = P[n]
    res.orig_segments = _segments(tr.orig_rate)

    if P[n] <= EPS_RATE:
        res.flat_rate = list(tr.orig_rate)
        res.running_flat = list(P)
        res.revisits = res.orig_segments
        return res

    if method == "tube":
        bot = [max(floor[k], 0.0) for k in range(n + 1)]
        top = list(P)  # P_orig running-total
        path = taut_string(t, bot, top, 0.0, P[n])
        px = [p[0] for p in path]
        py = [p[1] for p in path]
        running = [_interp(px, py, t[k]) for k in range(n + 1)]
    elif method == "chord":
        deadlines = [0]
        for k in range(1, n):
            if tr.inv_boundary[k] <= eps_zero:
                deadlines.append(k)
        deadlines.append(n)
        dx = [t[i] for i in deadlines]
        dy = [0.0 if i == 0 else (P[n] if i == n else floor[i]) for i in deadlines]
        running = [_interp(dx, dy, t[k]) for k in range(n + 1)]
    else:
        raise ValueError(f"unknown method {method!r}")

    # Self-stockouts: flattened running-total below the requirement floor.
    for k in range(n + 1):
        if running[k] < floor[k] - 1e-3:
            res.self_stockouts += 1

    rates = []
    for k in range(n):
        dur = t[k + 1] - t[k] or 1e-9
        rates.append(max(0.0, (running[k + 1] - running[k]) / dur))
    res.flat_rate = rates
    res.running_flat = running
    res.revisits = _segments(rates)
    return res


# --------------------------------------------------------------------------
# MRP-style flattening: backward demand explosion through the dependency
# graph, chord flattener per level. Smoothing starts at science (whose
# demand is exogenous research draw) and propagates backward: each item is
# flattened to meet its *consumers'* already-flattened demand (plus its own
# exogenous research / launch / goal draws). The backward pass is run as a
# Jacobi fixpoint (all items update from the prior round's scales), which
# reaches the same result as a strict science-first topological sweep but
# also tolerates the gear->assembler->gear feedback that would stall a
# topo sort.
#
# Effect: because each level targets its consumers' *smoothed* demand, the
# per-level demand is itself smoother, so intermediates need FEWER revisits
# than the independent chord. It does NOT, however, make the chord feasible:
# the chord between two surplus-zero deadlines can still dip below the
# propagated floor (a self-stockout). Resolving those is a deliberate
# *following* stage (minimal-perturbation repair), not done here — this is
# the low-revisit stage-1 intermediate. Fluids and raw mined/pumped items
# are excluded (already rate-pinned by non-fungible drills). v1 = aggregate.
# --------------------------------------------------------------------------
MRP_MAX_ITERS = 40
MRP_TOL = 1e-4  # units; convergence on per-step flattened production


def mrp_exclude_set(model) -> set:
    fluids, producers = set(), defaultdict(list)
    for r in model.recipes.values():
        for o in r.outputs:
            producers[o.name].append(r)
            if getattr(o, "kind", None) == "fluid":
                fluids.add(o.name)
        for ing in r.ingredients:
            if getattr(ing, "kind", None) == "fluid":
                fluids.add(ing.name)
    raw = {
        it
        for it, rs in producers.items()
        if rs and all(r.kind in ("mining", "pumping") for r in rs)
    }
    return fluids | raw


def _chord_to_demand(dem, tr, dur, t, eps_zero, total):
    """Chord-flatten production using the ORIGINAL solve's surplus-zero
    deadline structure (for science, the research-completion timestamps) for
    the interior shape, but anchoring the final running-total to the item's
    original total production ``total`` so the area under the curve is
    CONSERVED (same as the chord method). The propagated demand only reshapes
    the rate between deadlines; it does not change how much is made — which
    keeps building accumulation (produced-but-not-consumed machines) intact.
    Returns flat produced units per step."""
    n = len(dem)
    running_demand = [0.0] * (n + 1)
    for k in range(n):
        running_demand[k + 1] = running_demand[k] + dem[k]
    inv0 = tr.inv_boundary[0]
    floor = [max(0.0, running_demand[k] - inv0) for k in range(n + 1)]
    deadlines = [0]
    for k in range(1, n):
        if tr.inv_boundary[k] <= eps_zero:
            deadlines.append(k)
    deadlines.append(n)
    dx = [t[i] for i in deadlines]
    dy = [0.0 if i == 0 else (total if i == n else floor[i]) for i in deadlines]
    running = [_interp(dx, dy, t[k]) for k in range(n + 1)]
    return [max(0.0, running[k + 1] - running[k]) for k in range(n)]


def flatten_mrp(steps, traces, model, t):
    n = len(steps)
    dur = [(t[k + 1] - t[k]) or 1e-9 for k in range(n)]
    items = list(traces)
    exclude = mrp_exclude_set(model)

    consumed = {it: [0.0] * n for it in items}
    for k, s in enumerate(steps):
        for it in s.get("items", []):
            if it["name"] in consumed:
                consumed[it["name"]][k] = float(it.get("consumed") or 0.0)
    produced = {it: traces[it].produced for it in items}

    rcyc = defaultdict(lambda: [0.0] * n)
    for k, s in enumerate(steps):
        for act in s.get("activity", []) or []:
            rcyc[act["recipe"]][k] += float(act.get("cycles") or 0.0)

    consumers = defaultdict(list)  # item -> [(recipe, amount_per_cycle)]
    principal = {}  # recipe -> principal output item
    for rname in rcyc:
        rec = model.recipes.get(rname)
        if rec is None:
            continue
        if rec.outputs:
            principal[rname] = rec.outputs[0].name
        for ing in rec.ingredients:
            consumers[ing.name].append((rname, ing.amount))

    # Exogenous (non-recipe) demand = consumed minus what tracked recipes
    # draw: the research / launch / goal pull. Fixed under flattening.
    exo = {}
    for B in items:
        cons_list = consumers.get(B, [])
        exo[B] = [
            max(
                0.0,
                consumed[B][k] - sum(amt * rcyc[r][k] for (r, amt) in cons_list),
            )
            for k in range(n)
        ]

    # Jacobi fixpoint: all items update from the previous round's scales,
    # which handles the gear->assembler->gear feedback without a topo sort.
    flat = {it: list(produced[it]) for it in items}
    for _ in range(MRP_MAX_ITERS):
        scale = {}
        for oi in items:
            po, fo = produced[oi], flat[oi]
            scale[oi] = [
                (fo[k] / po[k])
                if abs(po[k]) > EPS_RATE
                else (0.0 if abs(fo[k]) <= EPS_RATE else 1.0)
                for k in range(n)
            ]
        new = {}
        for B in items:
            if B in exclude:
                new[B] = list(produced[B])
                continue
            cons_list = consumers.get(B, [])
            dem = []
            for k in range(n):
                d = exo[B][k]
                for r, amt in cons_list:
                    oi = principal.get(r)
                    if oi is not None:
                        d += amt * rcyc[r][k] * scale[oi][k]
                dem.append(d)
            new[B] = _chord_to_demand(
                dem, traces[B], dur, t, EPS_ZERO, sum(produced[B])
            )
        diff = max(
            (abs(new[B][k] - flat[B][k]) for B in items for k in range(n)),
            default=0.0,
        )
        flat = new
        if diff < MRP_TOL:
            break

    flats = {}
    for B in items:
        fr = FlatResult(B)
        fr.excluded = B in exclude
        fr.flat_rate = [flat[B][k] / dur[k] for k in range(n)]
        running = [0.0] * (n + 1)
        for k in range(n):
            running[k + 1] = running[k] + flat[B][k]
        fr.running_flat = running
        fr.total_units = sum(produced[B])
        fr.orig_segments = _segments(traces[B].orig_rate)
        fr.revisits = _segments(fr.flat_rate)
        flats[B] = fr
    return flats


# --------------------------------------------------------------------------
# Unmet inputs (buffer-aware, running-total). For each input item, compare the
# flattened RUNNING-TOTAL production through a step against how much the raw L2
# solution required by then (running-total consumption minus initial inventory,
# i.e. the floor R[k] = P_raw[k] - inv[k]). A shortfall means the flattened
# plan has, in total, made fewer of that input than were needed by then —
# inventory would have gone negative. Unlike a per-step rate comparison this
# correctly credits buffers / reserves built up earlier (it is the
# running-total integral, not the instantaneous rate). Attributed to each
# recipe consuming the short input at that step.
# --------------------------------------------------------------------------
def compute_deficits(steps, traces, flats, model, t):
    n = len(steps)
    rfloor = {}  # item -> [R[b] for b in 0..n]  (raw running-total requirement)
    for iname, tr in traces.items():
        P = tr.running_total()
        rfloor[iname] = [P[b] - tr.inv_boundary[b] for b in range(n + 1)]

    lines = []
    for k, s in enumerate(steps):
        b = k + 1  # boundary at end of step k
        consumers = defaultdict(list)  # input -> [recipe, ...]
        for act in s.get("activity", []) or []:
            rname = act.get("recipe")
            if float(act.get("cycles") or 0.0) <= 0:
                continue
            rec = model.recipes.get(rname)
            if rec is None:
                continue
            for ing in rec.ingredients:
                consumers[ing.name].append(rname)
        for item, rnames in consumers.items():
            fl = flats.get(item)
            if fl is None or not fl.running_flat:
                continue
            required = rfloor[item][b]  # raw: must have been made by now
            made = fl.running_flat[b]  # flattened: actually made by now
            short = required - made
            if short <= EPS_SHORT:
                continue
            # Time slack: units short / the faster of the adjacent production
            # rates — roughly how much in-game time a real plan has to make
            # up the shortfall (overall finish time is set by total resources
            # gathered, which is fixed even when intermediates shift).
            r_before = fl.flat_rate[k] if k < len(fl.flat_rate) else 0.0
            r_after = fl.flat_rate[k + 1] if (k + 1) < len(fl.flat_rate) else 0.0
            rate = max(r_before, r_after)
            if rate <= EPS_RATE and fl.flat_rate:
                rate = max(fl.flat_rate)  # fallback: item's peak rate
            short_time = (short / rate) if rate > EPS_RATE else None
            for rname in dict.fromkeys(rnames):  # one line per consuming recipe
                lines.append(
                    {
                        "step": k,
                        "label": s.get("label", ""),
                        "time": t[k],
                        "recipe": rname,
                        "input": item,
                        "short": short,
                        "short_time": short_time,
                        "made": made,
                        "required": required,
                    }
                )
    lines.sort(key=lambda r: (r["step"], r["recipe"], r["input"]))
    return lines


# --------------------------------------------------------------------------
# Orchestration + the rates-post.yaml output.
# --------------------------------------------------------------------------
METHODS = ("tube", "chord", "mrp")


@dataclass
class FlattenResult:
    """The full output of one flatten run: the per-item schedules, the
    unmet-input report, and the step-boundary time grid. Everything the CLI
    needs to write ``rates-post.yaml`` and everything the viz needs to draw
    the diff is reachable from here (no recomputation downstream)."""

    method: str
    t: list[float]
    flats: dict[str, FlatResult]
    deficits: list[dict]

    def scored(self) -> list[FlatResult]:
        """Items that carry production and aren't rate-pinned (the mrp
        exclude set) — the ones whose revisit counts are meaningful."""
        return [
            f
            for f in self.flats.values()
            if f.total_units > EPS_RATE and not f.excluded
        ]

    def summary(self) -> dict:
        scored = self.scored()
        revisits = sum(f.revisits for f in scored)
        orig = sum(f.orig_segments for f in scored)
        return {
            "items_scored": len(scored),
            "revisits": revisits,
            "orig_segments": orig,
            "revisits_saved": orig - revisits,
            "self_stockouts": sum(f.self_stockouts for f in scored),
            "deficit_lines": len(self.deficits),
        }


def flatten(
    l2: dict, *, method: str, model, eps_zero: float = EPS_ZERO
) -> FlattenResult:
    """Flatten every item in an L2 solve dict by ``method`` and compute the
    unmet-input report. ``model`` is required for the dependency-graph
    diagnostics (and the ``mrp`` explosion); see ``compute_deficits``."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; choose from {', '.join(METHODS)}")
    steps = l2.get("steps", []) or []
    initial = float(l2.get("initial_time_s", 0.0) or 0.0)
    t = [initial]
    for s in steps:
        t.append(t[-1] + float(s.get("duration_s") or 0.0))
    traces = build_traces(steps)
    if method == "mrp":
        flats = flatten_mrp(steps, traces, model, t)
    else:
        flats = {
            name: flatten_item(tr, t, method, eps_zero) for name, tr in traces.items()
        }
    deficits = compute_deficits(steps, traces, flats, model, t)
    return FlattenResult(method=method, t=t, flats=flats, deficits=deficits)


def build_post_yaml(l2: dict, result: FlattenResult, *, source_ref: str) -> dict:
    """Produce the ``rates-post.yaml`` dict: the L2 solve dict with each
    item's **production characteristics** (``production_rate_per_s`` and
    ``produced``) replaced by the flattened schedule, plus a sibling ``post:``
    metadata block carrying the method, the source reference, the summary, and
    the per-item / unmet-input diagnostics.

    PROVISIONAL by design: this is the temporary L2→L3 input, and its schema is
    temporary too — it mirrors the ``rates.yaml`` shape only because L3's
    preferred format isn't decided yet (see ``docs/L2-rate-flattening.md`` and
    issue #25). The flattening operation rewrites *production* only; consumption /
    inventory columns pass through unchanged from the solve (the divergence
    between flattened production and the solve's inventory is exactly what the
    unmet-input report quantifies). Don't build anything downstream that assumes
    this is stable.
    """
    out = copy.deepcopy(l2)
    t = result.t
    for k, s in enumerate(out.get("steps", []) or []):
        dur = (t[k + 1] - t[k]) if (k + 1) < len(t) else 0.0
        items = s.setdefault("items", [])
        by_name = {it.get("name"): it for it in items}
        for name, fl in result.flats.items():
            if k >= len(fl.flat_rate):
                continue
            rate = fl.flat_rate[k]
            row = by_name.get(name)
            if row is None:
                # The solve omits an item from a step when it has no activity
                # and zero inventory there; but the flattener (notably `mrp`,
                # which reshapes by propagated consumer demand) can want to
                # produce it in that step. Without a row those units would be
                # silently dropped from the artifact — breaking area
                # conservation. Synthesize a production-only row (consumption /
                # inventory default to zero: the item had no presence here).
                if rate <= EPS_RATE:
                    continue
                row = {
                    "name": name,
                    "consumption_rate_per_s": 0.0,
                    "consumed": 0.0,
                    "count_start": 0.0,
                    "count_end": 0.0,
                }
                items.append(row)
            row["production_rate_per_s"] = rate
            row["produced"] = rate * dur

    out["post"] = {
        "method": result.method,
        "source": source_ref,
        # Marker that this file is the provisional L2→L3 input (rates-mirror
        # schema), not a solve output. Auto-detected by `fplan rates viz`.
        "schema": "provisional-rates-mirror",
        "summary": result.summary(),
        "per_item": {
            f.name: {
                "revisits": f.revisits,
                "orig_segments": f.orig_segments,
                "self_stockouts": f.self_stockouts,
                "excluded": f.excluded,
                "total_units": f.total_units,
            }
            for f in result.flats.values()
            if f.total_units > EPS_RATE
        },
        "deficits": result.deficits,
    }
    return out
