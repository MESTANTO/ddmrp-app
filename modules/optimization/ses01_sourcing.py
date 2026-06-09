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

    Optional:
        candidates:   list of {item_id, supplier_id, unit_cost, lead_time_days,
                               min_order_qty} from the Supplier-Part Matrix.
                      When non-empty, sourcing is RESTRICTED to these pairs and
                      the per-link unit_cost / lead_time are used (overriding the
                      item unit_cost and supplier lead-time). Items with no
                      candidate link are reported as `unsourced` and excluded
                      from the demand constraint. When omitted/empty, every item
                      may be sourced from every supplier (Cartesian, legacy).

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
    # Soft bias toward preferred links: shave `preferred_discount` off the
    # *objective* coefficient of preferred pairs (reporting still uses the true
    # effective cost), so a preferred supplier wins when costs are close.
    prefer_preferred   = bool(params.get("prefer_preferred", False))
    preferred_discount = float(params.get("preferred_discount", 0.03))

    item_ids  = [i["id"] for i in items]
    sup_ids   = [s["id"] for s in suppliers]
    item_map  = {i["id"]: i for i in items}
    sup_map   = {s["id"]: s for s in suppliers}

    # ── Sourcing candidate pairs ─────────────────────────────────────────────
    # If the Supplier-Part Matrix defined explicit links, restrict sourcing to
    # those pairs and use the per-link unit_cost / lead_time. Otherwise fall
    # back to the Cartesian items × suppliers product (legacy behaviour).
    candidates = params.get("candidates") or []
    restricted = bool(candidates)

    # pair → (unit_cost, lead_time_days, min_order_qty)
    pair_terms: dict[tuple[int, int], tuple[float, float, float]] = {}
    pair_pref:  dict[tuple[int, int], bool] = {}
    if restricted:
        for c in candidates:
            i_id = int(c["item_id"])
            s_id = int(c["supplier_id"])
            if i_id not in item_map or s_id not in sup_map:
                continue
            pair_terms[(i_id, s_id)] = (
                float(c.get("unit_cost") or 0.0),
                float(c.get("lead_time_days") or 0.0),
                float(c.get("min_order_qty") or 0.0),
            )
            pair_pref[(i_id, s_id)] = bool(c.get("is_preferred"))
    else:
        for i_id in item_ids:
            unit = float(item_map[i_id]["unit_cost"] or 0.0)
            for s_id in sup_ids:
                lt = float(sup_map[s_id]["lead_time_days"] or 0.0)
                pair_terms[(i_id, s_id)] = (unit, lt, 0.0)
                pair_pref[(i_id, s_id)] = False

    pairs = list(pair_terms.keys())
    suppliers_for_item: dict[int, list[int]] = {i: [] for i in item_ids}
    for (i_id, s_id) in pairs:
        suppliers_for_item[i_id].append(s_id)

    sourceable_items = [i for i in item_ids if suppliers_for_item[i]]
    unsourced_items  = [i for i in item_ids if not suppliers_for_item[i]]

    if not sourceable_items:
        return SolveResult(
            status="failed",
            objective_value=None,
            solver_used="CBC",
            runtime_ms=int((time.perf_counter() - t0) * 1000),
            error_message=(
                "No sourceable items: the Supplier-Part Matrix has no links for "
                "any of the selected items. Add links in Master Data → "
                "Supplier-Part Matrix, or clear the matrix to allow all suppliers."
            ),
        )

    # ── Effective cost per pair (reliability from supplier; lead-time per link)
    # eff_cost = the TRUE effective cost used for value reporting.
    # obj_cost = the coefficient the solver minimises; identical to eff_cost
    #            unless the user enabled the preferred-bias, which shaves a small
    #            discount off preferred pairs so they win on near-ties.
    eff_cost: dict[tuple[int, int], float] = {}
    obj_cost: dict[tuple[int, int], float] = {}
    for (i_id, s_id), (unit, lt, _moq) in pair_terms.items():
        rel = float(sup_map[s_id]["reliability_pct"] or 100.0)
        penalty = alpha * (100.0 - rel) / 100.0 + beta * lt / 30.0
        ec = max(unit, 0.01) * (1.0 + penalty)
        eff_cost[(i_id, s_id)] = ec
        if prefer_preferred and pair_pref[(i_id, s_id)]:
            obj_cost[(i_id, s_id)] = ec * (1.0 - preferred_discount)
        else:
            obj_cost[(i_id, s_id)] = ec

    # ── LP model ─────────────────────────────────────────────────────────────
    prob = pulp.LpProblem("sourcing_allocation", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("x", pairs, lowBound=0, cat="Continuous")
    y = pulp.LpVariable.dicts("y", pairs, cat="Binary")

    # Objective
    prob += (
        pulp.lpSum(obj_cost[p] * x[p] for p in pairs)
        + fixed_cost * pulp.lpSum(y[p] for p in pairs)
    ), "total_cost"

    # Demand satisfaction (equality) — only for sourceable items
    for i in sourceable_items:
        demand = float(item_map[i]["demand"])
        prob += (
            pulp.lpSum(x[(i, s)] for s in suppliers_for_item[i]) == demand,
            f"demand_{i}",
        )

    # Link x → y (big-M based on item demand) + per-link MOQ lower bound
    for (i, s), (_u, _lt, moq) in pair_terms.items():
        demand = float(item_map[i]["demand"])
        big_m = max(demand, 1.0)
        prob += x[(i, s)] <= big_m * y[(i, s)], f"link_{i}_{s}"
        # Minimum order quantity: if this link is used, ship at least the MOQ
        # (capped at demand so it can never make the problem infeasible).
        moq_eff = min(moq, demand)
        if moq_eff > 0:
            prob += x[(i, s)] >= moq_eff * y[(i, s)], f"moq_{i}_{s}"

    # Diversification cap
    for i in sourceable_items:
        prob += (
            pulp.lpSum(y[(i, s)] for s in suppliers_for_item[i]) <= max_per_item,
            f"diversify_{i}",
        )

    # Supplier capacity (if any supplier has a capped value)
    items_for_supplier: dict[int, list[int]] = {s: [] for s in sup_ids}
    for (i_id, s_id) in pairs:
        items_for_supplier[s_id].append(i_id)
    for s in sup_ids:
        cap = sup_map[s].get("capacity")
        if cap is None:
            continue
        prob += (
            pulp.lpSum(x[(i, s)] for i in items_for_supplier[s]) <= float(cap),
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
    for i in sourceable_items:
        demand = float(item_map[i]["demand"])
        for s in suppliers_for_item[i]:
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
                "is_preferred": bool(pair_pref.get((i, s), False)),
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
    single_source = sum(1 for i in sourceable_items if per_item_supcount[i] == 1)
    single_pct = (single_source / max(len(sourceable_items), 1)) * 100.0

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
        "sourcing_mode":      "matrix" if restricted else "cartesian",
        "unsourced_items":    len(unsourced_items),
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
        "unsourced": [
            {
                "item_id":     i,
                "part_number": item_map[i]["part_number"],
                "demand":      round(float(item_map[i]["demand"]), 2),
            }
            for i in unsourced_items
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
