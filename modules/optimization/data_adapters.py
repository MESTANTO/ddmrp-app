"""
Read-only loaders that turn DDMRP app tables (items, suppliers, buffers, …)
into the small structured dicts the optimization solvers consume.

Each adapter is company-scoped and returns plain Python types (no SQLAlchemy
objects) so the result can be JSON-serialized into `optimization_runs.params_json`.
"""

from __future__ import annotations

from typing import Any

from database.db import (
    get_session, Item, Supplier,
    Location, Lane, LocationDemand,
)


def load_sourcing_inputs(
    company_id: int,
    *,
    horizon_days: int = 30,
    top_n_items: int | None = 25,
) -> dict[str, Any]:
    """
    Build inputs for the Session 1 sourcing-allocation MILP.

    Returns a dict with:
        items:     list of {part_number, description, demand, unit_cost}
        suppliers: list of {code, name, lead_time_days, reliability_pct, capacity}
        candidates: list of {item, supplier, unit_cost, effective_cost}
                    — Cartesian product items × active suppliers, since the
                    real-world purchase rarely restricts to default supplier.

    `top_n_items` keeps the LP tractable. The selection is by **dollar volume**
    (ADU × unit_cost × horizon), which is also where money is on the line.
    """
    sess = get_session()
    try:
        items_q = sess.query(Item).filter(Item.company_id == company_id).all()
        sup_q   = sess.query(Supplier).filter(
            Supplier.company_id == company_id,
            Supplier.status != "inactive",
        ).all()

        # Rank items by dollar volume
        scored: list[tuple[Item, float]] = []
        for it in items_q:
            adu  = float(it.adu or 0.0)
            cost = float(it.unit_cost or 0.0)
            scored.append((it, adu * cost * horizon_days))
        scored.sort(key=lambda x: x[1], reverse=True)
        if top_n_items:
            scored = scored[:top_n_items]
        items_kept = [it for it, _ in scored if (it.adu or 0) > 0]

        items_out: list[dict[str, Any]] = []
        for it in items_kept:
            items_out.append({
                "id":          int(it.id),
                "part_number": it.part_number,
                "description": it.description or "",
                "demand":      float(it.adu or 0.0) * horizon_days,
                "unit_cost":   float(it.unit_cost or 0.0),
                "default_supplier_id": (
                    int(it.default_supplier_id) if it.default_supplier_id else None
                ),
            })

        suppliers_out: list[dict[str, Any]] = []
        for s in sup_q:
            suppliers_out.append({
                "id":              int(s.id),
                "code":            s.code,
                "name":            s.name,
                "lead_time_days":  int(s.lead_time_days or 0),
                "reliability_pct": float(s.reliability_pct or 100.0),
                # Capacity = sum of item demands × 1.5 by default
                # (over-cap so feasibility is not artificially blocked).
                # A real user can override via the UI before solving.
                "capacity":        None,
                "status":          s.status or "active",
            })

        # Candidate matrix: every item can be sourced from every active supplier.
        # `effective_cost` blends unit cost + reliability penalty + lead-time
        # penalty. The penalties are *parametric* — the view exposes the
        # weights so the user can re-run with different trade-offs.
        return {
            "horizon_days": horizon_days,
            "top_n_items":  top_n_items,
            "items":        items_out,
            "suppliers":    suppliers_out,
        }
    finally:
        sess.close()


