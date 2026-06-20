"""
Read-only loaders that turn DDMRP app tables (items, suppliers, buffers, …)
into the small structured dicts the optimization solvers consume.

Each adapter is company-scoped and returns plain Python types (no SQLAlchemy
objects) so the result can be JSON-serialized into `optimization_runs.params_json`.
"""

from __future__ import annotations

import math
from typing import Any

from datetime import datetime, timedelta

from database.db import (
    get_session, Item, Supplier, DemandEntry,
    Location, Lane, LocationDemand, ItemSupplier,
)


def _num(value: Any, default: float = 0.0) -> float:
    """
    Coerce a master-data value to a finite float, falling back to `default`.

    Unlike `float(x or default)`, this also catches NaN — which is *truthy*, so
    `nan or 0.5` would otherwise leak the NaN through. Master-data factors like
    lead_time_factor / variability_factor are sometimes stored as NaN; left
    unguarded they poison the DDMRP zone formulas (red_base = adu·dlt·ltf).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


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
        candidates: list of {item_id, supplier_id, unit_cost, lead_time_days,
                             min_order_qty, is_preferred} — the explicit
                    supplier↔part links from the Supplier-Part Matrix
                    (`item_suppliers`), restricted to kept items × active
                    suppliers. Per-link unit_cost / lead_time inherit from the
                    Item / Supplier master when left at 0.
        has_links: True if the company defined ANY supplier-part links. When
                   False, `candidates` is empty and the solver falls back to
                   the Cartesian items × suppliers product (legacy behaviour).

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

        # Candidate links from the Supplier-Part Matrix (item_suppliers).
        # When the company has defined explicit links, the solver restricts
        # sourcing to those pairs and inherits per-link unit_cost / lead_time
        # (0 → fall back to Item.unit_cost / Supplier.lead_time_days). When no
        # links exist at all, `candidates` stays empty and the solver uses the
        # Cartesian items × active-suppliers product (legacy behaviour).
        active_sup_ids = {s["id"] for s in suppliers_out}
        kept_item_map  = {int(it.id): it for it in items_kept}

        links_q = (sess.query(ItemSupplier)
                       .filter(ItemSupplier.company_id == company_id,
                               ItemSupplier.status != "inactive")
                       .all())
        has_links = (sess.query(ItemSupplier)
                         .filter(ItemSupplier.company_id == company_id)
                         .first() is not None)

        candidates: list[dict[str, Any]] = []
        for ln in links_q:
            i_id = int(ln.item_id)
            s_id = int(ln.supplier_id)
            if i_id not in kept_item_map or s_id not in active_sup_ids:
                continue
            it  = kept_item_map[i_id]
            sup = next(s for s in sup_q if int(s.id) == s_id)
            link_cost = float(ln.unit_cost or 0.0)
            link_lt   = int(ln.lead_time_days or 0)
            link_moq  = float(ln.min_order_qty or 0.0)
            candidates.append({
                "item_id":        i_id,
                "supplier_id":    s_id,
                "unit_cost":      link_cost if link_cost > 0 else float(it.unit_cost or 0.0),
                "lead_time_days": link_lt   if link_lt  > 0 else int(sup.lead_time_days or 0),
                "min_order_qty":  link_moq  if link_moq > 0 else float(it.min_order_qty or 0.0),
                "is_preferred":   bool(ln.is_preferred),
            })

        return {
            "horizon_days": horizon_days,
            "top_n_items":  top_n_items,
            "items":        items_out,
            "suppliers":    suppliers_out,
            "candidates":   candidates,
            "has_links":    has_links,
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


def load_simulation_inputs(
    company_id: int,
    *,
    lookback_days: int = 90,
    only_ddmrp_eligible: bool = False,
    top_n_items: int | None = 50,
) -> dict[str, Any]:
    """
    Build inputs for the DDMRP-vs-MRP methodology simulation.

    For every (or every DDMRP-eligible) item we pull the full set of master-data
    parameters the inventory policies need — nothing is duplicated, all values
    come from the existing `items` table — plus a historical daily-demand series
    (used both for the bootstrap demand mode and to derive a coefficient of
    variation that shapes the synthetic demand mode).

    DDMRP eligibility comes from `positioning_engine.analyze_all_items`
    (recommended_type=="DDMRP" or a manual mrp_type_override=="DDMRP").

    Returns:
        lookback_days
        items: list of per-item dicts with master parameters + demand stats:
            id, part_number, description, item_type,
            adu, dlt, lead_time_factor, variability_factor,
            min_order_qty, order_cycle, on_hand,
            unit_cost, ordering_cost, holding_cost_pct,
            supplier_lead_time_days,
            is_ddmrp_eligible, positioning_score,
            hist_mean, hist_cv, hist_series (daily, len=lookback_days)
    """
    from modules.positioning_engine import analyze_all_items

    sess = get_session()
    try:
        items_q = (sess.query(Item)
                       .filter(Item.company_id == company_id)
                       .order_by(Item.part_number)
                       .all())

        # Default supplier lead time per item (fallback to item.dlt when unset).
        sup_lt: dict[int, int] = {}
        sup_rows = (sess.query(Supplier)
                        .filter(Supplier.company_id == company_id).all())
        sup_lt_by_id = {int(s.id): int(s.lead_time_days or 0) for s in sup_rows}

        # Historical actual-demand series per item over the lookback window.
        today = datetime.utcnow().date()
        start = today - timedelta(days=lookback_days)
        dem_rows = (sess.query(DemandEntry)
                        .filter(DemandEntry.demand_type == "actual",
                                DemandEntry.demand_date >= datetime.combine(start, datetime.min.time()),
                                DemandEntry.demand_date <= datetime.combine(today, datetime.max.time()))
                        .all())
        per_item_day: dict[int, dict] = {}
        for e in dem_rows:
            d = e.demand_date.date()
            per_item_day.setdefault(int(e.item_id), {})
            per_item_day[int(e.item_id)][d] = (
                per_item_day[int(e.item_id)].get(d, 0.0) + float(e.quantity or 0.0)
            )

        # Positioning / eligibility (company-scoped).
        try:
            positioning = {p.item_id: p for p in analyze_all_items(company_id)}
        except Exception:
            positioning = {}

        def _series(item_id: int) -> list[float]:
            day_map = per_item_day.get(item_id, {})
            out = []
            for off in range(lookback_days):
                d = start + timedelta(days=off)
                out.append(round(day_map.get(d, 0.0), 4))
            return out

        items_out: list[dict[str, Any]] = []
        for it in items_q:
            series = _series(int(it.id))
            n = len(series) or 1
            hist_mean = sum(series) / n
            if hist_mean > 0 and len(series) > 1:
                var = sum((x - hist_mean) ** 2 for x in series) / len(series)
                hist_cv = (var ** 0.5) / hist_mean
            else:
                hist_cv = 0.0

            pos = positioning.get(int(it.id))
            eligible = bool(pos and pos.effective_type == "DDMRP")
            score = int(pos.total_score) if pos else 0

            sup_id = int(it.default_supplier_id) if it.default_supplier_id else None
            supplier_lt = sup_lt_by_id.get(sup_id, 0) if sup_id else 0

            items_out.append({
                "id":                      int(it.id),
                "part_number":             it.part_number,
                "description":             it.description or "",
                "item_type":               it.item_type or "P",
                "adu":                     _num(it.adu, 0.0),
                "dlt":                     _num(it.dlt, 0.0),
                "lead_time_factor":        _num(it.lead_time_factor, 0.5),
                "variability_factor":      _num(it.variability_factor, 0.5),
                "min_order_qty":           _num(it.min_order_qty, 0.0),
                "order_cycle":             _num(it.order_cycle, 0.0),
                "on_hand":                 _num(it.on_hand, 0.0),
                "unit_cost":               _num(it.unit_cost, 0.0),
                "ordering_cost":           _num(it.ordering_cost, 0.0),
                "holding_cost_pct":        _num(it.holding_cost_pct, 0.0),
                "supplier_lead_time_days": int(supplier_lt),
                "is_ddmrp_eligible":       eligible,
                "positioning_score":       score,
                "hist_mean":               round(hist_mean, 4),
                "hist_cv":                 round(hist_cv, 4),
                "hist_series":             series,
                # ── Current SAP planning policy (AS-IS baseline) ──
                "sap_mrp_type":              (getattr(it, "sap_mrp_type", "") or "").upper(),
                "sap_safety_stock":          _num(getattr(it, "sap_safety_stock", 0.0), 0.0),
                "sap_reorder_point":         _num(getattr(it, "sap_reorder_point", 0.0), 0.0),
                "sap_fixed_lot":             _num(getattr(it, "sap_fixed_lot", 0.0), 0.0),
                "sap_min_lot":               _num(getattr(it, "sap_min_lot", 0.0), 0.0),
                "sap_max_lot":               _num(getattr(it, "sap_max_lot", 0.0), 0.0),
                "sap_rounding_value":        _num(getattr(it, "sap_rounding_value", 0.0), 0.0),
                "sap_planned_delivery_time": _num(getattr(it, "sap_planned_delivery_time", 0.0), 0.0),
                "sap_gr_processing_time":    _num(getattr(it, "sap_gr_processing_time", 0.0), 0.0),
            })

        if only_ddmrp_eligible:
            items_out = [d for d in items_out if d["is_ddmrp_eligible"]]

        # Keep items with positive demand potential, ranked by dollar volume.
        items_out = [d for d in items_out if d["adu"] > 0 or d["hist_mean"] > 0]
        items_out.sort(
            key=lambda d: max(d["adu"], d["hist_mean"]) * max(d["unit_cost"], 0.0),
            reverse=True,
        )
        if top_n_items:
            items_out = items_out[:top_n_items]

        return {
            "lookback_days": lookback_days,
            "items":         items_out,
        }
    finally:
        sess.close()
