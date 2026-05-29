"""
Session 3 — Facility Location (Ch7 UFLP / CFLP).

Decide WHICH candidate facilities (plant / dc Locations) to open, and how to
assign each customer's demand to open facilities, so that the total of
    fixed opening cost  +  demand-weighted transport cost
is minimised.

Two modes (chosen in the view):
  • UFLP  — uncapacitated: a facility, if open, can serve any volume.
  • CFLP  — capacitated: each open facility i is limited to capacity k_i
            (throughput_capacity × horizon).

Sourcing:
  • single_source = True  → each customer is served by exactly ONE facility
                            (x_ij binary). Classic UFLP/CFLP.
  • single_source = False → demand may be split across facilities
                            (x_ij continuous in [0,1]). Easier to feasibly
                            fill capacities.

Transport cost c_ij:
  The per-unit cost to serve customer j from facility i is the cheapest
  lane-path cost (sum of Lane.cost_per_unit along the path) over the directed
  lane graph. Unreachable (i, j) pairs simply get no assignment variable.

Solver: PuLP / CBC (MILP). Deterministic.
"""

from __future__ import annotations

import time
from typing import Any

import networkx as nx
import pulp

from modules.optimization.solver_base import SolveResult


def solve(params: dict[str, Any]) -> SolveResult:
    t0 = time.perf_counter()

    facilities = params["facilities"]
    customers  = params["customers"]
    lanes      = params["lanes"]
    horizon    = int(params.get("horizon_days", 30))

    respect_capacity = bool(params.get("respect_capacity", True))
    single_source    = bool(params.get("single_source", True))
    min_open         = params.get("min_open")          # int or None
    max_open         = params.get("max_open")          # int or None
    force_p          = params.get("force_p")           # int or None

    if not facilities:
        return _fail(t0, "No candidate facilities. Add plant/dc Locations in "
                         "Network Master before solving.")
    cust_with_demand = [c for c in customers if c["demand"] > 0]
    if not cust_with_demand:
        return _fail(t0, "No customer demand found. Add customer Locations and "
                         "per-location demand (or item ADU) before solving.")

    fac_by_id  = {f["id"]: f for f in facilities}
    cust_by_id = {c["id"]: c for c in cust_with_demand}
    fac_ids    = [f["id"] for f in facilities]
    cust_ids   = [c["id"] for c in cust_with_demand]

    # ── Cheapest per-unit transport cost c[(i, j)] via shortest lane path ───
    G = nx.DiGraph()
    for f in facilities:
        G.add_node(f["id"])
    for c in cust_with_demand:
        G.add_node(c["id"])
    for ln in lanes:
        # Keep the minimum cost if multiple lanes share an (origin, dest).
        o, d, w = ln["origin_id"], ln["dest_id"], ln["cost_per_unit"]
        if G.has_edge(o, d):
            if w < G[o][d]["weight"]:
                G[o][d]["weight"] = w
        else:
            G.add_edge(o, d, weight=w)

    cost: dict[tuple[int, int], float] = {}
    for i in fac_ids:
        if i not in G:
            continue
        try:
            lengths = nx.single_source_dijkstra_path_length(G, i, weight="weight")
        except nx.NetworkXError:
            lengths = {}
        for j in cust_ids:
            if j in lengths:
                cost[(i, j)] = float(lengths[j])

    # Customers reachable from at least one facility
    reachable = {j for (i, j) in cost}
    unreachable = [cust_by_id[j]["code"] for j in cust_ids if j not in reachable]
    if not reachable:
        return _fail(
            t0,
            "No lane path connects any candidate facility to any customer. "
            "Add lanes (Network Master) linking plant/dc nodes to customers.",
        )

    solve_cust_ids = [j for j in cust_ids if j in reachable]

    # ── Build the MILP ──────────────────────────────────────────────────────
    prob = pulp.LpProblem("FacilityLocation", pulp.LpMinimize)

    y = {i: pulp.LpVariable(f"open_{i}", cat="Binary") for i in fac_ids}
    cat = "Binary" if single_source else "Continuous"
    x = {
        (i, j): pulp.LpVariable(f"serve_{i}_{j}", lowBound=0, upBound=1, cat=cat)
        for (i, j) in cost if j in reachable
    }

    fixed = {i: fac_by_id[i]["fixed_open_cost"] for i in fac_ids}
    dem   = {j: cust_by_id[j]["demand"] for j in solve_cust_ids}

    # Objective: fixed opening cost + demand-weighted transport cost
    prob += (
        pulp.lpSum(fixed[i] * y[i] for i in fac_ids)
        + pulp.lpSum(dem[j] * cost[(i, j)] * x[(i, j)]
                     for (i, j) in x)
    ), "total_cost"

    # Each reachable customer's demand fully assigned
    for j in solve_cust_ids:
        serving = [x[(i, j)] for i in fac_ids if (i, j) in x]
        prob += pulp.lpSum(serving) == 1, f"demand_{j}"

    # Linking: cannot serve from a closed facility
    for (i, j) in x:
        prob += x[(i, j)] <= y[i], f"link_{i}_{j}"

    # Capacity (CFLP)
    if respect_capacity:
        for i in fac_ids:
            cap = fac_by_id[i]["capacity"]
            if cap and cap > 0:
                prob += (
                    pulp.lpSum(dem[j] * x[(i, j)]
                               for j in solve_cust_ids if (i, j) in x)
                    <= cap * y[i]
                ), f"cap_{i}"

    # Cardinality on number of open facilities
    if force_p:
        prob += pulp.lpSum(y[i] for i in fac_ids) == int(force_p), "force_p"
    else:
        if min_open:
            prob += pulp.lpSum(y[i] for i in fac_ids) >= int(min_open), "min_open"
        if max_open:
            prob += pulp.lpSum(y[i] for i in fac_ids) <= int(max_open), "max_open"

    # ── Solve ────────────────────────────────────────────────────────────────
    try:
        status = prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=60))
    except Exception as exc:  # solver binary issues, etc.
        return _fail(t0, f"Solver error: {type(exc).__name__}: {exc}")

    status_str = pulp.LpStatus[status]
    runtime_ms = int((time.perf_counter() - t0) * 1000)

    if status_str == "Infeasible":
        return SolveResult(
            status="infeasible",
            objective_value=None,
            solver_used="PULP_CBC",
            runtime_ms=runtime_ms,
            error_message=(
                "Infeasible — likely capacity too tight for total demand, or a "
                "facility-count constraint that cannot serve all customers. Try "
                "relaxing capacities, allowing demand splitting, or raising the "
                "max-open limit."
            ),
        )
    if status_str != "Optimal":
        return SolveResult(
            status="failed",
            objective_value=None,
            solver_used="PULP_CBC",
            runtime_ms=runtime_ms,
            error_message=f"Solver returned status: {status_str}.",
        )

    # ── Extract solution ─────────────────────────────────────────────────────
    open_ids = [i for i in fac_ids if y[i].value() and y[i].value() > 0.5]

    assignments: list[dict[str, Any]] = []
    fixed_total = sum(fixed[i] for i in open_ids)
    transport_total = 0.0
    served_by_fac: dict[int, float] = {}

    for (i, j), var in x.items():
        val = var.value() or 0.0
        if val <= 1e-6:
            continue
        d_served = dem[j] * val
        line_cost = d_served * cost[(i, j)]
        transport_total += line_cost
        served_by_fac[i] = served_by_fac.get(i, 0.0) + d_served
        assignments.append({
            "facility_id":   i,
            "facility_code": fac_by_id[i]["code"],
            "facility_name": fac_by_id[i]["name"],
            "customer_id":   j,
            "customer_code": cust_by_id[j]["code"],
            "customer_name": cust_by_id[j]["name"],
            "share_pct":     round(val * 100.0, 1),
            "demand_served": round(d_served, 1),
            "unit_cost":     round(cost[(i, j)], 4),
            "line_cost":     round(line_cost, 2),
        })

    assignments.sort(key=lambda r: r["line_cost"], reverse=True)
    total_cost = round(fixed_total + transport_total, 2)

    # Facility rows (open + closed) with utilization
    facility_rows: list[dict[str, Any]] = []
    for f in facilities:
        i = f["id"]
        is_open = i in open_ids
        served = served_by_fac.get(i, 0.0)
        cap = f["capacity"]
        util = (served / cap * 100.0) if (cap and cap > 0) else None
        facility_rows.append({
            "facility_id":   i,
            "code":          f["code"],
            "name":          f["name"],
            "node_type":     f["node_type"],
            "is_open":       is_open,
            "fixed_cost":    round(f["fixed_open_cost"], 2),
            "demand_served": round(served, 1),
            "capacity":      round(cap, 1) if cap else None,
            "util_pct":      round(util, 1) if util is not None else None,
            "customers":     sum(1 for a in assignments if a["facility_id"] == i),
        })
    facility_rows.sort(key=lambda r: (not r["is_open"], -r["demand_served"]))

    total_demand = sum(dem.values())

    summary = {
        "total_cost":       total_cost,
        "fixed_cost":       round(fixed_total, 2),
        "transport_cost":   round(transport_total, 2),
        "facilities_open":  len(open_ids),
        "facilities_total": len(facilities),
        "customers_served": len(solve_cust_ids),
        "customers_total":  len(cust_with_demand),
        "total_demand":     round(total_demand, 1),
        "horizon_days":     horizon,
        "mode":             "CFLP" if respect_capacity else "UFLP",
        "single_source":    single_source,
    }

    # Cost share per open facility (for a bar chart)
    cost_by_fac = []
    for i in open_ids:
        f_transport = sum(a["line_cost"] for a in assignments
                          if a["facility_id"] == i)
        cost_by_fac.append({
            "code":           fac_by_id[i]["code"],
            "fixed_cost":     round(fixed[i], 2),
            "transport_cost": round(f_transport, 2),
            "total_cost":     round(fixed[i] + f_transport, 2),
        })
    cost_by_fac.sort(key=lambda r: r["total_cost"], reverse=True)

    # Sankey facility → customer
    open_index = {i: idx for idx, i in enumerate(open_ids)}
    sankey_labels = [f"{fac_by_id[i]['code']} · {fac_by_id[i]['node_type']}"
                     for i in open_ids]
    cust_index = {}
    for j in solve_cust_ids:
        if any(a["customer_id"] == j for a in assignments):
            cust_index[j] = len(sankey_labels)
            sankey_labels.append(f"{cust_by_id[j]['code']} · customer")
    sankey_links = [{
        "source": open_index[a["facility_id"]],
        "target": cust_index[a["customer_id"]],
        "value":  a["demand_served"],
        "label":  f"{a['demand_served']:,.0f} u · €{a['line_cost']:,.0f}",
    } for a in assignments
      if a["facility_id"] in open_index and a["customer_id"] in cust_index]

    # Sensitivity: bottleneck (CFLP) + closed facilities + costly customers
    bottlenecks = [r for r in facility_rows
                   if r["util_pct"] is not None and r["util_pct"] >= 90.0]
    closed = [r for r in facility_rows if not r["is_open"]]
    cust_cost = {}
    for a in assignments:
        cust_cost[a["customer_code"]] = cust_cost.get(a["customer_code"], 0.0) + a["line_cost"]
    costly_customers = sorted(
        ({"customer_code": k, "transport_cost": round(v, 2)}
         for k, v in cust_cost.items()),
        key=lambda r: r["transport_cost"], reverse=True,
    )[:10]

    artefacts = [
        ("summary",    summary),
        ("allocation", {"assignments": assignments}),
        ("chart_data", {
            "facility_rows": facility_rows,
            "cost_by_fac":   cost_by_fac,
            "sankey_nodes":  sankey_labels,
            "sankey_links":  sankey_links,
            "unreachable_customers": unreachable,
        }),
        ("sensitivity", {
            "bottlenecks":      bottlenecks,
            "closed":           closed,
            "costly_customers": costly_customers,
        }),
        ("kpi", {
            "objective_value": total_cost,
            "facilities_open": len(open_ids),
            "fixed_cost":      round(fixed_total, 2),
            "transport_cost":  round(transport_total, 2),
        }),
    ]

    return SolveResult(
        status="solved",
        objective_value=total_cost,
        solver_used="PULP_CBC",
        runtime_ms=runtime_ms,
        artefacts=artefacts,
    )


def _fail(t0: float, msg: str) -> SolveResult:
    return SolveResult(
        status="failed",
        objective_value=None,
        solver_used="PULP_CBC",
        runtime_ms=int((time.perf_counter() - t0) * 1000),
        error_message=msg,
    )
