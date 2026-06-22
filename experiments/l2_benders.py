"""Real-L2 decomposed solve — forward stage-solver + scaling/subgradient probes.

Builds on the validated stage interface (l2_decompose.py). Before committing to a
full forward/backward Nested Benders loop, this measures the two things that
decide whether it is tractable on the real model:

  (A) PER-STAGE SOLVE COST — is each one-step subproblem cheap (the linear-in-T
      premise)? Solve every fishminer stage given a realistic incoming state and
      report the time distribution; compare the sum to the monolith's 600s wall.

  (B) SUBGRADIENT EXTRACTION — can the cost-to-go subgradient w.r.t. the incoming
      state be read from ONE stage solve (via reduced costs of the tier-0 state
      vars), giving O(1) solves/stage? Finite differences would be O(state-dim),
      which is the curse-of-dimensionality the literature flags as the real cost
      driver. This probe checks the reduced costs are available and finite.

Run: .venv/bin/python experiments/l2_benders.py [run] [mode] [mono_time_s]
"""

from __future__ import annotations

import sys
import time

import experiments.l2_decompose as D
from fplan.l2 import solve as l2_solve


def solve_stage_true(inst, model, i, incoming_inv, is_last):
    """Solve the TRUE (bilinear) one-step subproblem for stage i given its
    incoming inventory, outgoing free. Returns (duration, solve_time, handles,
    model, status)."""
    sub = D.make_stage_instance(inst, i, incoming_inv, is_last)
    t0 = time.time()
    m, h = l2_solve.build_lp(
        sub, model, verbose=False, seed=1, lp_algorithm="barrier", decomposed=True
    )
    m.optimize()
    dt = time.time() - t0
    dur = m.getVal(h["duration"][0]) if m.getNSols() else None
    return dur, dt, h, m, m.getStatus()


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else "fishminer"
    mode = sys.argv[2] if len(sys.argv) > 2 else "trapezoidal"
    mono_time = float(sys.argv[3]) if len(sys.argv) > 3 else 400.0
    inst, model = D.build_run(run, mode)
    n = len(inst.steps)
    print(f"=== decomposed-solve probes on '{run}' ({n} steps) ===", flush=True)
    # Reference trajectory from the committed rates.yaml (deterministic; the
    # fishminer monolith primal is too flaky/slow to re-solve each run). Only
    # used to supply realistic incoming states for the probes.
    traj = D.traj_from_rates(run)
    print(f"reference trajectory from rates.yaml (obj={traj['obj']:.2f})\n", flush=True)

    # (A) per-stage solve cost: each stage given its monolith incoming inventory.
    print(
        "(A) per-stage TRUE-subproblem solve cost (incoming=monolith state):",
        flush=True,
    )
    times = []
    durs_ok = 0
    redcost_ok = 0
    redcost_probed = 0
    for i in range(n):
        tracked = [nm for (nm, t) in traj["item"] if t == i]
        inc = {nm: traj["item"][(nm, i)] for nm in tracked}
        dur, dt, h, m, st = solve_stage_true(inst, model, i, inc, is_last=(i == n - 1))
        times.append(dt)
        if dur is not None:
            durs_ok += 1
        # (B) subgradient probe on EVERY feasible stage: reduced costs of tier-0
        # state vars (item[·,0]) — the incoming-state subgradient for a Benders
        # cut, read from one solve (O(1)/stage).
        if m.getNSols():
            redcost_probed += 1
            finite = 0
            total = 0
            for (_nm, t), v in h["item"].items():
                if t == 0:
                    total += 1
                    try:
                        rc = m.getVarRedcost(v)
                        if rc is not None and abs(rc) < 1e30:
                            finite += 1
                    except Exception:
                        pass
            if finite == total and total > 0:
                redcost_ok += 1

    times.sort()
    tot = sum(times)
    print(f"\n  stages solved feasibly: {durs_ok}/{n}")
    print(
        f"  per-stage solve time: min={times[0]:.3f}s  median={times[n // 2]:.3f}s  "
        f"max={times[-1]:.3f}s"
    )
    print(f"  Σ per-stage solve time = {tot:.1f}s   (monolith wall ≈ {mono_time:g}s)")
    print(
        f"\n(B) reduced-cost subgradient available on "
        f"{redcost_ok}/{redcost_probed} probed stages "
        f"({'O(1)/stage cuts feasible' if redcost_ok == redcost_probed and redcost_probed else 'needs FD fallback — dimensionality risk'})"
    )


if __name__ == "__main__":
    main()