def load_network_inputs(
    company_id: int,
    *,
    horizon_days: int = 30,
    item_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    Build inputs for the Session 2 multi-echelon min-cost flow.

    Returns:
        horizon_days:  user-selected horizon
        locations:     [{id, code, name, node_type, throughput_capacity,
                         storage_capacity, holding_cost_per_unit_day}]
        lanes:         [{id, origin_id, dest_id, mode, cost_per_unit,
                         lead_time_days, capacity_units}]
        items:         [{id, part_number, unit_cost, aggregate_demand}]
        per_item_demand: dict[item_id, list[{location_id, demand}]] over horizon
                         — sourced from LocationDemand × horizon/30 if rows
                         exist; otherwise a single-virtual-customer fallback.

    Only `active` Lanes/Locations are returned.
    """
    sess = get_session()
    try:
        locs_q = (sess.query(Location)
                      .filter(Location.company_id == company_id,
                              Location.status == "active")
                      .all())
        lanes_q = (sess.query(Lane)
                       .filter(Lane.company_id == company_id,
                               Lane.status == "active")
                       .all())
        items_q = sess.query(Item).filter(Item.company_id == company_id)
        if item_ids:
            items_q = items_q.filter(Item.id.in_(item_ids))
        items_q = items_q.all()
        demand_q = (sess.query(LocationDemand)
                        .filter(LocationDemand.company_id == company_id)
                        .all())

        locations_out = [{
            "id":                        int(l.id),
            "code":                      l.code,
            "name":                      l.name,
            "node_type":                 l.node_type,
            "throughput_capacity":       float(l.throughput_capacity or 0.0),
            "storage_capacity":          float(l.storage_capacity or 0.0),
            "fixed_open_cost":           float(l.fixed_open_cost or 0.0),
            "holding_cost_per_unit_day": float(l.holding_cost_per_unit_day or 0.0),
        } for l in locs_q]

        lanes_out = [{
            "id":             int(ln.id),
            "origin_id":      int(ln.origin_id),
            "dest_id":        int(ln.dest_id),
            "mode":           ln.mode,
            "cost_per_unit":  float(ln.cost_per_unit or 0.0),
            "lead_time_days": int(ln.lead_time_days or 0),
            "capacity_units": float(ln.capacity_units or 0.0),
        } for ln in lanes_q]

        items_out: list[dict[str, Any]] = []
        per_item_demand: dict[int, list[dict[str, Any]]] = {}

        # Index per-item demand rows by item
        demand_by_item: dict[int, list[Any]] = {}
        for d in demand_q:
            demand_by_item.setdefault(int(d.item_id), []).append(d)

        # Default fallback: pick the *first* customer Location as the sink
        customer_locs = [l for l in locs_q if l.node_type == "customer"]
        fallback_sink = customer_locs[0].id if customer_locs else None

        scale = horizon_days / 30.0  # LocationDemand.monthly_demand → period

        for it in items_q:
            adu  = float(it.adu or 0.0)
            cost = float(it.unit_cost or 0.0)
            agg_demand = adu * horizon_days
            items_out.append({
                "id":               int(it.id),
                "part_number":      it.part_number,
                "description":      it.description or "",
                "unit_cost":        cost,
                "aggregate_demand": agg_demand,
            })

            rows = demand_by_item.get(int(it.id))
            if rows:
                per_item_demand[int(it.id)] = [
                    {"location_id": int(r.location_id),
                     "demand":      float(r.monthly_demand or 0.0) * scale}
                    for r in rows
                ]
            elif fallback_sink and agg_demand > 0:
                per_item_demand[int(it.id)] = [
                    {"location_id": int(fallback_sink),
                     "demand":      agg_demand}
                ]
            else:
                per_item_demand[int(it.id)] = []

        return {
            "horizon_days":    horizon_days,
            "locations":       locations_out,
            "lanes":           lanes_out,
            "items":           items_out,
            "per_item_demand": per_item_demand,
        }
    finally:
        sess.close()


def load_facility_inputs(
    company_id: int,
    *,
    horizon_days: int = 30,
) -> dict[str, Any]:
    """
    Build inputs for the Session 3 facility-location model (Ch7 UFLP/CFLP).

    Candidate facilities = active plant/dc Locations (each carries a
    `fixed_open_cost` and a per-period `capacity` derived from
    throughput_capacity × horizon). Customers = active customer Locations,
    each with aggregate demand over the horizon (summed across all items).

    Transport cost between a facility and a customer is NOT precomputed here;
    the solver derives it as the cheapest lane-path cost over `lanes`.

    Returns:
        horizon_days
        facilities: [{id, code, name, node_type, fixed_open_cost, capacity}]
                    capacity = throughput_capacity × horizon (0 = ∞)
        customers:  [{id, code, name, demand}]  (aggregate over horizon)
        lanes:      [{id, origin_id, dest_id, mode, cost_per_unit,
                      lead_time_days, capacity_units}]
    """
    sess = get_session()
    try:
        locs_q = (sess.query(Location)
                      .filter(Location.company_id == company_id,
                              Location.status == "active")
                      .all())
        lanes_q = (sess.query(Lane)
                       .filter(Lane.company_id == company_id,
                               Lane.status == "active")
                       .all())
        demand_q = (sess.query(LocationDemand)
                        .filter(LocationDemand.company_id == company_id)
                        .all())
        items_q = sess.query(Item).filter(Item.company_id == company_id).all()

        facilities_out = [{
            "id":              int(l.id),
            "code":            l.code,
            "name":            l.name,
            "node_type":       l.node_type,
            "fixed_open_cost": float(l.fixed_open_cost or 0.0),
            "capacity":        float(l.throughput_capacity or 0.0) * horizon_days,
        } for l in locs_q if l.node_type in ("plant", "dc")]

        customer_locs = [l for l in locs_q if l.node_type == "customer"]

        scale = horizon_days / 30.0  # monthly_demand → period
        demand_by_loc: dict[int, float] = {}
        for d in demand_q:
            demand_by_loc[int(d.location_id)] = (
                demand_by_loc.get(int(d.location_id), 0.0)
                + float(d.monthly_demand or 0.0) * scale
            )

        # Fallback when no LocationDemand rows exist: load the whole network's
        # aggregate demand (Σ adu × horizon) onto the first customer location,
        # mirroring the Session 2 single-virtual-customer fallback.
        if not demand_by_loc and customer_locs:
            total_demand = sum(float(it.adu or 0.0) * horizon_days for it in items_q)
            demand_by_loc[int(customer_locs[0].id)] = total_demand

        customers_out = [{
            "id":     int(l.id),
            "code":   l.code,
            "name":   l.name,
            "demand": round(demand_by_loc.get(int(l.id), 0.0), 4),
        } for l in customer_locs]

        lanes_out = [{
            "id":             int(ln.id),
            "origin_id":      int(ln.origin_id),
            "dest_id":        int(ln.dest_id),
            "mode":           ln.mode,
            "cost_per_unit":  float(ln.cost_per_unit or 0.0),
            "lead_time_days": int(ln.lead_time_days or 0),
            "capacity_units": float(ln.capacity_units or 0.0),
        } for ln in lanes_q]

        return {
            "horizon_days": horizon_days,
            "facilities":   facilities_out,
            "customers":    customers_out,
            "lanes":        lanes_out,
        }
    finally:
        sess.close()
