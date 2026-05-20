"""
Apply / reject layer for the AI Inventory Manager write tools.

The chat agent inserts pending `AgentAction` rows via the `propose_*` tools
in `agents/chat_tools.py`. The Pending Changes tab in
`views/ai_inventory_manager.py` calls `apply_action(action_id)` or
`reject_action(action_id, reason)` from this module when the user clicks
Approve / Reject.

Each handler:
  • Re-validates company_id against the target row (multi-tenant safety).
  • Re-applies its field allowlist (defense in depth).
  • Runs sanity ranges on numeric fields.
  • Mirrors the same write path the UI uses.
  • Stores an `after_json` snapshot once the write succeeds.

Handlers never raise; all errors are captured into the AgentAction row
with status='failed' and the exception text in `notes`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from database.db import (
    AgentAction,
    BufferAdjustment,
    Item,
    Supplier,
    SessionLocal,
)


# ---------------------------------------------------------------------------
# Field allowlists — agent may ONLY set these keys
# ---------------------------------------------------------------------------

ITEM_FIELDS = {
    "part_number", "description", "category", "unit_of_measure", "item_type",
    "buffer_profile_id", "spike_horizon_days", "spike_threshold_factor",
    "adu", "dlt", "lead_time_factor", "variability_factor", "min_order_qty",
    "order_cycle", "on_hand", "unit_cost", "ordering_cost", "holding_cost_pct",
    "default_supplier_id", "customer_tolerance_time", "market_potential_lt",
    "order_visibility_horizon", "demand_variability_score",
    "supply_variability_score", "inventory_leverage_score",
    "critical_operation", "mrp_type_override",
}

SUPPLIER_FIELDS = {
    "code", "name", "country", "city", "address", "website", "phone", "email",
    "material_contact_name", "material_contact_email", "material_contact_phone",
    "procurement_contact_name", "procurement_contact_email", "procurement_contact_phone",
    "manager_contact_name", "manager_contact_email", "manager_contact_phone",
    "lead_time_days", "reliability_pct", "payment_terms", "currency",
    "incoterms", "status", "certifications", "notes",
}

BUFFER_ADJ_FIELDS = {
    "item_id", "start_date", "end_date",
    "daf", "ltaf", "red_zaf", "yellow_zaf", "green_zaf", "note",
}

# Sanity ranges enforced at apply time
_RANGES = {
    "adu":               (0,    None),
    "dlt":               (0,    None),
    "lead_time_factor":  (0.0,  1.0),
    "variability_factor":(0.0,  1.0),
    "holding_cost_pct":  (0.0,  1.0),
    "reliability_pct":   (0.0,  100.0),
    "min_order_qty":     (0,    None),
    "order_cycle":       (0,    None),
    "unit_cost":         (0,    None),
    "ordering_cost":     (0,    None),
    "on_hand":           (0,    None),
    "lead_time_days":    (0,    None),
    "daf":               (0.0,  None),
    "ltaf":              (0.0,  None),
    "red_zaf":           (0.0,  None),
    "yellow_zaf":        (0.0,  None),
    "green_zaf":         (0.0,  None),
    "spike_horizon_days":(0,    None),
    "spike_threshold_factor":(0.0, None),
}


def _validate_ranges(fields: dict) -> None:
    """Raise ValueError if any field is outside its sanity range."""
    for k, v in fields.items():
        if k not in _RANGES or v is None:
            continue
        lo, hi = _RANGES[k]
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if lo is not None and fv < lo:
            raise ValueError(f"{k}={v} below allowed minimum {lo}")
        if hi is not None and fv > hi:
            raise ValueError(f"{k}={v} above allowed maximum {hi}")


def _filter_allowed(fields: dict, allowed: set) -> dict:
    """Keep only keys present in `allowed`; drop unknown keys silently."""
    if not isinstance(fields, dict):
        return {}
    return {k: v for k, v in fields.items() if k in allowed}


def _parse_date(v):
    """Accept date / datetime / 'YYYY-MM-DD' strings."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
    # date object
    try:
        return datetime.combine(v, datetime.min.time())
    except Exception:
        return None


def _snapshot_item(it: Item) -> dict:
    return {f: getattr(it, f, None) for f in ITEM_FIELDS} | {"id": it.id}


def _snapshot_supplier(s: Supplier) -> dict:
    return {f: getattr(s, f, None) for f in SUPPLIER_FIELDS} | {"id": s.id}


def _snapshot_buffer_adj(a: BufferAdjustment) -> dict:
    out = {f: getattr(a, f, None) for f in BUFFER_ADJ_FIELDS}
    out["id"] = a.id
    # JSON-friendly dates
    for k in ("start_date", "end_date"):
        if isinstance(out.get(k), datetime):
            out[k] = out[k].isoformat()
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_update_item(action: AgentAction, payload: dict, session) -> dict:
    it = session.query(Item).get(action.target_id)
    if it is None:
        raise ValueError(f"Item id={action.target_id} not found")
    if it.company_id != action.company_id:
        raise PermissionError("Cross-company write blocked")

    fields = _filter_allowed(payload.get("fields") or {}, ITEM_FIELDS)
    _validate_ranges(fields)
    for k, v in fields.items():
        setattr(it, k, v)
    session.commit()
    return _snapshot_item(it)


