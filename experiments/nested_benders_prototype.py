"""Nested Benders / dual dynamic programming prototype for the L2 staircase.

This is the issue-#56 "main spirit" prototype: confirm that a stage-wise
decomposition of an L2-shaped multistage problem scales (near-)LINEARLY in the
number of stages T, where a monolithic solve scales super-linearly — the
property that would make a finer time mesh affordable.

Per the issue's recommended path, this first rung uses a CONVEX synthetic
staircase that faithfully mirrors L2's structure:
  - carried state between stages = (inventory, accumulated machine count)
  - capacity tied to the accumulating count (build machines early to produce
    more later — the long-horizon "invest early to finish faster" trade #56
    cares about)
  - multistage objective summed across stages
Convexity makes Nested Benders EXACT, so we can validate the decomposition
reproduces the monolith optimum before tackling the bilinear (nonconvex) stages
in a later rung. The same forward/backward + cut machinery carries over; only
the cut family changes (McCormick-relaxed / Lagrangian) for the nonconvex case.

The monolith and the per-stage subproblems are built from the SAME
`_add_stage` helper, so the comparison is provably apples-to-apples.

Run: .venv/bin/python experiments/nested_benders_prototype.py
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pyscipopt import SCIP_PARAMSETTING, Model, quicksum


@dataclass(frozen=True)
class Data:
    """A convex multistage production/inventory instance.

    minimize  Sum_t [ prod_cost*x_t + build_cost*a_t + hold_cost*inv_{t+1} ]
    s.t.      inv_{t+1} = inv_t + x_t - demand_t        (inventory balance)
              count_{t+1} = count_t + a_t               (machines accumulate)
              x_t <= CAP * count_{t+1}                  (capacity; linear => convex)
              inv_t, x_t, a_t, count_t >= 0
    State carried tier-to-tier: (inv, count). count is non-decreasing (a_t>=0),
    exactly like L2's drill/furnace assignment counts; inv is the science/goal
    backbone. Demand rises over time, so the optimizer must invest in `count`
    early to serve later demand cheaply — the staircase's long-horizon trade.
    """

    T: int
    demand: tuple[float, ...]
    prod_cost: float = 1.0
    build_cost: float = 8.0
    hold_cost: float = 0.3
    CAP: float = 5.0
    inv0: float = 0.0
    count0: float = 1.0


def make_data(T: int) -> Data:
    # Deterministic, reproducible demand profile that ramps then plateaus —
    # forces capacity investment to be timed, not trivially front/back-loaded.
    demand = tuple(
        3.0 + 4.0 * (t / max(1, T - 1)) + (2.0 if (t % 7 == 0) else 0.0)
        for t in range(T)
    )
    return Data(T=T, demand=demand)


# --- shared per-stage constraint builder (used by BOTH monolith and stages) ---


def _add_stage(m, d: Data, t: int, inv_in, count_in, inv_out, count_out):
    """Add stage-t decision vars + constraints; return
    (stage_cost_expr, invbal_cons, cntbal_cons).

    inv_in/count_in may be vars (monolith threads them across the horizon) OR
    plain floats (a stage subproblem bakes its incoming state in as constants).
    Either way the constraints are identical, which is what guarantees the
    decomposition solves the SAME model as the monolith. The returned balance
    constraints carry the incoming state in their RHS, so their duals are the
    Benders subgradient of the stage value w.r.t. that state.
    """
    x = m.addVar(name=f"x_{t}", lb=0.0)  # production this stage
    a = m.addVar(name=f"a_{t}", lb=0.0)  # machines built this stage
    invbal = m.addCons(inv_out == inv_in + x - d.demand[t], name=f"invbal_{t}")
    cntbal = m.addCons(count_out == count_in + a, name=f"cntbal_{t}")
    m.addCons(x <= d.CAP * count_out, name=f"cap_{t}")
    stage_cost = d.prod_cost * x + d.build_cost * a + d.hold_cost * inv_out
    return stage_cost, invbal, cntbal


def _quiet_lp(m: Model) -> None:
    m.hideOutput()
    # Clean LP duals: presolve OFF (else the balance constraints get aggregated
    # away and getDualsolLinear hits a NULL transformed constraint).
    m.setPresolve(SCIP_PARAMSETTING.OFF)
    m.setHeuristics(SCIP_PARAMSETTING.OFF)
    m.setSeparating(SCIP_PARAMSETTING.OFF)
    m.setParam(
        "lp/initalgorithm", "s"
    )  # primal/dual simplex -> well-defined basis duals


# --- monolith ---


def solve_monolith(d: Data) -> tuple[float, float]:
    t0 = time.time()
    m = Model("monolith")
    _quiet_lp(m)
    inv = [m.addVar(name=f"inv_{i}", lb=0.0) for i in range(d.T + 1)]
    cnt = [m.addVar(name=f"cnt_{i}", lb=0.0) for i in range(d.T + 1)]
    m.addCons(inv[0] == d.inv0)
    m.addCons(cnt[0] == d.count0)
    total = []
    for t in range(d.T):
        sc, _, _ = _add_stage(m, d, t, inv[t], cnt[t], inv[t + 1], cnt[t + 1])
        total.append(sc)
    m.setObjective(quicksum(total), sense="minimize")
    m.optimize()
    obj = m.getObjVal()
    return obj, time.time() - t0


# --- nested Benders (deterministic; exact for this convex instance) ---


@dataclass
class Cut:
    # theta_{t} >= const + g_inv*inv_out_t + g_cnt*count_out_t
    const: float
    g_inv: float
    g_cnt: float


def _solve_stage(
    d: Data, t: int, s_inv: float, s_cnt: float, cuts: list[Cut], is_last: bool
):
    """Solve stage t given incoming state (s_inv, s_cnt) and the current
    cost-to-go cuts. Returns (stage_value_including_theta, inv_out, cnt_out).
    """
    m = Model(f"stage_{t}")
    _quiet_lp(m)
    inv_out = m.addVar(name="inv_out", lb=0.0)
    cnt_out = m.addVar(name="cnt_out", lb=0.0)
    stage_cost, _invbal, _cntbal = _add_stage(m, d, t, s_inv, s_cnt, inv_out, cnt_out)
    theta = m.addVar(name="theta", lb=0.0, ub=None)  # cost-to-go, floored by cuts
    if is_last:
        m.addCons(theta == 0.0)
    for k, c in enumerate(cuts):
        m.addCons(
            theta >= c.const + c.g_inv * inv_out + c.g_cnt * cnt_out, name=f"cut_{k}"
        )
    m.setObjective(stage_cost + theta, sense="minimize")
    m.optimize()
    return m.getObjVal(), m.getVal(inv_out), m.getVal(cnt_out)


def _stage_subgradient(
    d: Data,
    t: int,
    s_inv: float,
    s_cnt: float,
    cuts: list[Cut],
    is_last: bool,
    eps: float = 1e-5,
):
    """Value V_t(s) and its subgradient (dV/ds_inv, dV/ds_cnt) by central
    finite differences — sign-correct by construction (avoids SCIP dual-sign
    conventions). Still O(1) solves per stage, so the pass stays O(T)."""
    val, _, _ = _solve_stage(d, t, s_inv, s_cnt, cuts, is_last)
    vi_p = _solve_stage(d, t, s_inv + eps, s_cnt, cuts, is_last)[0]
    vi_m = _solve_stage(d, t, s_inv - eps, s_cnt, cuts, is_last)[0]
    vc_p = _solve_stage(d, t, s_inv, s_cnt + eps, cuts, is_last)[0]
    vc_m = _solve_stage(d, t, s_inv, s_cnt - eps, cuts, is_last)[0]
    di = (vi_p - vi_m) / (2 * eps)
    dc = (vc_p - vc_m) / (2 * eps)
    return val, di, dc


def solve_nested_benders(d: Data, max_iters: int = 80, tol: float = 1e-5):
    t0 = time.time()
    cuts: list[list[Cut]] = [[] for _ in range(d.T)]  # cuts[t] floors theta_t
    lb = -1e30
    ub = 1e30
    iters = 0
    for it in range(max_iters):
        iters = it + 1
        # ---- forward pass: propagate state, accumulate realized stage cost ----
        s_inv, s_cnt = d.inv0, d.count0
        realized = 0.0
        stage0_obj = None
        states = [(s_inv, s_cnt)]
        for t in range(d.T):
            val, iv, cv = _solve_stage(
                d, t, s_inv, s_cnt, cuts[t], is_last=(t == d.T - 1)
            )
            if t == 0:
                stage0_obj = val  # stage-0 objective (with cuts) = global lower bound
            # realized stage cost (drop the theta estimate, count actual cost):
            x_t = iv - s_inv + d.demand[t]
            a_t = cv - s_cnt
            stage_cost = d.prod_cost * x_t + d.build_cost * a_t + d.hold_cost * iv
            realized += stage_cost
            s_inv, s_cnt = iv, cv
            states.append((s_inv, s_cnt))
        lb = max(lb, stage0_obj if stage0_obj is not None else lb)
        ub = min(ub, realized)
        # One-sided: stop once the lower bound reaches within tol of the best
        # feasible cost. (lb may overshoot ub by FD noise on later iters — the
        # one-sided test fires at first closure, before that happens.)
        if ub - lb <= tol * (1.0 + abs(ub)):
            break
        # ---- backward pass: build cuts for theta_{t-1} from stage t ----
        for t in range(d.T - 1, 0, -1):
            s_inv, s_cnt = states[t]  # incoming state to stage t (= outgoing of t-1)
            val, di, dc = _stage_subgradient(
                d, t, s_inv, s_cnt, cuts[t], is_last=(t == d.T - 1)
            )
            # supporting hyperplane of the (convex) value function V_t at s:
            # theta_{t-1} >= val + di*(inv_out_{t-1} - s_inv) + dc*(count_out_{t-1} - s_cnt)
            const = val - di * s_inv - dc * s_cnt
            cuts[t - 1].append(Cut(const=const, g_inv=di, g_cnt=dc))
    return lb, ub, iters, time.time() - t0


def main() -> None:
    print("=== correctness: Nested Benders vs monolith (convex => exact) ===")
    ok = True
    for T in (5, 10, 25, 50):
        d = make_data(T)
        mono, _ = solve_monolith(d)
        lb, ub, iters, _ = solve_nested_benders(d)
        match = abs(ub - mono) <= 1e-3 * (1 + abs(mono))
        ok = ok and match
        print(
            f"  T={T:<4} monolith={mono:12.4f}  benders[lb={lb:12.4f} ub={ub:12.4f}] "
            f"iters={iters:<3} {'OK' if match else 'MISMATCH'}"
        )
    print(f"\ncorrectness: {'ALL MATCH' if ok else 'FAILURES PRESENT'}")

    print("\n=== scaling: wall time vs T (monolith vs nested Benders) ===")
    print(
        f"  {'T':>5} | {'monolith_s':>11} | {'benders_s':>10} | {'iters':>5} | {'mono/T(ms)':>10} | {'bend/T(ms)':>10}"
    )
    for T in (10, 25, 50, 100, 200, 300):
        d = make_data(T)
        mono, tm = solve_monolith(d)
        lb, ub, iters, tb = solve_nested_benders(d)
        match = abs(ub - mono) <= 1e-3 * (1 + abs(mono))
        flag = "" if match else "  <-- MISMATCH"
        print(
            f"  {T:>5} | {tm:>11.3f} | {tb:>10.3f} | {iters:>5} | "
            f"{1000 * tm / T:>10.3f} | {1000 * tb / T:>10.3f}{flag}"
        )


if __name__ == "__main__":
    main()
