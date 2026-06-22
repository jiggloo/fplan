"""Nested Benders prototype, rung 2: the NONCONVEX (bilinear) win demo.

Rung 1 (nested_benders_prototype.py) validated the decomposition machinery on a
convex staircase. This rung adds the property that makes L2 hard: a BILINEAR
capacity `x <= RATE * count * duration` (the `count * duration` term that drives
L2's nonconvexity). The objective is min total time `Sum_t duration_t` — L2's
`t_FINAL`.

The monolith is then a nonconvex NLP that SCIP solves by spatial branch-and-bound,
which scales super-linearly in the number of stages T. The decomposition instead:

  * LOWER BOUND (valid): run exact Nested Benders on the McCormick RELAXATION of
    each stage (convex => exact cuts => the relaxed optimum is a valid global
    lower bound on the true nonconvex problem). Scales ~linearly in T.
  * PRIMAL (feasible): a forward pass that solves each TRUE bilinear stage to
    global optimality (each stage is tiny, so SCIP closes it instantly), guided
    by the cost-to-go cuts from the relaxation. Scales linearly in T.

So LB <= monolith_opt <= UB, and both decomposition bounds are computed in
linear time where the monolith's exact solve blows up. This is the #56 thesis:
the per-stage subproblems stay small (cheaply global) while the count of stages
grows — the monolith feels T as super-linear pain; the decomposition feels it as
T easy stages.

Run: .venv/bin/python experiments/nested_benders_nonconvex.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pyscipopt import SCIP_PARAMSETTING, Model, quicksum


@dataclass(frozen=True)
class Data:
    T: int
    demand: tuple[float, ...]
    RATE: float = 1.0  # production per (machine * second)
    PLACE: float = 0.4  # seconds to place one machine (build-time cost)
    Cmax: float = 20.0  # machine-count cap (McCormick bound + area analogue)
    Dmax: float = 10.0  # per-stage duration cap (McCormick bound)
    inv0: float = 0.0
    count0: float = 1.0


def make_data(T: int) -> Data:
    demand = tuple(
        2.0 + 6.0 * (t / max(1, T - 1)) + (3.0 if t % 5 == 0 else 0.0) for t in range(T)
    )
    return Data(T=T, demand=demand)


def _quiet(m: Model, lp: bool) -> None:
    m.hideOutput()
    if lp:  # convex relaxed stage: clean LP, presolve OFF for stable behaviour
        m.setPresolve(SCIP_PARAMSETTING.OFF)
        m.setHeuristics(SCIP_PARAMSETTING.OFF)
        m.setSeparating(SCIP_PARAMSETTING.OFF)
        m.setParam("lp/initalgorithm", "s")


# --- true (bilinear) stage: used by the monolith and the forward primal ---


def _add_true_stage(m, d: Data, t, inv_in, cnt_in, inv_out, cnt_out, dur):
    x = m.addVar(name=f"x_{t}", lb=0.0)
    a = m.addVar(name=f"a_{t}", lb=0.0)
    m.addCons(inv_out == inv_in + x - d.demand[t], name=f"invbal_{t}")
    m.addCons(cnt_out == cnt_in + a, name=f"cntbal_{t}")
    m.addCons(dur >= d.PLACE * a, name=f"build_{t}")  # building takes time
    m.addCons(x <= d.RATE * cnt_out * dur, name=f"cap_{t}")  # BILINEAR (count*duration)
    return dur  # stage cost = duration (objective is min total time)


# --- McCormick-relaxed stage: convex; used for the valid lower bound ---


def _add_relaxed_stage(m, d: Data, t, inv_in, cnt_in, inv_out, cnt_out, dur):
    x = m.addVar(name=f"x_{t}", lb=0.0)
    a = m.addVar(name=f"a_{t}", lb=0.0)
    w = m.addVar(name=f"w_{t}", lb=0.0)  # w ~ cnt_out * dur, McCormick-relaxed
    invbal = m.addCons(inv_out == inv_in + x - d.demand[t], name=f"invbal_{t}")
    cntbal = m.addCons(cnt_out == cnt_in + a, name=f"cntbal_{t}")
    m.addCons(dur >= d.PLACE * a, name=f"build_{t}")
    # McCormick envelope of w = cnt_out*dur with cnt_out in [0,Cmax], dur in [0,Dmax]
    # (cL=dL=0 simplifies two of the four inequalities):
    m.addCons(w <= d.Cmax * dur, name=f"mc1_{t}")
    m.addCons(w <= d.Dmax * cnt_out, name=f"mc2_{t}")
    m.addCons(w >= d.Cmax * dur + d.Dmax * cnt_out - d.Cmax * d.Dmax, name=f"mc3_{t}")
    m.addCons(x <= d.RATE * w, name=f"cap_{t}")
    return dur, invbal, cntbal


# --- monolith (true nonconvex NLP) ---


def solve_monolith(d: Data, time_limit: float = 60.0):
    t0 = time.time()
    m = Model("monolith_nc")
    m.hideOutput()
    m.setParam("limits/time", time_limit)
    inv = [m.addVar(name=f"inv_{i}", lb=0.0) for i in range(d.T + 1)]
    cnt = [m.addVar(name=f"cnt_{i}", lb=0.0, ub=d.Cmax) for i in range(d.T + 1)]
    dur = [m.addVar(name=f"dur_{i}", lb=0.0, ub=d.Dmax) for i in range(d.T)]
    m.addCons(inv[0] == d.inv0)
    m.addCons(cnt[0] == d.count0)
    for t in range(d.T):
        _add_true_stage(m, d, t, inv[t], cnt[t], inv[t + 1], cnt[t + 1], dur[t])
    m.setObjective(quicksum(dur), sense="minimize")
    m.optimize()
    obj = m.getObjVal() if m.getNSols() > 0 else None
    return obj, m.getDualbound(), m.getStatus(), time.time() - t0


# --- relaxed stage solve (convex) for the lower-bound Benders ---


@dataclass
class Cut:
    const: float
    g_inv: float
    g_cnt: float


def _relaxed_stage_solve(d, t, s_inv, s_cnt, cuts, is_last):
    m = Model(f"rstage_{t}")
    _quiet(m, lp=True)
    inv_out = m.addVar(name="inv_out", lb=0.0)
    cnt_out = m.addVar(name="cnt_out", lb=0.0, ub=d.Cmax)
    dur = m.addVar(name="dur", lb=0.0, ub=d.Dmax)
    _add_relaxed_stage(m, d, t, s_inv, s_cnt, inv_out, cnt_out, dur)
    theta = m.addVar(name="theta", lb=0.0)
    if is_last:
        m.addCons(theta == 0.0)
    for k, c in enumerate(cuts):
        m.addCons(
            theta >= c.const + c.g_inv * inv_out + c.g_cnt * cnt_out, name=f"cut_{k}"
        )
    m.setObjective(dur + theta, sense="minimize")
    m.optimize()
    return m.getObjVal(), m.getVal(inv_out), m.getVal(cnt_out)


def _relaxed_subgrad(d, t, s_inv, s_cnt, cuts, is_last, eps=1e-5):
    val, _, _ = _relaxed_stage_solve(d, t, s_inv, s_cnt, cuts, is_last)
    di = (
        _relaxed_stage_solve(d, t, s_inv + eps, s_cnt, cuts, is_last)[0]
        - _relaxed_stage_solve(d, t, s_inv - eps, s_cnt, cuts, is_last)[0]
    ) / (2 * eps)
    dc = (
        _relaxed_stage_solve(d, t, s_inv, s_cnt + eps, cuts, is_last)[0]
        - _relaxed_stage_solve(d, t, s_inv, s_cnt - eps, cuts, is_last)[0]
    ) / (2 * eps)
    return val, di, dc


def lower_bound_benders(d: Data, max_iters=80, tol=1e-5):
    """Exact Nested Benders on the convex McCormick relaxation -> the relaxed
    optimum, which is a VALID global lower bound on the true nonconvex problem."""
    t0 = time.time()
    cuts = [[] for _ in range(d.T)]
    lb, ub = -1e30, 1e30
    iters = 0
    final_cuts = cuts
    for it in range(max_iters):
        iters = it + 1
        s_inv, s_cnt = d.inv0, d.count0
        realized, stage0, states = 0.0, None, [(s_inv, s_cnt)]
        for t in range(d.T):
            val, iv, cv = _relaxed_stage_solve(
                d, t, s_inv, s_cnt, cuts[t], t == d.T - 1
            )
            if t == 0:
                stage0 = val
            realized += (
                iv - s_inv + d.demand[t]
            ) * 0.0 + 0.0  # placeholder; dur recomputed below
            s_inv, s_cnt = iv, cv
            states.append((s_inv, s_cnt))
        # realized upper bound on the RELAXED problem = sum of stage durations on
        # the forward trajectory (recompute cleanly via a no-cut forward eval):
        realized = _relaxed_forward_cost(d, states)
        lb = max(lb, stage0 if stage0 is not None else lb)
        ub = min(ub, realized)
        if ub - lb <= tol * (1 + abs(ub)):
            break
        for t in range(d.T - 1, 0, -1):
            si, sc = states[t]
            val, di, dc = _relaxed_subgrad(d, t, si, sc, cuts[t], t == d.T - 1)
            cuts[t - 1].append(Cut(const=val - di * si - dc * sc, g_inv=di, g_cnt=dc))
        final_cuts = cuts
    return lb, iters, time.time() - t0, final_cuts


def _relaxed_forward_cost(d, states):
    """Sum of stage durations implied by a fixed state trajectory under the
    relaxed model (each stage's min duration given its fixed in/out states)."""
    total = 0.0
    for t in range(d.T):
        si, sc = states[t]
        so, co = states[t + 1]
        m = Model(f"fc_{t}")
        _quiet(m, lp=True)
        dur = m.addVar(name="dur", lb=0.0, ub=d.Dmax)
        x = m.addVar(name="x", lb=0.0)
        a = m.addVar(name="a", lb=0.0)
        w = m.addVar(name="w", lb=0.0)
        m.addCons(so == si + x - d.demand[t])
        m.addCons(co == sc + a)
        m.addCons(dur >= d.PLACE * a)
        m.addCons(w <= d.Cmax * dur)
        m.addCons(w <= d.Dmax * co)
        m.addCons(w >= d.Cmax * dur + d.Dmax * co - d.Cmax * d.Dmax)
        m.addCons(x <= d.RATE * w)
        m.setObjective(dur, sense="minimize")
        m.optimize()
        if m.getNSols() == 0:
            return 1e30
        total += m.getObjVal()
    return total


# --- forward primal over TRUE bilinear stages (feasible upper bound) ---


def _true_stage_solve(d, t, s_inv, s_cnt, cuts, is_last):
    m = Model(f"tstage_{t}")
    m.hideOutput()  # nonconvex: leave SCIP's spatial B&B on (tiny problem -> instant global)
    inv_out = m.addVar(name="inv_out", lb=0.0)
    cnt_out = m.addVar(name="cnt_out", lb=0.0, ub=d.Cmax)
    dur = m.addVar(name="dur", lb=0.0, ub=d.Dmax)
    _add_true_stage(m, d, t, s_inv, s_cnt, inv_out, cnt_out, dur)
    theta = m.addVar(name="theta", lb=0.0)
    if is_last:
        m.addCons(theta == 0.0)
    for k, c in enumerate(cuts):
        m.addCons(
            theta >= c.const + c.g_inv * inv_out + c.g_cnt * cnt_out, name=f"cut_{k}"
        )
    m.setObjective(dur + theta, sense="minimize")
    m.optimize()
    if m.getNSols() == 0:
        return None, None, None
    return m.getVal(dur), m.getVal(inv_out), m.getVal(cnt_out)


def forward_primal(d: Data, cuts):
    """One forward pass solving the TRUE bilinear stages to global, guided by the
    relaxation's cost-to-go cuts. Returns a feasible total duration (upper bound)."""
    t0 = time.time()
    s_inv, s_cnt = d.inv0, d.count0
    total = 0.0
    for t in range(d.T):
        dur, iv, cv = _true_stage_solve(d, t, s_inv, s_cnt, cuts[t], t == d.T - 1)
        if dur is None:
            return None, time.time() - t0
        total += dur
        s_inv, s_cnt = iv, cv
    return total, time.time() - t0


def main():
    print("=== rung 2: nonconvex (bilinear count*duration), min total time ===\n")
    print("validity (small T): need  LB <= monolith_opt <= UB")
    for T in (4, 6, 8):
        d = make_data(T)
        mono, mono_db, mono_st, _ = solve_monolith(d, time_limit=120)
        lb, iters, _, cuts = lower_bound_benders(d)
        ub, _ = forward_primal(d, cuts)
        valid = (lb <= (mono or 1e30) + 1e-3) and (
            (ub or -1e30) >= (mono or -1e30) - 1e-3
        )
        print(
            f"  T={T}: monolith={mono:.4f} ({mono_st})  LB={lb:.4f}  UB={ub:.4f}  "
            f"{'VALID' if valid else 'INVALID'}"
        )

    print("\nscaling: monolith (exact spatial B&B, 60s cap) vs decomposition (LB+UB)")
    print(
        f"  {'T':>4} | {'mono_obj':>9} {'mono_st':>10} {'mono_s':>8} | "
        f"{'LB':>9} {'UB':>9} {'decomp_s':>9}"
    )
    for T in (8, 16, 32, 64, 128):
        d = make_data(T)
        mono, mono_db, mono_st, tm = solve_monolith(d, time_limit=60)
        lb, iters, tlb, cuts = lower_bound_benders(d)
        ub, tub = forward_primal(d, cuts)
        td = tlb + tub
        mono_s = f"{mono:.3f}" if mono is not None else "—"
        print(
            f"  {T:>4} | {mono_s:>9} {mono_st:>10} {tm:>8.2f} | "
            f"{lb:>9.3f} {ub:>9.3f} {td:>9.2f}"
        )


if __name__ == "__main__":
    main()
