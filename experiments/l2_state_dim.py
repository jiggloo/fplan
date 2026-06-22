"""Characterize the state-dimension problem of the L2 temporal decomposition (#56).

The carried state between consecutive stages is { item inventories } ∪ { assignment
buckets } at a tier. Exact decomposition methods (NC-NBD / regularized SDDP) are
exponential in this dimension d, so the question is the *effective* d, measured
three ways (loosest -> tightest):

  (1) NOMINAL d   — count of state components per tier (every tracked item +
                    every assignment bucket). Naive upper bound.
  (2) ACTIVE d    — inventory components actually *banked across a boundary*
                    (carry > eps), vs produced-and-consumed within a step
                    (carry ~0, no information). Assignment buckets counted
                    separately (irreducibly state).
  (3) CUT-RELEVANT d — components the cost-to-go responds to: per stage, the
                    sensitivity ∂V_i/∂state_k by FINITE DIFFERENCE on the full
                    stage solve (a perturbation that breaks feasibility = strong
                    coupling). The dimension cuts live in; governs the
                    decomposition's cost. (Node-limit-1 reduced costs were tried
                    first and were unreliable — SCIP-infinity duals.)

Incoming states come from a fresh, model-CONSISTENT monolith solve (the stale
rates.yaml leaves most stages infeasible, which corrupts the measurement).

Run: .venv/bin/python -m experiments.l2_state_dim [run] [mode]
"""

from __future__ import annotations

import sys

import experiments.l2_decompose as D
from fplan.l2 import solve as l2_solve

ASSIGN = ("drill_assign", "furnace_assign", "assembler_assign")
# handle-family name -> trajectory dict key (traj_from_rates uses short keys)
FAM_KEY = {
    "drill_assign": "drill",
    "furnace_assign": "furnace",
    "assembler_assign": "asm",
}


def _stage_value(inst, model, i, inc, asm_pins, is_last):
    """Full (true) stage solve value V_i given incoming inventory `inc` and
    pinned incoming assignment `asm_pins` {(fam,key)->value}. Time-capped.
    Returns objective, or None if infeasible/no-primal."""
    sub = D.make_stage_instance(inst, i, inc, is_last)
    m, h = l2_solve.build_lp(
        sub,
        model,
        verbose=False,
        seed=1,
        time_limit_s=10,
        lp_algorithm="barrier",
        decomposed=True,
    )
    for fam in ASSIGN:
        for k, v in h[fam].items():
            if k[-1] == 0:
                D._fix(m, v, asm_pins.get((fam, k[:-1]), 0.0))
    m.optimize()
    return m.getObjVal() if m.getNSols() else None


def fd_sensitivity(inst, model, i, traj, is_last, components):
    """Finite-difference ∂V_i/∂state_k for each component in `components`, at the
    (model-consistent) trajectory incoming state. A perturbation that turns the
    stage INFEASIBLE is flagged feasibility-critical (strong coupling). Returns
    {key: ('grad', |dV/dunit|) | ('feas', None)} or None if the base is infeasible."""
    tracked = [n for (n, t) in traj["item"] if t == i]
    inc0 = {n: traj["item"][(n, i)] for n in tracked}
    asm0 = {
        (fam, k[:-1]): traj[FAM_KEY[fam]].get(k[:-1] + (i,), 0.0)
        for fam in ASSIGN
        for k in traj[FAM_KEY[fam]]
        if k[-1] == i
    }
    base = _stage_value(inst, model, i, inc0, asm0, is_last)
    if base is None:
        return None
    out: dict = {}
    for kind, key in components:
        d = 50.0 if kind == "item" else 2.0
        if kind == "item":
            up = dict(inc0)
            up[key] = up.get(key, 0.0) + d
            dn = dict(inc0)
            dn[key] = max(0.0, dn.get(key, 0.0) - d)
            vp = _stage_value(inst, model, i, up, asm0, is_last)
            vm = _stage_value(inst, model, i, dn, asm0, is_last)
        else:
            up = dict(asm0)
            up[key] = up.get(key, 0.0) + d
            dn = dict(asm0)
            dn[key] = max(0.0, dn.get(key, 0.0) - d)
            vp = _stage_value(inst, model, i, inc0, up, is_last)
            vm = _stage_value(inst, model, i, inc0, dn, is_last)
        if vp is None or vm is None:
            out[(kind, key)] = ("feas", None)  # perturbation broke feasibility
        else:
            out[(kind, key)] = ("grad", abs(vp - vm) / (2 * d))
    return out