def _handle_create_item(action: AgentAction, payload: dict, session) -> dict:
    fields = _filter_allowed(payload.get("fields") or {}, ITEM_FIELDS)
    # part_number is required and can also come at the top level
    pn = payload.get("part_number") or fields.get("part_number")
    if not pn:
        raise ValueError("part_number is required")
    fields["part_number"] = str(pn).strip().upper()
    fields.setdefault("description", fields["part_number"])
    _validate_ranges(fields)

    # Check uniqueness within company
    existing = session.query(Item).filter(
        Item.part_number == fields["part_number"],
        Item.company_id == action.company_id,
    ).first()
    if existing:
        raise ValueError(f"Item {fields['part_number']} already exists")

    it = Item(company_id=action.company_id, **fields)
    session.add(it)
    session.commit()
    action.target_id = it.id
    session.commit()
    return _snapshot_item(it)


def _handle_delete_item(action: AgentAction, payload: dict, session) -> dict:
    it = session.query(Item).get(action.target_id)
    if it is None:
        raise ValueError(f"Item id={action.target_id} not found")
    if it.company_id != action.company_id:
        raise PermissionError("Cross-company write blocked")

    # Cascade protection — refuse if FK-referencing rows exist
    from database.db import BomLine, DemandEntry, SupplyEntry
    bom_count = session.query(BomLine).filter(
        (BomLine.parent_item_id == it.id) | (BomLine.child_item_id == it.id)
    ).count()
    if bom_count:
        raise ValueError(f"Delete blocked: {bom_count} BOM lines reference this item")
    dem_count = session.query(DemandEntry).filter_by(item_id=it.id).count()
    sup_count = session.query(SupplyEntry).filter_by(item_id=it.id).count()
    if dem_count or sup_count:
        raise ValueError(
            f"Delete blocked: {dem_count} demand and {sup_count} supply entries reference this item"
        )

    snap = _snapshot_item(it)
    session.delete(it)
    session.commit()
    return snap


def _handle_update_supplier(action: AgentAction, payload: dict, session) -> dict:
    s = session.query(Supplier).get(action.target_id)
    if s is None:
        raise ValueError(f"Supplier id={action.target_id} not found")
    if s.company_id != action.company_id:
        raise PermissionError("Cross-company write blocked")

    fields = _filter_allowed(payload.get("fields") or {}, SUPPLIER_FIELDS)
    _validate_ranges(fields)
    for k, v in fields.items():
        setattr(s, k, v)
    s.updated_at = datetime.utcnow()
    session.commit()
    return _snapshot_supplier(s)


def _handle_create_supplier(action: AgentAction, payload: dict, session) -> dict:
    fields = _filter_allowed(payload.get("fields") or {}, SUPPLIER_FIELDS)
    code = payload.get("code") or fields.get("code")
    if not code:
        raise ValueError("code is required")
    fields["code"] = str(code).strip()
    fields.setdefault("name", fields["code"])
    _validate_ranges(fields)

    existing = session.query(Supplier).filter(
        Supplier.code == fields["code"],
        Supplier.company_id == action.company_id,
    ).first()
    if existing:
        raise ValueError(f"Supplier {fields['code']} already exists")

    s = Supplier(company_id=action.company_id, **fields)
    session.add(s)
    session.commit()
    action.target_id = s.id
    session.commit()
    return _snapshot_supplier(s)


def _handle_delete_supplier(action: AgentAction, payload: dict, session) -> dict:
    s = session.query(Supplier).get(action.target_id)
    if s is None:
        raise ValueError(f"Supplier id={action.target_id} not found")
    if s.company_id != action.company_id:
        raise PermissionError("Cross-company write blocked")

    linked = session.query(Item).filter_by(default_supplier_id=s.id).count()
    if linked:
        raise ValueError(f"Delete blocked: {linked} items reference this supplier as default")

    snap = _snapshot_supplier(s)
    session.delete(s)
    session.commit()
    return snap


