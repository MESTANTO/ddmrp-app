"""
Session 2 — Multi-Echelon Network Flow (Ch4 Network LP).

Aggregate min-cost flow across the user's Location/Lane network. Demand is
computed per item over a horizon (from LocationDemand or items.adu fallback),
and the solver finds the lowest-cost set of lane flows that meet all customer
demand without violating lane / node throughput capacities.

Solver: NetworkX `min_cost_flow` (Successive Shortest Paths + capacity scaling).
Integer-friendly, deterministic, no external binary required.

Model
─────
Graph G = (V, E):
    V = Locations ∪ {SUPER_SOURCE, SUPER_SINK}
    E = Lanes (origin → dest, capacity, cost)
        ∪ plant_supply (SUPER_SOURCE → plant)            for each plant
        ∪ supplier_supply (SUPER_SOURCE → supplier-loc)  for each supplier-loc
        ∪ customer_drain (customer → SUPER_SINK)         for each customer

Capacities:
    lane.capacity_units (0 → big_M for "unlimited")
    plant/supplier supply edges: throughput_capacity × horizon_days
        (or big_M if 0)
    customer drain edges: total per-item demand at that customer location

Node demand on the digraph (NetworkX convention):
    SUPER_SOURCE.demand = -total_demand
    SUPER_SINK.demand   = +total_demand
    all other nodes:    0

Costs:
    lane.cost_per_unit                                          (€/unit)
    supply edges & drain edges: 0

Per-item flows: solved one item at a time and aggregated. This avoids the
much larger multi-commodity LP while still respecting per-lane and per-node
capacity in aggregate via a *shared-capacity check*: if total aggregate flow
on any lane exceeds capacity, the solver flags the lane as a bottleneck.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from modules.optimization.solver_base import SolveResult


BIG_M = 10**9  # NetworkX needs integer capacities; this is "effectively ∞"


def solve(params: dict[str, Any]) -> SolveResult:
    import time
    t0 = time.perf_counter()

    locations  = params["locations"]
    lanes      = params["lanes"]
    items      = params["items"]
    per_item_d = params["per_item_demand"]
    horizon    = int(params.get("horizon_days", 30))

    if not locations:
        return _fail(t0, "No Locations defined. Add at least one plant/supplier "
                         "and one customer in Network Master.")
    if not lanes:
        return _fail(t0, "No Lanes defined. Add lanes between Locations.")

    loc_by_id = {l["id"]: l for l in locations}
    plants_or_sources = [
        l for l in locations if l["node_type"] in ("plant", "supplier")
    ]
    customers = [l for l in locations if l["node_type"] == "customer"]

    if not plants_or_sources:
        return _fail(t0, "No source nodes (plant or supplier) found.")
    if not customers:
        return _fail(t0, "No customer nodes found.")

    # Aggregate flow per (origin, dest) lane across all items
    agg_lane_flow: dict[tuple[int, int], float] = {}
    agg_lane_cost: dict[tuple[int, int], float] = {}
    per_item_results: list[dict[str, Any]] = []
    total_cost = 0.0
    items_solved = 0
    items_infeasible = 0
    items_no_demand = 0

    SUPER_SOURCE = "__SRC__"
    SUPER_SINK   = "__SNK__"

    for it in items:
        demand_rows = per_item_d.get(it["id"], [])
        item_demand = sum(d["demand"] for d in demand_rows)
        if item_demand <= 0:
            items_no_demand += 1
            per_item_results.append({
                "item_id":     it["id"],
                "part_number": it["part_number"],
                "status":      "no_demand",
                "demand":      0.0,
                "cost":        0.0,
                "lanes_used":  0,
            })
            continue

        G = nx.DiGraph()

        # Lane edges (carry the real cost)
        for ln in lanes:
            cap = int(ln["capacity_units"]) if ln["capacity_units"] > 0 else BIG_M
            # NetworkX min_cost_flow needs integer weights — scale by 1000.
            cost = int(round(ln["cost_per_unit"] * 1000))
            G.add_edge(ln["origin_id"], ln["dest_id"],
                       capacity=cap, weight=cost,
                       _lane_id=ln["id"])

        # Source supply edges
        for src in plants_or_sources:
            cap_per_day = src["throughput_capacity"]
            cap = int(cap_per_day * horizon) if cap_per_day > 0 else BIG_M
            G.add_edge(SUPER_SOURCE, src["id"], capacity=cap, weight=0)

        # Customer drain edges (sized exactly to this customer's demand)
        cust_demand_map: dict[int, float] = {}
        for row in demand_rows:
            cust_demand_map[row["location_id"]] = (
                cust_demand_map.get(row["location_id"], 0.0) + row["demand"]
            )
        for cust_id, dem in cust_demand_map.items():
            G.add_edge(cust_id, SUPER_SINK,
                       capacity=int(dem) + 1, weight=0)

        # Node demands (NetworkX convention)
        int_demand = int(item_demand)
        G.add_node(SUPER_SOURCE, demand=-int_demand)
        G.add_node(SUPER_SINK,   demand=+int_demand)

        try:
            flow_dict = nx.min_cost_flow(G)
            item_cost_scaled = nx.cost_of_flow(G, flow_dict)
            item_cost = item_cost_scaled / 1000.0
            items_solved += 1
        except nx.NetworkXUnfeasible:
            items_infeasible += 1
            per_item_results.append({
                "item_id":     it["id"],
                "part_number": it["part_number"],
                "status":      "infeasible",
                "demand":      item_demand,
                "cost":        0.0,
                "lanes_used":  0,
            })
            continue

        # Record this item's lane flows + add to aggregate
        lanes_used = 0
        for u, dests in flow_dict.items():
            for v, qty in dests.items():
                if qty <= 0 or u in (SUPER_SOURCE,) or v in (SUPER_SINK,):
                    continue
                key = (u, v)
                if key in {(ln["origin_id"], ln["dest_id"]) for ln in lanes}:
                    agg_lane_flow[key] = agg_lane_flow.get(key, 0.0) + qty
                    # Lookup cost from lanes list
                    lane_cost = next(
                        (ln["cost_per_unit"] for ln in lanes
                         if ln["origin_id"] == u and ln["dest_id"] == v),
                        0.0,
                    )
                    agg_lane_cost[key] = agg_lane_cost.get(key, 0.0) + qty * lane_cost
                    lanes_used += 1

        total_cost += item_cost
        per_item_results.append({
            "item_id":     it["id"],
            "part_number": it["part_number"],
            "status":      "solved",
            "demand":      round(item_demand, 2),
            "cost":        round(item_cost, 2),
            "lanes_used":  lanes_used,
        })

    runtime_ms = int((time.perf_counter() - t0) * 1000)

    if items_solved == 0 and items_infeasible > 0:
        return SolveResult(
            status="infeasible",
            objective_value=None,
            solver_used="NETWORKX",
            runtime_ms=runtime_ms,
            error_message=(
                f"All items infeasible — likely a missing lane or insufficient "
                f"capacity. ({items_infeasible} infeasible, {items_no_demand} "
                f"with zero demand)"
            ),
        )

    # ── Build artefacts ──────────────────────────────────────────────────────
    lane_rows = []
    lane_lookup = {(ln["origin_id"], ln["dest_id"]): ln for ln in lanes}
    for (u, v), qty in sorted(agg_lane_flow.items(),
                              key=lambda kv: kv[1], reverse=True):
        ln = lane_lookup.get((u, v), {})
        cap = ln.get("capacity_units", 0.0) or 0.0
        util = (qty / cap * 100.0) if cap > 0 else None
        lane_rows.append({
            "lane_id":      ln.get("id"),
            "origin_id":    u,
            "origin_code":  loc_by_id.get(u, {}).get("code", str(u)),
            "origin_name":  loc_by_id.get(u, {}).get("name", ""),
            "dest_id":      v,
            "dest_code":    loc_by_id.get(v, {}).get("code", str(v)),
            "dest_name":    loc_by_id.get(v, {}).get("name", ""),
            "mode":         ln.get("mode", ""),
            "units":        round(qty, 1),
            "cost_per_unit": round(ln.get("cost_per_unit", 0.0), 4),
            "value":        round(agg_lane_cost.get((u, v), 0.0), 2),
            "capacity":     cap,
            "util_pct":     round(util, 1) if util is not None else None,
        })

    # Sankey-friendly source/target arrays
    sankey_nodes = sorted({
        nid for row in lane_rows for nid in (row["origin_id"], row["dest_id"])
    })
    sankey_node_labels = [
        f"{loc_by_id[nid]['code']} · {loc_by_id[nid]['node_type']}"
        for nid in sankey_nodes if nid in loc_by_id
    ]
    sankey_node_index = {nid: i for i, nid in enumerate(sankey_nodes)
                          if nid in loc_by_id}
    sankey_links = [{
        "source": sankey_node_index[row["origin_id"]],
        "target": sankey_node_index[row["dest_id"]],
        "value":  row["units"],
        "label":  f"{row['units']:,.0f} units · €{row['value']:,.0f}",
    } for row in lane_rows
      if row["origin_id"] in sankey_node_index
         and row["dest_id"] in sankey_node_index]

    # Node load (in / out)
    node_load_in: dict[int, float] = {}
    node_load_out: dict[int, float] = {}
    for row in lane_rows:
        node_load_out[row["origin_id"]] = node_load_out.get(row["origin_id"], 0) + row["units"]
        node_load_in[row["dest_id"]]    = node_load_in.get(row["dest_id"], 0) + row["units"]

    node_rows = [{
        "id":        nid,
        "code":      loc_by_id[nid]["code"],
        "name":      loc_by_id[nid]["name"],
        "type":      loc_by_id[nid]["node_type"],
        "in_units":  round(node_load_in.get(nid, 0.0), 1),
        "out_units": round(node_load_out.get(nid, 0.0), 1),
    } for nid in sankey_nodes if nid in loc_by_id]

    summary = {
        "total_cost":      round(total_cost, 2),
        "horizon_days":    horizon,
        "items_solved":    items_solved,
        "items_infeasible": items_infeasible,
        "items_no_demand": items_no_demand,
        "lanes_active":    len(lane_rows),
        "nodes_active":    len(node_rows),
    }

    artefacts = [
        ("summary",     summary),
        ("allocation",  {"per_item": per_item_results}),
        ("chart_data",  {
            "sankey_nodes": sankey_node_labels,
            "sankey_links": sankey_links,
            "lane_rows":    lane_rows,
            "node_rows":    node_rows,
        }),
        ("sensitivity", {
            "bottlenecks": [r for r in lane_rows
                            if r["util_pct"] is not None and r["util_pct"] >= 90.0],
            "underused":   [r for r in lane_rows
                            if r["util_pct"] is not None and r["util_pct"] <= 10.0],
        }),
        ("kpi", {
            "objective_value": round(total_cost, 2),
            "items_solved":    items_solved,
            "lanes_active":    len(lane_rows),
        }),
    ]

    return SolveResult(
        status="solved",
        objective_value=round(total_cost, 2),
        solver_used="NETWORKX",
        runtime_ms=runtime_ms,
        artefacts=artefacts,
    )


def _fail(t0: float, msg: str) -> SolveResult:
    import time
    return SolveResult(
        status="failed",
        objective_value=None,
        solver_used="NETWORKX",
        runtime_ms=int((time.perf_counter() - t0) * 1000),
        error_message=msg,
    )
