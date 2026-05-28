"""
Session 1 — Sourcing Allocation (Ch2 LP + Ch3 IP)

Decision:   x[i, s] ≥ 0    — units of item i sourced from supplier s.
            y[i, s] ∈ {0,1} — 1 if supplier s is used at all for item i (optional,
                              activates a fixed sourcing cost).
Objective:  min  Σ_{i,s} effective_cost[i,s] · x[i,s]  +  Σ_{i,s} fixed_cost · y[i,s]
            where effective_cost[i,s] = unit_cost[i]
                                       · (1 + α · (100 - reliability[s]) / 100
                                            + β · lead_time[s] / 30)
Constraints:
            Σ_s x[i,s]          = demand[i]               ∀ i   (meet demand)
            x[i,s]              ≤ M · y[i,s]              ∀ i,s (link)
            Σ_s y[i,s]          ≤ max_suppliers_per_item   ∀ i  (diversification cap)
            Σ_i x[i,s]          ≤ capacity[s]              ∀ s  (if capped)

Solver: PuLP / CBC (bundled with PuLP, no system install required).
"""

from __future__ import annotations

from typing import Any

import pulp

from modules.optimization.solver_base import SolveResult


def solve(params: dict[str, Any]) -> SolveResult:
    """
    Run the sourcing-allocation MILP.

    Required keys in `params`:
        items:        list of {id, part_number, demand, unit_cost}
        suppliers:    list of {id, code, name, lead_time_days, reliability_pct,
                               capacity (may be None for ∞)}
        alpha:        reliability penalty weight (default 0.10)
        beta:         lead-time penalty weight   (default 0.05)
        fixed_cost_per_link: € per item-supplier relationship used (default 0)
        max_suppliers_per_item: int (default 2) — diversification cap

    Returns a SolveResult with artefacts:
        allocation:   list of {item, supplier, units, value, share_pct}
        summary:      totals (cost, demand met, suppliers used, items)
        sensitivity:  per-supplier load, per-item supplier-count
        kpi:          objective, single-source pct, hh-index
    """
    import time
    t0 = time.perf_counter()

    items     = params["items"]
    suppliers = params["suppliers"]
    if not items or not suppliers:
        return SolveResult(
            status="failed",
            objective_value=None,
            solver_used="CBC",
            runtime_ms=int((time.perf_counter() - t0) * 1000),
            error_message="No items or no active suppliers found for this company.",
        )

    alpha       = float(params.get("alpha", 0.10))
    beta        = float(params.get("beta",  0.05))
    fixed_cost  = float(params.get("fixed_cost_per_link", 0.0))
    max_per_item = int(params.get("max_suppliers_per_item", 2))

    item_ids  = [i["id"] for i in items]
    sup_ids   = [s["id"] for s in suppliers]
    item_map  = {i["id"]: i for i in items}
    sup_map   = {s["id"]: s for s in suppliers}

    # ── Effective cost matrix ────────────────────────────────────────────────
    eff_cost: dict[tuple[int, int], float] = {}
    for i_id in item_ids:
        unit = float(item_map[i_id]["unit_cost"] or 0.0)
        for s_id in sup_ids:
            rel   = float(sup_map[s_id]["reliability_pct"] or 100.0)
            lt    = float(sup_map[s_id]["lead_time_days"]  or 0.0)
            penalty = alpha * (100.0 - rel) / 100.0 + beta * lt / 30.0
            eff_cost[(i_id, s_id)] = max(unit, 0.01) * (1.0 + penalty)

    # ── LP model ─────────────────────────────────────────────────────────────
    prob = pulp.LpProblem("sourcing_allocation", pulp.LpMinimize)
    x = pulp.LpVariable.dicts(
        "x",
        ((i, s) for i in item_ids for s in sup_ids),
        lowBound=0,
        cat="Continuous",
    )
    y = pulp.LpVariable.dicts(
        "y",
        ((i, s) for i in item_ids for s in sup_ids),
        cat="Binary",
    )

    # Objective
    prob += (
        pulp.lpSum(eff_cost[(i, s)] * x[(i, s)] for i in item_ids for s in sup_ids)
        + fixed_cost * pulp.lpSum(y[(i, s)] for i in item_ids for s in sup_ids)
    ), "total_cost"

    # Demand satisfaction (equality)
    for i in item_ids:
        demand = float(item_map[i]["demand"])
        prob += (
            pulp.lpSum(x[(i, s)] for s in sup_ids) == demand,
            f"demand_{i}",
        )

    # Link x → y (big-M based on item demand)
    for i in item_ids:
        big_m = max(float(item_map[i]["demand"]), 1.0)
        for s in sup_ids:
            prob += x[(i, s)] <= big_m * y[(i, s)], f"link_{i}_{s}"

    # Diversification cap
    for i in item_ids:
        prob += (
            pulp.lpSum(y[(i, s)] for s in sup_ids) <= max_per_item,
            f"diversify_{i}",
        )

    # Supplier capacity (if any supplier has a capped value)
    for s in sup_ids:
        cap = sup_map[s].get("capacity")
        if cap is None:
            continue
        prob += (
            pulp.lpSum(x[(i, s)] for i in item_ids) <= float(cap),
            f"capacity_{s}",
        )

    # Solve silently
    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=30)
    pulp.LpStatus  # touch to ensure import
    status_code = prob.solve(solver)
    runtime_ms = int((time.perf_counter() - t0) * 1000)

    pulp_status = pulp.LpStatus[prob.status]
    if pulp_status not in ("Optimal", "Not Solved"):
        return SolveResult(
            status="infeasible" if pulp_status == "Infeasible" else "failed",
            objective_value=None,
            solver_used="CBC",
            runtime_ms=runtime_ms,
            error_message=f"PuLP status: {pulp_status}",
        )

    obj = pulp.value(prob.objective) or 0.0

    # ── Extract allocation ───────────────────────────────────────────────────
    allocation: list[dict[str, Any]] = []
    per_supplier_load: dict[int, float] = {s: 0.0 for s in sup_ids}
    per_item_supcount: dict[int, int]   = {i: 0 for i in item_ids}
    for i in item_ids:
        demand = float(item_map[i]["demand"])
        for s in sup_ids:
            units = pulp.value(x[(i, s)]) or 0.0
            if units < 1e-6:
                continue
            value = units * eff_cost[(i, s)]
            share = (units / demand * 100.0) if demand > 0 else 0.0
            allocation.append({
                "item_id":      i,
                "part_number":  item_map[i]["part_number"],
                "supplier_id":  s,
                "supplier_code": sup_map[s]["code"],
                "supplier_name": sup_map[s]["name"],
                "units":        round(units, 2),
                "value":        round(value, 2),
                "share_pct":    round(share, 1),
                "unit_cost":    round(eff_cost[(i, s)], 4),
            })
            per_supplier_load[s] += value
            per_item_supcount[i] += 1

    # ── Summary + KPI ────────────────────────────────────────────────────────
    total_demand = sum(float(it["demand"]) for it in items)
    total_value  = sum(a["value"] for a in allocation)
    suppliers_used = sum(1 for s in sup_ids if per_supplier_load[s] > 1e-6)
    items_solved = sum(
        1 for i in item_ids
        if abs(
            sum(a["units"] for a in allocation if a["item_id"] == i)
            - float(item_map[i]["demand"])
        ) < 1e-3
    )
    single_source = sum(1 for c in per_item_supcount.values() if c == 1)
    single_pct = (single_source / max(len(item_ids), 1)) * 100.0

    # Herfindahl-Hirschman index on supplier value share (concentration; 0..10000)
    hh = 0.0
    if total_value > 0:
        for s in sup_ids:
            share = per_supplier_load[s] / total_value * 100.0
            hh += share * share

    summary = {
        "total_items":        len(item_ids),
        "items_demand_met":   items_solved,
        "total_demand_units": round(total_demand, 1),
        "total_cost":         round(total_value, 2),
        "suppliers_used":     suppliers_used,
        "single_source_pct":  round(single_pct, 1),
        "hh_index":           round(hh, 1),
    }

    sensitivity = {
        "per_supplier_load": [
            {
                "supplier_id":   s,
                "supplier_code": sup_map[s]["code"],
                "supplier_name": sup_map[s]["name"],
                "value":         round(per_supplier_load[s], 2),
                "share_pct":     round(per_supplier_load[s] / total_value * 100.0, 1) if total_value > 0 else 0.0,
                "items_count":   sum(1 for a in allocation if a["supplier_id"] == s),
            }
            for s in sup_ids if per_supplier_load[s] > 1e-6
        ],
        "per_item_supplier_count": [
            {
                "item_id":     i,
                "part_number": item_map[i]["part_number"],
                "n_suppliers": per_item_supcount[i],
            }
            for i in item_ids
        ],
    }

    artefacts = [
        ("allocation",  {"rows": allocation}),
        ("summary",     summary),
        ("sensitivity", sensitivity),
        ("kpi", {
            "objective_value":   round(obj, 2),
            "single_source_pct": round(single_pct, 1),
            "hh_index":          round(hh, 1),
            "suppliers_used":    suppliers_used,
        }),
    ]

    return SolveResult(
        status="solved",
        objective_value=round(obj, 2),
        solver_used="CBC",
        runtime_ms=runtime_ms,
        artefacts=artefacts,
    )