def _handle_create_buffer_adjustment(action: AgentAction, payload: dict, session) -> dict:
    fields = _filter_allowed(payload, BUFFER_ADJ_FIELDS)
    item_id = fields.get("item_id")
    if not item_id:
        raise ValueError("item_id is required")
    item = session.query(Item).get(int(item_id))
    if item is None:
        raise ValueError(f"Item id={item_id} not found")
    if item.company_id != action.company_id:
        raise PermissionError("Cross-company write blocked")

    start = _parse_date(fields.get("start_date"))
    if start is None:
        raise ValueError("start_date is required")
    end = _parse_date(fields.get("end_date"))

    # Default factors to 1.0 if not provided
    adj_kwargs = {
        "item_id":    int(item_id),
        "start_date": start,
        "end_date":   end,
        "daf":        float(fields.get("daf", 1.0) or 1.0),
        "ltaf":       float(fields.get("ltaf", 1.0) or 1.0),
        "red_zaf":    float(fields.get("red_zaf", 1.0) or 1.0),
        "yellow_zaf": float(fields.get("yellow_zaf", 1.0) or 1.0),
        "green_zaf":  float(fields.get("green_zaf", 1.0) or 1.0),
        "note":       (fields.get("note") or "").strip(),
    }
    _validate_ranges(adj_kwargs)
    if end is not None and end < start:
        raise ValueError("end_date is before start_date")

    adj = BufferAdjustment(**adj_kwargs)
    session.add(adj)
    session.commit()
    action.target_id = adj.id
    session.commit()
    return _snapshot_buffer_adj(adj)


def _handle_delete_buffer_adjustment(action: AgentAction, payload: dict, session) -> dict:
    adj = session.query(BufferAdjustment).get(action.target_id)
    if adj is None:
        raise ValueError(f"BufferAdjustment id={action.target_id} not found")
    # The adjustment doesn't have a company_id directly — check via the item
    item = session.query(Item).get(adj.item_id)
    if item is None or item.company_id != action.company_id:
        raise PermissionError("Cross-company write blocked")

    snap = _snapshot_buffer_adj(adj)
    session.delete(adj)
    session.commit()
    return snap


_HANDLERS: dict[str, Callable[[AgentAction, dict, Any], dict]] = {
    "update_item":              _handle_update_item,
    "create_item":              _handle_create_item,
    "delete_item":              _handle_delete_item,
    "update_supplier":          _handle_update_supplier,
    "create_supplier":          _handle_create_supplier,
    "delete_supplier":          _handle_delete_supplier,
    "create_buffer_adjustment": _handle_create_buffer_adjustment,
    "delete_buffer_adjustment": _handle_delete_buffer_adjustment,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_action(action_id: int) -> dict:
    """
    Run the handler for the given pending action.

    Returns {ok: bool, error?: str, after?: dict, already_applied?: bool}.
    """
    session = SessionLocal()
    try:
        action = session.query(AgentAction).get(action_id)
        if action is None:
            return {"ok": False, "error": f"action id={action_id} not found"}
        if action.status != "pending":
            return {"ok": True, "already_applied": True, "status": action.status}

        handler = _HANDLERS.get(action.action_type)
        if handler is None:
            action.status = "failed"
            action.notes = f"Unknown action_type: {action.action_type}"
            session.commit()
            return {"ok": False, "error": action.notes}

        try:
            payload = json.loads(action.payload_json or "{}")
        except Exception as exc:
            action.status = "failed"
            action.notes = f"Bad payload JSON: {exc}"
            session.commit()
            return {"ok": False, "error": action.notes}

        try:
            after = handler(action, payload, session)
        except Exception as exc:
            session.rollback()
            # Re-fetch the action after rollback to update it cleanly
            action = session.query(AgentAction).get(action_id)
            if action is not None:
                action.status = "failed"
                action.notes = f"{type(exc).__name__}: {exc}"
                session.commit()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # Mark success
        action.status = "applied"
        action.applied_at = datetime.utcnow()
        try:
            action.after_json = json.dumps(after, default=str, ensure_ascii=False)
        except Exception:
            action.after_json = str(after)
        session.commit()

        # Invalidate Streamlit page caches so the rest of the app sees the change
        _invalidate_caches()

        return {"ok": True, "after": after}
    finally:
        session.close()


def reject_action(action_id: int, reason: str = "") -> dict:
    """Mark a pending action as rejected. Returns {ok: bool, error?: str}."""
    session = SessionLocal()
    try:
        action = session.query(AgentAction).get(action_id)
        if action is None:
            return {"ok": False, "error": f"action id={action_id} not found"}
        if action.status != "pending":
            return {"ok": True, "already_handled": True, "status": action.status}
        action.status = "rejected"
        action.notes = (reason or "").strip()
        session.commit()
        return {"ok": True}
    finally:
        session.close()


def _invalidate_caches() -> None:
    """Clear page-level @st.cache_data caches so changes show up immediately."""
    # Wrapped in try/except so a missing cached loader never blocks the apply.
    try:
        from views.dashboard import _load_dashboard_data  # type: ignore
        _load_dashboard_data.clear()
    except Exception:
        pass
    try:
        from views.alarms import _load_state_cached  # type: ignore
        _load_state_cached.clear()
    except Exception:
        pass
    try:
        from views.model_velocity import _compute_model_velocity_cached  # type: ignore
        _compute_model_velocity_cached.clear()
    except Exception:
        pass