def main():
    run = sys.argv[1] if len(sys.argv) > 1 else "fishminer"
    mode = sys.argv[2] if len(sys.argv) > 2 else "trapezoidal"
    import pickle
    from pathlib import Path

    inst, model = D.build_run(run, mode)
    n = len(inst.steps)
    print(
        f"=== state-dimension characterization: '{run}' ({n} stages) ===\n", flush=True
    )
    # Model-CONSISTENT trajectory: a fresh conditioning-fixed monolith solve (the
    # stale rates.yaml leaves most stages infeasible). Cached for re-runs.
    cache = Path(f"/tmp/statedim_traj_{run}_{mode}.pkl")
    if cache.exists():
        traj = pickle.loads(cache.read_bytes())
        print(
            f"loaded cached monolith trajectory (obj={traj['obj']:.1f})\n", flush=True
        )
    else:
        print(
            "solving monolith for a consistent trajectory (multi-seed) ...", flush=True
        )
        traj, _ = D.solve_monolith(
            inst,
            model,
            time_limit=250,
            seeds=(276989655, 1219118205, 1019815643, 170600372, 1, 2, 3, 4),
        )
        if traj is None:
            print("no primal on any seed; cannot characterize.")
            return
        cache.write_bytes(pickle.dumps(traj))
        print(f"monolith obj={traj['obj']:.1f} (cached)\n", flush=True)

    # (1) NOMINAL d (per tier)
    items_with_var = sorted({nm for (nm, t) in traj["item"]})
    # count assignment buckets at a representative interior tier
    mid = n // 2
    assign_mid = sum(1 for fam in ASSIGN for k in traj[FAM_KEY[fam]] if k[-1] == mid)
    print(
        f"(1) NOMINAL d: {len(items_with_var)} tracked items + "
        f"~{assign_mid} assignment buckets/tier = ~{len(items_with_var) + assign_mid}"
    )

    # (2) ACTIVE d: items banked across an interior boundary
    eps = 1.0  # >1 unit carried at some interior boundary
    carried_max = {}
    for (nm, t), val in traj["item"].items():
        if 0 < t < n:  # interior boundary
            carried_max[nm] = max(carried_max.get(nm, 0.0), val)
    active_items = sorted(
        (nm for nm, c in carried_max.items() if c > eps),
        key=lambda nm: -carried_max[nm],
    )
    print(
        f"\n(2) ACTIVE d: {len(active_items)} items carry >{eps:g} unit across "
        f"some interior boundary (of {len(items_with_var)} tracked)"
    )
    print(f"    + ~{assign_mid} assignment buckets/tier (irreducibly state)")
    print("    top banked items:")
    for nm in active_items[:10]:
        print(f"      {nm:30} max carried = {carried_max[nm]:.1f}")

    # (3) CUT-RELEVANT d: finite-difference sensitivity on FULL stage solves at a
    # sample of stages (FD avoids the dual-extraction murk; full solve gives a
    # real primal). A perturbation that breaks feasibility = strong coupling.
    sample = sorted(set([1, n // 4, n // 2, 3 * n // 4, n - 2]))
    tol = 1e-3  # seconds of t_FINAL per unit of state
    # components to probe: active-banked items + all assignment buckets
    comp_items = [("item", nm) for nm in active_items]
    print(
        f"\n(3) CUT-RELEVANT d via FD on full solves, sampled stages {sample}",
        flush=True,
    )
    union: set = set()
    detail: dict = {}  # component -> {"stages": set, "feas": bool}
    for i in sample:
        lbl = inst.steps[i].label or inst.steps[i].research_tech or "FINAL"
        asm_keys = [
            (fam, k[:-1]) for fam in ASSIGN for k in traj[FAM_KEY[fam]] if k[-1] == i
        ]
        res = fd_sensitivity(
            inst,
            model,
            i,
            traj,
            i == n - 1,
            [("item", nm) for (_t, nm) in comp_items]
            + [(fam, k) for (fam, k) in asm_keys],
        )
        if res is None:
            print(f"    stage {i:2d} {lbl:24.24}: base infeasible (skip)", flush=True)
            continue
        inv_rel = [
            k
            for k, (kind, g) in res.items()
            if k[0] == "item" and (kind == "feas" or g > tol)
        ]
        asm_rel = [
            k
            for k, (kind, g) in res.items()
            if k[0] != "item" and (kind == "feas" or g > tol)
        ]
        feas_crit = [k for k, (kind, _g) in res.items() if kind == "feas"]
        union.update(inv_rel + asm_rel)
        for k in inv_rel + asm_rel:
            d_ = detail.setdefault(k, {"stages": set(), "feas": False})
            d_["stages"].add(i)
            if res[k][0] == "feas":
                d_["feas"] = True
        print(
            f"    stage {i:2d} {lbl:24.24}: cut-relevant = "
            f"{len(inv_rel)} inv + {len(asm_rel)} asm "
            f"({len(feas_crit)} feasibility-critical)",
            flush=True,
        )

    # The actual list, categorized: fluid vs solid, raw vs intermediate.
    def cat(name):
        it = model.items.get(name)
        if it is not None and getattr(it, "kind", None) == "fluid":
            return "fluid"
        if name in inst.tile_pool or name in ("crude-oil",):
            return "raw"
        return "solid"

    inv_comps = [k for k in union if k[0] == "item"]
    by_cat: dict = {"fluid": [], "raw": [], "solid": []}
    for k in inv_comps:
        by_cat[cat(k[1])].append(k[1])
    print("\n  --- cut-relevant inventory components, by category ---")
    for c in ("fluid", "raw", "solid"):
        names = sorted(by_cat[c])
        nfeas = sum(1 for nm in names if detail[("item", nm)]["feas"])
        print(
            f"  {c.upper():6} ({len(names)}, {nfeas} feasibility-critical): "
            f"{', '.join(names)}"
        )
    asm_comps = [k for k in union if k[0] != "item"]
    if asm_comps:
        print(f"  ASSIGN ({len(asm_comps)}): {', '.join(str(k[1]) for k in asm_comps)}")

    print("\n=== verdict ===")
    print(f"  nominal d ~ {len(items_with_var) + assign_mid}")
    print(f"  active d  ~ {len(active_items)} inv + {assign_mid} asm")
    inv_u = sum(1 for k in union if k[0] == "item")
    asm_u = len(union) - inv_u
    print(
        f"  cut-relevant d (union over sampled stages) ~ {len(union)} "
        f"({inv_u} inv + {asm_u} asm)"
    )


if __name__ == "__main__":
    main()
