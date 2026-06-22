"""Real-L2 forward/backward Nested Benders loop (issue #56, rung A).

Quality-preserving decomposed solve over the validated stage interface
(l2_decompose.py): each stage is a one-step sub-instance built by the real
build_lp. The carried state between stages is inventories + assignment buckets.

  * FORWARD pass: from t0, solve each TRUE (bilinear) stage given its incoming
    state plus the current cost-to-go cuts; propagate the outgoing state. The
    realized Σ duration is a feasible primal (upper bound).
  * BACKWARD pass: at each forward state, read the subgradient of the stage value
    w.r.t. the incoming state (reduced costs of the tier-0 state vars — O(1) per
    stage) and add a supporting cut to the previous stage's cost-to-go θ.

Not myopic (θ carries the future), but the cuts are reduced-cost (local)
subgradients of nonconvex stages, so the lower bound is heuristic, not certified
— tightness needs NC-NBD (binary refinement + Lagrangian cuts). The PRIMAL is
genuinely feasible. We report the primal trajectory vs the monolith incumbent.

Run: .venv/bin/python -m experiments.l2_benders_loop [run] [mode] [iters]
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import experiments.l2_decompose as D
from fplan.l2 import solve as l2_solve

ASSIGN_FAMS = ("drill_assign", "furnace_assign", "assembler_assign")


@dataclass
class Cut:
    const: float
    grad: dict  # state-key -> coefficient (on the OUTGOING state vars)


def _state_at(h, m, tier):
    """Extract the carried state (inventories + assignment buckets) at `tier`
    from a solved stage model, keyed canonically."""
    st = {}
    for (n, t), v in h["item"].items():
        if t == tier:
            st[("item", n)] = m.getVal(v)
    for fam in ASSIGN_FAMS:
        for k, v in h[fam].items():
            if k[-1] == tier:
                st[(fam, k[:-1])] = m.getVal(v)
    return st


def _tier_vars(h, tier):
    """Map state-key -> SCIP var at `tier` (for pinning incoming / cutting on
    outgoing)."""
    out = {}
    for (n, t), v in h["item"].items():
        if t == tier:
            out[("item", n)] = v
    for fam in ASSIGN_FAMS:
        for k, v in h[fam].items():
            if k[-1] == tier:
                out[(fam, k[:-1])] = v
    return out


STAGE_TIME_LIMIT = 10.0  # per-stage cap; the forward primal needs a feasible
# point, not proven-optimal B&B (whose dual-simplex node resolves flood HiGHS).


def build_stage(inst, model, i, incoming_state, cuts, is_last, seed=1, time_limit=None):
    """Build stage i: incoming inventory via the sub-instance initial items;
    incoming assignment pinned on the built model; θ floored by cuts on the
    outgoing (tier-1) state vars."""
    incoming_inv = {n: v for (fam, n), v in incoming_state.items() if fam == "item"}
    sub = D.make_stage_instance(inst, i, incoming_inv, is_last)
    m, h = l2_solve.build_lp(
        sub,
        model,
        verbose=False,
        seed=seed,
        time_limit_s=time_limit,
        lp_algorithm="barrier",
        decomposed=True,
    )
    # pin incoming assignment buckets (tier 0) to the handoff state
    in_vars = _tier_vars(h, 0)
    for key, var in in_vars.items():
        if key[0] != "item" and key in incoming_state:
            D._fix(m, var, incoming_state[key])
    # cost-to-go theta floored by cuts on the OUTGOING (tier 1) state vars
    out_vars = _tier_vars(h, 1)
    theta = m.addVar(name="theta", lb=0.0)
    if is_last:
        m.addCons(theta == 0.0)
    for c in cuts:
        expr = c.const
        for key, g in c.grad.items():
            if key in out_vars:
                expr = expr + g * out_vars[key]
        m.addCons(theta >= expr)
    dur = h["duration"][0]
    m.setObjective(dur + theta, sense="minimize")
    return m, h, theta


def solve_stage_robust(inst, model, i, state, cuts, is_last, root_only=False):
    """Build + optimize a stage, retrying seeds on the HiGHS LP error. Returns
    (m, h, info) where info records what happened: seeds_tried, lp_errored (all
    seeds raised), status, time. (m, h) are None if every seed raised.

    root_only=True caps the search at the root node (the McCormick relaxation),
    used for the valid backward cut; the full solve is the forward primal."""
    import time as _t

    t0 = _t.time()
    seeds_tried = 0
    last_exc = "n/a"
    tl = None if root_only else STAGE_TIME_LIMIT
    for seed in (1, 2, 3):
        seeds_tried += 1
        m, h, _theta = build_stage(
            inst, model, i, state, cuts, is_last, seed=seed, time_limit=tl
        )
        if root_only:
            m.setParam("limits/nodes", 1)
        try:
            m.optimize()
        except Exception as exc:
            last_exc = str(exc)
            continue
        info = {
            "seeds_tried": seeds_tried,
            "lp_errored": False,
            "status": m.getStatus(),
            "nsols": m.getNSols(),
            "time": _t.time() - t0,
        }
        return m, h, info
    return (
        None,
        None,
        {
            "seeds_tried": seeds_tried,
            "lp_errored": True,
            "status": f"lp_error: {last_exc}",
            "nsols": 0,
            "time": _t.time() - t0,
        },
    )


def solve_forward(inst, model, cuts_by_stage, n, init_state):
    """Forward pass: feasible primal + per-stage outgoing states + per-stage
    instrumentation records."""
    s = init_state
    states = [s]
    realized = 0.0
    stage0_val = None
    feasible = True
    fail = None
    records = []
    for i in range(n):
        m, h, info = solve_stage_robust(inst, model, i, s, cuts_by_stage[i], i == n - 1)
        dur = m.getVal(h["duration"][0]) if (m is not None and info["nsols"]) else None
        records.append((i, info["status"], info["seeds_tried"], info["time"], dur))
        # live progress (so a hang/flood is visible without waiting for the pass)
        lbl = inst.steps[i].label or inst.steps[i].research_tech or "FINAL"
        dur_str = f"{dur:.1f}" if dur is not None else "—"
        print(
            f"    stage {i:2d} {lbl:28.28} status={info['status']:<12.12} "
            f"seeds={info['seeds_tried']} t={info['time']:.2f}s dur={dur_str}",
            flush=True,
        )
        if m is None or info["nsols"] == 0:
            feasible = False
            reason = "lp_error" if (info.get("lp_errored")) else info["status"]
            fail = (i, reason)
            break
        if i == 0:
            stage0_val = m.getObjVal()  # with cost-to-go => global lower bound
        realized += dur
        s = _state_at(h, m, 1)
        states.append(s)
    return feasible, realized, stage0_val, states, fail, records


def diagnose_failure(inst, model, i, incoming_state, cuts, is_last):
    """When a forward stage is infeasible, isolate the cause: is it the cuts
    (cost-to-go cut conflict), the goal floor (last stage), or genuinely the
    stage given its incoming state? Re-solve with pieces removed."""
    label = inst.steps[i].label or inst.steps[i].research_tech or "FINAL"
    lines = [f"  diagnose stage {i} ({label}):"]
    # (a) without cuts
    m, h, info = solve_stage_robust(inst, model, i, incoming_state, [], is_last)
    lines.append(f"    without cuts: status={info['status']} nsols={info['nsols']}")
    # (b) without the goal floor (only meaningful on the last stage)
    if is_last and inst.final_floors:
        m2, h2, info2 = solve_stage_robust(
            inst, model, i, incoming_state, cuts, is_last=False
        )
        lines.append(
            f"    without goal floor: status={info2['status']} nsols={info2['nsols']}"
        )
    return "\n".join(lines)


def backward_cut(inst, model, i, incoming_state, cuts, is_last):
    """Solve stage i's McCormick RELAXATION (root only) at its incoming state and
    return (relaxation value, subgradient). The relaxation is convex, so its dual
    bound is a valid lower bound on the stage value and the tier-0 reduced costs
    are a valid subgradient — a sound Benders cut (vs the invalid reduced costs
    of the nonconvex full solve)."""
    m, h, _info = solve_stage_robust(
        inst, model, i, incoming_state, cuts, is_last, root_only=True
    )
    if m is None:
        return None, None
    val = m.getDualbound()  # McCormick relaxation lower bound on the stage value
    if val is None or abs(val) > 1e30:
        return None, None
    in_vars = _tier_vars(h, 0)
    grad = {}
    for key, var in in_vars.items():
        try:
            rc = m.getVarRedcost(var)
        except Exception:
            rc = 0.0
        if rc is not None and abs(rc) < 1e30 and abs(rc) > 1e-9:
            grad[key] = rc
    return val, grad


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else "fishminer"
    mode = sys.argv[2] if len(sys.argv) > 2 else "trapezoidal"
    iters = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    inst, model = D.build_run(run, mode)
    n = len(inst.steps)
    ref = D.traj_from_rates(run)
    print(
        f"=== Benders loop on '{run}' ({n} stages); monolith incumbent ≈ "
        f"{ref['obj']:.1f} ===",
        flush=True,
    )

    # initial state = scenario t0 (stage-0 incoming): inventory only; assignment
    # buckets start unpinned (tier 0 of the run is the true start).
    init_state = {
        ("item", k): float(v) for k, v in inst.effective_initial_items.items()
    }

    cuts_by_stage = [[] for _ in range(n)]
    best_primal = float("inf")
    for it in range(iters):
        t0 = time.time()
        feasible, realized, lb, states, fail, records = solve_forward(
            inst, model, cuts_by_stage, n, init_state
        )
        # instrumentation: per-stage solve summary (always, so a failure has data)
        n_done = len(records)
        n_lperr = sum(1 for r in records if str(r[1]).startswith("lp_error"))
        n_retry = sum(1 for r in records if r[2] > 1)
        times = sorted(r[3] for r in records)
        tmed = times[len(times) // 2] if times else 0.0
        print(
            f"iter {it + 1}: forward solved {n_done}/{n} stages "
            f"(lp_errors={n_lperr}, multi-seed-retries={n_retry}, "
            f"median stage {tmed:.2f}s, max {max(times, default=0):.2f}s)",
            flush=True,
        )
        if not feasible:
            fi, reason = fail
            lbl = inst.steps[fi].label or inst.steps[fi].research_tech or "FINAL"
            print(
                f"  -> FORWARD STOPPED at stage {fi} ({lbl}): {reason}",
                flush=True,
            )
            if not str(reason).startswith("lp_error"):
                # algorithmic (infeasible) — isolate the cause
                print(
                    diagnose_failure(
                        inst, model, fi, states[fi], cuts_by_stage[fi], fi == n - 1
                    ),
                    flush=True,
                )
            break
        best_primal = min(best_primal, realized)
        fwd_t = time.time() - t0
        # backward: add a cut to each stage's predecessor
        t1 = time.time()
        ncuts = 0
        for i in range(n - 1, 0, -1):
            val, grad = backward_cut(
                inst, model, i, states[i], cuts_by_stage[i], i == n - 1
            )
            if val is None:
                continue
            const = val - sum(g * states[i][k] for k, g in grad.items())
            cuts_by_stage[i - 1].append(Cut(const=const, grad=grad))
            ncuts += 1
        bwd_t = time.time() - t1
        print(
            f"iter {it + 1}: primal(Σdur)={realized:.1f}  best={best_primal:.1f}  "
            f"lb(stage0)={lb:.1f}  +{ncuts} cuts  "
            f"[fwd {fwd_t:.1f}s, bwd {bwd_t:.1f}s]",
            flush=True,
        )

    print(
        f"\nbest decomposed primal = {best_primal:.1f}   "
        f"monolith incumbent ≈ {ref['obj']:.1f}   "
        f"({'BEATS' if best_primal < ref['obj'] else 'does not beat'} incumbent)"
    )


if __name__ == "__main__":
    main()
