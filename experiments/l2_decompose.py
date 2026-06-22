"""Real-L2 temporal decomposition — stage subproblems built by REUSING build_lp.

A faithful per-stage subproblem is a one-step "sub-instance": tier 0 = incoming
state (inventory via effective_initial_items; assignment-bucket split fixed via
the returned handles), tier 1 = outgoing state. Calling the real `build_lp` on
that sub-instance reuses every constraint family verbatim, so a stage model
cannot drift from the monolith. The two genuinely cross-step couplings (launch
equality, wood budget) are dropped in stages via `decomposed=True` —
launches are carried by the tracked LAUNCH_EVENT_ITEM inventory + final floor;
wood is deferred (a cumulative counter is the eventual home).

Scenario-agnostic: resolves inputs from a run manifest, so it targets fishminer
(the real test) and any future run without per-scenario code.

First milestone (this file): END-TO-END validation that the stage interface is
correct on the real model — solve the monolith, then rebuild each stage with
BOTH boundaries pinned to the monolith trajectory and confirm every stage is
feasible and the durations sum back to the monolith objective. A mismatch
localizes a missing piece of carried state (the debugging tool). The standalone
decomposed SOLVE (forward/backward Benders) builds on this.

Run: .venv/bin/python experiments/l2_decompose.py [run_name] [mode] [mono_time_s]
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import yaml

from fplan import scenario as scenario_mod
from fplan.cli import main as cli_main
from fplan.l2 import config as l2_config_mod
from fplan.l2 import instance as l2_instance
from fplan.l2 import solve as l2_solve

REPO = Path(__file__).resolve().parent.parent
EX = REPO / "examples"


def build_run(run_name: str, mode: str):
    """Build the L2 instance for an example run, resolving inputs from its
    manifest (paths are relative to examples/)."""
    man = yaml.safe_load((EX / "runs" / run_name / "manifest.yaml").read_text())
    ins = man["inputs"]
    scen = EX / ins["scenario"]["path"]
    l1 = EX / ins["tech-order"]["path"]
    mp = EX / ins["map"]["path"] if "map" in ins else None
    model = cli_main.load_model_or_exit(REPO / ".fplan-config.yaml")
    cfg = l2_config_mod.load_config(None)
    inst = l2_instance.build_instance(
        scenario_mod.load(scen),
        l1,
        model,
        mode=mode,
        map_probe_path=mp,
        deployment_enabled=True,
        player_time_enabled=True,
        l2_config=cfg,
    )
    return inst, model


# --- monolith solve + full state-trajectory extraction ---


def solve_monolith(inst, model, seed=276989655, time_limit=None, seeds=None):
    # fishminer's primal is stochastic and the HiGHS LP can throw a fatal
    # numerical error on some seeds — retry a few known-good seeds until one
    # returns a feasible primal.
    h = None
    m = None
    for s in seeds or (276989655, 1219118205, 1019815643):
        m, h = l2_solve.build_lp(
            inst,
            model,
            verbose=False,
            seed=s,
            time_limit_s=time_limit,
            lp_algorithm="barrier",
        )
        try:
            m.optimize()
        except Exception as exc:  # SCIP: error in LP solver! (HiGHS numerical)
            print(f"  [monolith seed={s} LP error: {exc}; retrying]", flush=True)
            continue
        if m.getNSols() > 0:
            break
        print(
            f"  [monolith seed={s}: no primal in {time_limit}s; retrying]", flush=True
        )
    if m is None or m.getNSols() == 0:
        return None, h
    g = m.getVal
    traj = {
        "obj": m.getObjVal(),
        "status": m.getStatus(),
        "item": {k: g(v) for k, v in h["item"].items()},
        "drill": {k: g(v) for k, v in h["drill_assign"].items()},
        "furnace": {k: g(v) for k, v in h["furnace_assign"].items()},
        "asm": {k: g(v) for k, v in h["assembler_assign"].items()},
        # asm pooled count is not pinned: the link constraint
        # (pool + Σ assigned == item[building]) determines it once item[building]
        # and the assigned buckets are pinned.
        "duration": {k: g(v) for k, v in h["duration"].items()},
    }
    return traj, h


def traj_from_rates(run_name: str):
    """Build a reference trajectory from a run's committed rates.yaml instead of
    re-solving the monolith (deterministic; sidesteps the flaky/slow fishminer
    primal). count_start is the value at tier i, count_end at tier i+1.
    Assignment building names are `base@key` (e.g. electric-mining-drill@coal)."""
    d = yaml.safe_load((EX / "runs" / run_name / "rates.yaml").read_text())
    steps = d["steps"]
    n = len(steps)
    item: dict = {}
    drill: dict = {}
    furnace: dict = {}
    asm: dict = {}
    duration: dict = {}
    for i, s in enumerate(steps):
        duration[i] = s.get("duration_s", 0.0)
        for it in s.get("items", []):
            item[(it["name"], i)] = it["count_start"]
            if i == n - 1:
                item[(it["name"], i + 1)] = it["count_end"]
        for rec in s.get("mining_assignment", []) or []:
            base = rec["building"].split("@", 1)[0]
            drill[(base, rec["ore"], i)] = rec["count_start"]
            if i == n - 1:
                drill[(base, rec["ore"], i + 1)] = rec["count_end"]
        for rec in s.get("smelting_assignment", []) or []:
            base = rec["building"].split("@", 1)[0]
            furnace[(base, rec["output"], i)] = rec["count_start"]
            if i == n - 1:
                furnace[(base, rec["output"], i + 1)] = rec["count_end"]
        for rec in s.get("assembler_assignment", []) or []:
            base = rec["building"].split("@", 1)[0]
            asm[(base, rec["recipe"], i)] = rec["count_start"]
            if i == n - 1:
                asm[(base, rec["recipe"], i + 1)] = rec["count_end"]
    # also fill tier i+1 from the next step's count_start (== this step's end)
    for i in range(n - 1):
        for it in steps[i + 1].get("items", []):
            item.setdefault((it["name"], i + 1), it["count_start"])
        for rec in steps[i + 1].get("mining_assignment", []) or []:
            base = rec["building"].split("@", 1)[0]
            drill.setdefault((base, rec["ore"], i + 1), rec["count_start"])
        for rec in steps[i + 1].get("smelting_assignment", []) or []:
            base = rec["building"].split("@", 1)[0]
            furnace.setdefault((base, rec["output"], i + 1), rec["count_start"])
        for rec in steps[i + 1].get("assembler_assignment", []) or []:
            base = rec["building"].split("@", 1)[0]
            asm.setdefault((base, rec["recipe"], i + 1), rec["count_start"])
    return {
        "obj": float(d.get("solver", {}).get("objective_s", sum(duration.values()))),
        "status": "from-rates",
        "item": item,
        "drill": drill,
        "furnace": furnace,
        "asm": asm,
        "duration": duration,
    }


# --- one-step sub-instance for stage i ---


def make_stage_instance(inst, i: int, incoming_inv, is_last: bool):
    step0 = replace(inst.steps[i], index=0)
    return replace(
        inst,
        steps=(step0,),
        effective_initial_items=dict(incoming_inv),
        final_floors=dict(inst.final_floors) if is_last else {},
        checkpoints=(),
    )


def _fix(m, var, value, tol=1e-5):
    # Pin to a tolerance BAND, not lb==ub: exact float pins leave no slack for
    # tight constraints (the per-ore drill capacity in particular) and turn
    # solver-level numerical noise into spurious infeasibility.
    band = tol * (1.0 + abs(value))
    m.chgVarLb(var, value - band)
    m.chgVarUb(var, value + band)


def _pin_assign(m, h, traj, sub_tier, mono_tier):
    for (b, ore, t), v in h["drill_assign"].items():
        if t == sub_tier:
            _fix(m, v, traj["drill"].get((b, ore, mono_tier), 0.0))
    for (b, out, t), v in h["furnace_assign"].items():
        if t == sub_tier:
            _fix(m, v, traj["furnace"].get((b, out, mono_tier), 0.0))
    for (b, r, t), v in h["assembler_assign"].items():
        if t == sub_tier:
            _fix(m, v, traj["asm"].get((b, r, mono_tier), 0.0))


def reconstruct_stage(inst, model, i, traj, n_steps):
    """Build stage i with BOTH boundaries pinned to the monolith trajectory;
    return (status, duration | None, infeasible)."""
    is_last = i == n_steps - 1
    tracked = [n for (n, t) in traj["item"] if t == i]
    incoming_inv = {n: traj["item"][(n, i)] for n in tracked}
    sub = make_stage_instance(inst, i, incoming_inv, is_last)
    m, h = l2_solve.build_lp(
        sub, model, verbose=False, seed=1, lp_algorithm="barrier", decomposed=True
    )
    # Pin outgoing inventory (sub tier 1 <- monolith tier i+1).
    for (n, t), v in h["item"].items():
        if t == 1 and (n, i + 1) in traj["item"]:
            _fix(m, v, traj["item"][(n, i + 1)])
    _pin_assign(m, h, traj, sub_tier=0, mono_tier=i)
    _pin_assign(m, h, traj, sub_tier=1, mono_tier=i + 1)
    m.optimize()
    if m.getNSols() == 0:
        return m.getStatus(), None, True
    return m.getStatus(), m.getVal(h["duration"][0]), False


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else "fishminer"
    mode = sys.argv[2] if len(sys.argv) > 2 else "trapezoidal"
    mono_time = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
    print(
        f"=== real-L2 stage decomposition: consistency check on '{run}' "
        f"(mode={mode}) ===\n",
        flush=True,
    )
    inst, model = build_run(run, mode)
    n = len(inst.steps)
    print(
        f"steps={n}; solving monolith (time_limit={mono_time:g}s) for a "
        f"reference trajectory ...",
        flush=True,
    )
    traj, _ = solve_monolith(inst, model, time_limit=mono_time)
    if traj is None:
        print("monolith found NO primal in the time limit; raise mono_time.")
        return
    print(f"monolith: obj={traj['obj']:.4f} ({traj['status']})\n", flush=True)

    print("per-stage reconstruction (both boundaries pinned to monolith):", flush=True)
    total = 0.0
    infeasible = []
    mism = []
    for i in range(n):
        st, dur, infeas = reconstruct_stage(inst, model, i, traj, n)
        mono_dur = traj["duration"].get(i, float("nan"))
        if infeas:
            infeasible.append(i)
            print(
                f"  stage {i:2d}: INFEASIBLE ({st})  [monolith dur={mono_dur:.3f}]",
                flush=True,
            )
        else:
            total += dur
            ok = abs(dur - mono_dur) <= 1e-3 * (1 + abs(mono_dur))
            if not ok:
                mism.append(i)
            tag = "OK" if ok else "DUR MISMATCH"
            print(
                f"  stage {i:2d}: dur={dur:9.4f}  monolith={mono_dur:9.4f}  {tag}",
                flush=True,
            )

    consistent = (
        not infeasible
        and not mism
        and abs(total - traj["obj"]) <= 1e-3 * (1 + abs(traj["obj"]))
    )
    print(f"\n  Σ stage durations = {total:.4f}   monolith obj = {traj['obj']:.4f}")
    print(
        f"  infeasible stages: {infeasible or 'none'}   duration mismatches: {mism or 'none'}"
    )
    print(
        f"  => {'CONSISTENT (stage interface validated)' if consistent else 'INCONSISTENT — missing carried state; investigate above'}"
    )


if __name__ == "__main__":
    main()
