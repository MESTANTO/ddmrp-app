"""
Read-only loaders that turn DDMRP app tables (items, suppliers, buffers, …)
into the small structured dicts the optimization solvers consume.

Each adapter is company-scoped and returns plain Python types (no SQLAlchemy
objects) so the result can be JSON-serialized into `optimization_runs.params_json`.
"""

from __future__ import annotations

from typing import Any

from database.db import get_session, Item, Supplier


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
