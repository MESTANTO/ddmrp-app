"""
Tool catalogue for the AI Inventory Manager chat agent.

Exposes Python callables + an OpenAI-style JSON schema so a tool-capable
LLM can:
  • run one of the 8 focused skills (`run_skill`)
  • query specific data slices (overstock, gaps, supplier risk, etc.)
  • compute headline inventory-value metrics
  • render a chart inline in the chat thread

All data tools are company-scoped: caller passes `company_id` once and
each invocation re-derives the data fresh, so the agent always sees the
current DB state.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from agents import inventory_agent
from agents.action_applier import (
    BUFFER_ADJ_FIELDS,
    ITEM_FIELDS,
    SUPPLIER_FIELDS,
)
from agents.inventory_agent import (
    ANALYSIS_CATEGORIES,
    build_focused_context,
    call_nvidia_nim_focused,
    collect_raw_data,
    get_abc_xyz_classification,
    get_data_quality_issues,
    get_demand_trends,
    get_inventory_snapshot,
    get_inventory_value_summary,
    get_low_nfp_items,
    get_overstock_items,
    get_safety_stock_gaps,
    get_supplier_risk_items,
    parse_llm_signals,
)
from database.db import (
    AgentAction,
    BufferAdjustment,
    Item,
    SessionLocal,
    Supplier,
)


# ---------------------------------------------------------------------------
# Write-tool helpers
# ---------------------------------------------------------------------------

# Cap proposals per turn to prevent runaway tool loops
_MAX_PROPOSALS_PER_TURN = 10
_propose_counter: dict[int, int] = {}   # company_id -> count this turn


def reset_propose_counter(company_id: int) -> None:
    """Call at the start of every chat turn so the rate-limit resets."""
    _propose_counter[company_id] = 0


def _check_rate_limit(company_id: int) -> dict | None:
    """Return an error dict if the per-turn cap is exhausted, else None."""
    n = _propose_counter.get(company_id, 0)
    if n >= _MAX_PROPOSALS_PER_TURN:
        return {
            "error": "too many proposals in one turn",
            "hint":  f"Cap is {_MAX_PROPOSALS_PER_TURN}. Ask the user to approve "
                     "the queued changes before proposing more.",
        }
    _propose_counter[company_id] = n + 1
    return None


def _allowlist(fields: dict | None, allowed: set) -> dict:
    if not isinstance(fields, dict):
        return {}
    return {k: v for k, v in fields.items() if k in allowed}


def _snapshot_dict(obj, fields: set) -> dict:
    return {f: getattr(obj, f, None) for f in fields}


def _queue(action: AgentAction, session) -> dict:
    session.add(action)
    session.commit()
    return {
        "queued":      True,
        "action_id":   action.id,
        "action_type": action.action_type,
        "summary":     _summarise(action),
    }


def _summarise(action: AgentAction) -> str:
    try:
        p = json.loads(action.payload_json or "{}")
    except Exception:
        p = {}
    if action.action_type == "update_item":
        return f"Update item id={action.target_id} fields={list((p.get('fields') or {}).keys())}"
    if action.action_type == "create_item":
        return f"Create item part_number={p.get('part_number')}"
    if action.action_type == "delete_item":
        return f"Delete item id={action.target_id}"
    if action.action_type == "update_supplier":
        return f"Update supplier id={action.target_id} fields={list((p.get('fields') or {}).keys())}"
    if action.action_type == "create_supplier":
        return f"Create supplier code={p.get('code')}"
    if action.action_type == "delete_supplier":
        return f"Delete supplier id={action.target_id}"
    if action.action_type == "create_buffer_adjustment":
        return (f"Buffer adjustment item_id={p.get('item_id')} "
                f"daf={p.get('daf', 1.0)} ltaf={p.get('ltaf', 1.0)} "
                f"({p.get('start_date')} -> {p.get('end_date')})")
    if action.action_type == "delete_buffer_adjustment":
        return f"Delete buffer adjustment id={action.target_id}"
    return action.action_type


# ---------------------------------------------------------------------------
# OpenAI tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "inventory_value_summary",
            "description": (
                "Compute headline € figures for the company's inventory: on-hand value, "
                "annual usage value, average inventory target, excess value above TOG, "
                "gap value below TOR, breakdown by ABC class and by execution colour, "
                "and the top 10 items by on-hand value. Use this for any question about "
                "where cash is tied up or total inventory value."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_overstock",
            "description": (
                "List items where on_hand > TOG (excess inventory), sorted by excess "
                "value descending. Each row includes excess_units, excess_value (€), "
                "and on_hand_vs_tog ratio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 100,
                              "description": "Maximum rows to return (default 20)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_safety_gaps",
            "description": (
                "List items where on_hand < TOR (safety stock gap). Includes gap_units, "
                "gap_value (€), and days_until_stockout, sorted by stockout risk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_low_nfp",
            "description": (
                "Items with Net Flow Position below Top of Yellow — active replenishment "
                "signals. Includes nfp, top_of_yellow, top_of_green, and NFP as % of TOR."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_supplier_risk",
            "description": (
                "Items whose default supplier has reliability < 90%. Includes supplier "
                "name, reliability_pct, supplier_lt_days, and the lead-time gap vs DLT."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_demand_trends",
            "description": (
                "90-day demand trends: per-item CV, recent ADU vs stored ADU divergence, "
                "and trend direction (up/down/flat). Filter to items with notable signals."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_data_quality",
            "description": (
                "Items with missing or inconsistent master data (zero unit_cost, zero "
                "ADU with buffer, zero DLT with buffer, inconsistent buffer zones, "
                "missing supplier for purchased item)."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abc_xyz_matrix",
            "description": (
                "ABC/XYZ classification: 9-cell counts (A-X through C-Z) plus € annual "
                "value per cell. Use to recommend differentiated policies."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": (
                "Run one of the 8 focused inventory analyses (a single LLM-backed "
                "skill) and return its parsed signals. Skills: 1=data_quality, "
                "2=buffer_nfp, 3=abc_xyz, 4=demand_variability, 5=safety_stock, "
                "6=overstock, 7=supplier_risk, 8=value_reduction. Use when the user "
                "asks for a full analysis of a specific area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "integer", "minimum": 1, "maximum": 8,
                                 "description": "Skill number 1–8."},
                },
                "required": ["skill_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_value_reduction",
            "description": (
                "The headline tool: runs skill_08 (inventory value reduction) and "
                "returns prioritized cash-release levers ranked by € impact × confidence. "
                "Optionally targets a € amount to free up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_eur": {"type": "number",
                                   "description": "Optional € target to free."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_chart",
            "description": (
                "Render a chart inline in the chat thread. Use after retrieving data "
                "to make a point visual. Supports bar, pie, scatter, and line charts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind":  {"type": "string",
                              "enum": ["bar", "pie", "scatter", "line"]},
                    "title": {"type": "string"},
                    "x":     {"type": "array", "items": {"type": ["string", "number"]},
                              "description": "X-axis values (categories for bar/pie)."},
                    "y":     {"type": "array", "items": {"type": "number"},
                              "description": "Y-axis values."},
                    "x_label": {"type": "string"},
                    "y_label": {"type": "string"},
                },
                "required": ["kind", "title", "x", "y"],
            },
        },
    },
    # -----------------------------------------------------------------------
    # WRITE tools — every propose_* queues a change for human approval.
    # No data changes until the user clicks Approve on the Pending Changes tab.
    # -----------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "propose_update_item",
            "description": (
                "Queue an update to an item's master data / DDMRP parameters. "
                "The change is NOT applied immediately — it goes to a Pending "
                "Changes queue for the user to Approve/Reject. Pass only "
                "fields that should change; unknown keys are dropped. "
                "Always state your reasoning in `reason`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer",
                                "description": "Internal id of the item to update."},
                    "fields":  {"type": "object",
                                "description": "Dict of field → new value. "
                                               "Allowed: adu, dlt, lead_time_factor, "
                                               "variability_factor, min_order_qty, "
                                               "order_cycle, on_hand, unit_cost, "
                                               "buffer_profile_id, default_supplier_id, "
                                               "description, category, item_type, etc."},
                    "reason":  {"type": "string",
                                "description": "Short explanation of why this change is proposed."},
                },
                "required": ["item_id", "fields", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_create_item",
            "description": (
                "Queue creation of a new item. Goes to the Pending Changes queue. "
                "Provide `part_number` and any other allowed fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part_number": {"type": "string"},
                    "fields":      {"type": "object",
                                    "description": "Allowed item fields (adu, dlt, "
                                                   "description, category, …)."},
                    "reason":      {"type": "string"},
                },
                "required": ["part_number", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete_item",
            "description": (
                "Queue deletion of an item. The applier will refuse if BOM lines, "
                "demand entries, or supply entries reference the item."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "reason":  {"type": "string"},
                },
                "required": ["item_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_create_buffer_adjustment",
            "description": (
                "Queue a new time-bounded buffer adjustment (DAF/LTAF/ZAF) for an "
                "item. A factor of 1.0 = neutral. end_date may be null (open-ended)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":    {"type": "integer"},
                    "start_date": {"type": "string",
                                   "description": "YYYY-MM-DD"},
                    "end_date":   {"type": ["string", "null"],
                                   "description": "YYYY-MM-DD or null for open-ended"},
                    "daf":        {"type": "number", "description": "Demand Adj Factor × ADU (default 1.0)"},
                    "ltaf":       {"type": "number", "description": "Lead-Time Adj Factor × DLT (default 1.0)"},
                    "red_zaf":    {"type": "number"},
                    "yellow_zaf": {"type": "number"},
                    "green_zaf":  {"type": "number"},
                    "note":       {"type": "string"},
                    "reason":     {"type": "string"},
                },
                "required": ["item_id", "start_date", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete_buffer_adjustment",
            "description": "Queue deletion of an existing buffer adjustment by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "adjustment_id": {"type": "integer"},
                    "reason":        {"type": "string"},
                },
                "required": ["adjustment_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_update_supplier",
            "description": (
                "Queue an update to a supplier's master data (reliability, lead "
                "time, contacts, payment terms, …)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "supplier_id": {"type": "integer"},
                    "fields":      {"type": "object"},
                    "reason":      {"type": "string"},
                },
                "required": ["supplier_id", "fields", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_create_supplier",
            "description": "Queue creation of a new supplier. `code` is mandatory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code":   {"type": "string"},
                    "fields": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["code", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_delete_supplier",
            "description": (
                "Queue deletion of a supplier. Refused if any item references it "
                "as its default supplier."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "supplier_id": {"type": "integer"},
                    "reason":      {"type": "string"},
                },
                "required": ["supplier_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pending_actions",
            "description": (
                "List currently pending write proposals for the company (queue state)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

# Skill id → category key (drives `run_skill`)
_SKILL_ID_TO_KEY = {idx + 1: cat["key"] for idx, cat in enumerate(ANALYSIS_CATEGORIES)}


def _t_inventory_value_summary(company_id: int, **_) -> dict:
    return get_inventory_value_summary(company_id)


def _t_list_overstock(company_id: int, top_n: int = 20, **_) -> dict:
    rows = get_overstock_items(company_id)
    return {"count": len(rows), "items": rows[:max(1, int(top_n))]}


def _t_list_safety_gaps(company_id: int, top_n: int = 20, **_) -> dict:
    rows = get_safety_stock_gaps(company_id)
    return {"count": len(rows), "items": rows[:max(1, int(top_n))]}


def _t_list_low_nfp(company_id: int, top_n: int = 20, **_) -> dict:
    rows = get_low_nfp_items(company_id)
    return {"count": len(rows), "items": rows[:max(1, int(top_n))]}


def _t_list_supplier_risk(company_id: int, top_n: int = 20, **_) -> dict:
    rows = get_supplier_risk_items(company_id)
    return {"count": len(rows), "items": rows[:max(1, int(top_n))]}


def _t_list_demand_trends(company_id: int, top_n: int = 20, **_) -> dict:
    trends   = get_demand_trends(company_id)
    snap_map = {r["id"]: r["part_number"] for r in get_inventory_snapshot(company_id)}
    notable  = [
        {"part_number": snap_map.get(iid, str(iid)), **t}
        for iid, t in trends.items()
        if t["total_entries"] >= 5 and (t["adu_divergence_pct"] > 20 or t["cv"] > 0.8)
    ]
    notable.sort(key=lambda x: x["adu_divergence_pct"], reverse=True)
    return {"count": len(notable), "items": notable[:max(1, int(top_n))]}


def _t_list_data_quality(company_id: int, **_) -> dict:
    rows = get_data_quality_issues(company_id)
    return {"count": len(rows), "items": rows}


def _t_abc_xyz_matrix(company_id: int, **_) -> dict:
    from collections import Counter, defaultdict as _dd
    abc = get_abc_xyz_classification(company_id)
    counts: Counter = Counter()
    values: dict    = _dd(float)
    for r in abc:
        counts[r["acvs"]] += 1
        values[r["acvs"]] += r.get("annual_value", 0)
    cells = ["A-X", "A-Y", "A-Z", "B-X", "B-Y", "B-Z", "C-X", "C-Y", "C-Z"]
    return {
        "cells": [
            {"cell": c, "count": counts.get(c, 0),
             "annual_value": round(values.get(c, 0), 2)}
            for c in cells
        ],
        "total_items": sum(counts.values()),
        "total_annual_value": round(sum(values.values()), 2),
    }


def _t_run_skill(company_id: int, skill_id: int, model: str, api_key: str, **_) -> dict:
    """
    Invoke a single skill: build focused context, call NVIDIA NIM, parse signals.
    Does NOT persist to AgentRun/AgentSignal — chat invocations are ephemeral.
    """
    if skill_id not in _SKILL_ID_TO_KEY:
        return {"error": f"Unknown skill_id={skill_id}. Valid: 1–8."}

    cat_key = _SKILL_ID_TO_KEY[skill_id]
    raw     = collect_raw_data(company_id)
    ctx     = build_focused_context(cat_key, raw)
    text, status = call_nvidia_nim_focused(
        category_key=cat_key, focused_context=ctx,
        model=model, api_key=api_key,
    )
    if status != "ok":
        return {"error": text}

    item_id_map = {r["part_number"]: r["id"] for r in raw.get("snapshot", [])}
    signals = parse_llm_signals(
        raw_response=text, run_id=0, company_id=company_id,
        item_id_by_part_num=item_id_map, analysis_category=cat_key,
    )

    serialised = []
    for s in signals:
        serialised.append({
            "signal_type":      s.signal_type,
            "severity":         s.severity,
            "part_number":      s.part_number,
            "title":            s.title,
            "detail":           s.detail,
            "recommendation":   s.recommendation,
            "metric_name":      s.metric_name,
            "metric_value":     s.metric_value,
            "metric_threshold": s.metric_threshold,
        })
    label = next((c["label"] for c in ANALYSIS_CATEGORIES if c["key"] == cat_key), cat_key)
    return {"skill": label, "skill_key": cat_key,
            "count": len(serialised), "signals": serialised}


def _t_propose_value_reduction(company_id: int, model: str, api_key: str,
                               target_eur: float | None = None, **_) -> dict:
    """Headline tool — runs skill_08 and returns ranked cash-release levers."""
    result = _t_run_skill(company_id=company_id, skill_id=8,
                          model=model, api_key=api_key)
    if "error" in result:
        return result
    if target_eur:
        result["target_eur"] = float(target_eur)
    return result


def _t_render_chart(company_id: int, **kwargs) -> dict:
    """No-op on the backend — the view picks up the chart spec from tool_calls."""
    return {"ok": True, "rendered": True, "spec": kwargs}


# ---------------------------------------------------------------------------
# WRITE tools — queue pending AgentAction rows for human approval
# ---------------------------------------------------------------------------

def _t_propose_update_item(company_id: int, user_id: int | None = None,
                           item_id: int = 0, fields: dict | None = None,
                           reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        it = session.query(Item).get(int(item_id))
        if it is None:
            return {"error": f"Item id={item_id} not found"}
        if it.company_id != company_id:
            return {"error": "Cross-company access blocked"}
        cleaned = _allowlist(fields, ITEM_FIELDS)
        if not cleaned:
            return {"error": "No valid fields to update",
                    "hint": f"Allowed: {sorted(ITEM_FIELDS)}"}
        before = _snapshot_dict(it, ITEM_FIELDS) | {"id": it.id, "part_number": it.part_number}
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="update_item", target_table="items", target_id=it.id,
            payload_json=json.dumps({"fields": cleaned}, default=str),
            before_json=json.dumps(before, default=str),
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_create_item(company_id: int, user_id: int | None = None,
                           part_number: str = "", fields: dict | None = None,
                           reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not part_number:
        return {"error": "part_number is required"}
    cleaned = _allowlist(fields, ITEM_FIELDS)
    cleaned["part_number"] = str(part_number).strip().upper()
    session = SessionLocal()
    try:
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="create_item", target_table="items", target_id=None,
            payload_json=json.dumps({"part_number": cleaned["part_number"],
                                     "fields": cleaned}, default=str),
            before_json=None,
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_delete_item(company_id: int, user_id: int | None = None,
                           item_id: int = 0, reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        it = session.query(Item).get(int(item_id))
        if it is None:
            return {"error": f"Item id={item_id} not found"}
        if it.company_id != company_id:
            return {"error": "Cross-company access blocked"}
        before = _snapshot_dict(it, ITEM_FIELDS) | {"id": it.id, "part_number": it.part_number}
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="delete_item", target_table="items", target_id=it.id,
            payload_json=json.dumps({}, default=str),
            before_json=json.dumps(before, default=str),
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_create_buffer_adjustment(company_id: int, user_id: int | None = None,
                                        item_id: int = 0,
                                        start_date: str = "", end_date: str | None = None,
                                        daf: float = 1.0, ltaf: float = 1.0,
                                        red_zaf: float = 1.0, yellow_zaf: float = 1.0,
                                        green_zaf: float = 1.0, note: str = "",
                                        reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not item_id or not start_date:
        return {"error": "item_id and start_date are required"}
    session = SessionLocal()
    try:
        it = session.query(Item).get(int(item_id))
        if it is None:
            return {"error": f"Item id={item_id} not found"}
        if it.company_id != company_id:
            return {"error": "Cross-company access blocked"}
        payload = {
            "item_id":    int(item_id),
            "start_date": start_date,
            "end_date":   end_date,
            "daf":        float(daf),
            "ltaf":       float(ltaf),
            "red_zaf":    float(red_zaf),
            "yellow_zaf": float(yellow_zaf),
            "green_zaf":  float(green_zaf),
            "note":       str(note or "").strip(),
        }
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="create_buffer_adjustment",
            target_table="buffer_adjustments", target_id=None,
            payload_json=json.dumps(payload, default=str),
            before_json=None,
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_delete_buffer_adjustment(company_id: int, user_id: int | None = None,
                                        adjustment_id: int = 0, reason: str = "",
                                        **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not adjustment_id:
        return {"error": "adjustment_id is required"}
    session = SessionLocal()
    try:
        adj = session.query(BufferAdjustment).get(int(adjustment_id))
        if adj is None:
            return {"error": f"BufferAdjustment id={adjustment_id} not found"}
        it = session.query(Item).get(adj.item_id)
        if it is None or it.company_id != company_id:
            return {"error": "Cross-company access blocked"}
        before = _snapshot_dict(adj, BUFFER_ADJ_FIELDS) | {"id": adj.id}
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="delete_buffer_adjustment",
            target_table="buffer_adjustments", target_id=adj.id,
            payload_json=json.dumps({}, default=str),
            before_json=json.dumps(before, default=str),
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_update_supplier(company_id: int, user_id: int | None = None,
                               supplier_id: int = 0, fields: dict | None = None,
                               reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not supplier_id:
        return {"error": "supplier_id is required"}
    session = SessionLocal()
    try:
        s = session.query(Supplier).get(int(supplier_id))
        if s is None:
            return {"error": f"Supplier id={supplier_id} not found"}
        if s.company_id != company_id:
            return {"error": "Cross-company access blocked"}
        cleaned = _allowlist(fields, SUPPLIER_FIELDS)
        if not cleaned:
            return {"error": "No valid fields to update",
                    "hint": f"Allowed: {sorted(SUPPLIER_FIELDS)}"}
        before = _snapshot_dict(s, SUPPLIER_FIELDS) | {"id": s.id, "code": s.code}
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="update_supplier", target_table="suppliers", target_id=s.id,
            payload_json=json.dumps({"fields": cleaned}, default=str),
            before_json=json.dumps(before, default=str),
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_create_supplier(company_id: int, user_id: int | None = None,
                               code: str = "", fields: dict | None = None,
                               reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not code:
        return {"error": "code is required"}
    cleaned = _allowlist(fields, SUPPLIER_FIELDS)
    cleaned["code"] = str(code).strip()
    session = SessionLocal()
    try:
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="create_supplier", target_table="suppliers", target_id=None,
            payload_json=json.dumps({"code": cleaned["code"],
                                     "fields": cleaned}, default=str),
            before_json=None,
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_propose_delete_supplier(company_id: int, user_id: int | None = None,
                               supplier_id: int = 0, reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not supplier_id:
        return {"error": "supplier_id is required"}
    session = SessionLocal()
    try:
        s = session.query(Supplier).get(int(supplier_id))
        if s is None:
            return {"error": f"Supplier id={supplier_id} not found"}
        if s.company_id != company_id:
            return {"error": "Cross-company access blocked"}
        before = _snapshot_dict(s, SUPPLIER_FIELDS) | {"id": s.id, "code": s.code}
        action = AgentAction(
            company_id=company_id, user_id=user_id,
            action_type="delete_supplier", target_table="suppliers", target_id=s.id,
            payload_json=json.dumps({}, default=str),
            before_json=json.dumps(before, default=str),
            reason=str(reason or "").strip(),
        )
        return _queue(action, session)
    finally:
        session.close()


def _t_list_pending_actions(company_id: int, limit: int = 20, **_) -> dict:
    session = SessionLocal()
    try:
        rows = (session.query(AgentAction)
                .filter(AgentAction.company_id == company_id,
                        AgentAction.status == "pending")
                .order_by(AgentAction.created_at.desc())
                .limit(max(1, int(limit)))
                .all())
        out = []
        for a in rows:
            out.append({
                "action_id":   a.id,
                "action_type": a.action_type,
                "target_table":a.target_table,
                "target_id":   a.target_id,
                "reason":      a.reason or "",
                "created_at":  a.created_at.isoformat() if a.created_at else None,
            })
        return {"count": len(out), "items": out}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, Callable[..., dict]] = {
    "inventory_value_summary": _t_inventory_value_summary,
    "list_overstock":          _t_list_overstock,
    "list_safety_gaps":        _t_list_safety_gaps,
    "list_low_nfp":            _t_list_low_nfp,
    "list_supplier_risk":      _t_list_supplier_risk,
    "list_demand_trends":      _t_list_demand_trends,
    "list_data_quality":       _t_list_data_quality,
    "abc_xyz_matrix":          _t_abc_xyz_matrix,
    "run_skill":               _t_run_skill,
    "propose_value_reduction": _t_propose_value_reduction,
    "render_chart":            _t_render_chart,
    # Write tools — queue pending changes for human approval
    "propose_update_item":              _t_propose_update_item,
    "propose_create_item":              _t_propose_create_item,
    "propose_delete_item":              _t_propose_delete_item,
    "propose_create_buffer_adjustment": _t_propose_create_buffer_adjustment,
    "propose_delete_buffer_adjustment": _t_propose_delete_buffer_adjustment,
    "propose_update_supplier":          _t_propose_update_supplier,
    "propose_create_supplier":          _t_propose_create_supplier,
    "propose_delete_supplier":          _t_propose_delete_supplier,
    "list_pending_actions":             _t_list_pending_actions,
}


# Aliases for tool names that LLMs commonly hallucinate. Maps the wrong
# name → the real one in TOOL_FUNCTIONS. Lookup is case-insensitive.
TOOL_NAME_ALIASES = {
    "get_inventory_summary":   "inventory_value_summary",
    "inventory_summary":       "inventory_value_summary",
    "get_inventory_value":     "inventory_value_summary",
    "value_summary":           "inventory_value_summary",
    "get_overstock":           "list_overstock",
    "overstock":               "list_overstock",
    "get_safety_gaps":         "list_safety_gaps",
    "safety_gaps":             "list_safety_gaps",
    "get_low_nfp":             "list_low_nfp",
    "low_nfp":                 "list_low_nfp",
    "get_supplier_risk":       "list_supplier_risk",
    "supplier_risk":           "list_supplier_risk",
    "get_demand_trends":       "list_demand_trends",
    "demand_trends":           "list_demand_trends",
    "get_data_quality":        "list_data_quality",
    "data_quality":            "list_data_quality",
    "get_abc_xyz":             "abc_xyz_matrix",
    "abc_xyz":                 "abc_xyz_matrix",
    "run_analysis":            "run_skill",
    "execute_skill":           "run_skill",
    "value_reduction":         "propose_value_reduction",
    "reduce_inventory_value":  "propose_value_reduction",
    "chart":                   "render_chart",
    "draw_chart":              "render_chart",
    "plot":                    "render_chart",
    # write-tool aliases
    "update_item":                  "propose_update_item",
    "modify_item":                  "propose_update_item",
    "edit_item":                    "propose_update_item",
    "create_item":                  "propose_create_item",
    "add_item":                     "propose_create_item",
    "delete_item":                  "propose_delete_item",
    "remove_item":                  "propose_delete_item",
    "update_supplier":              "propose_update_supplier",
    "modify_supplier":              "propose_update_supplier",
    "edit_supplier":                "propose_update_supplier",
    "create_supplier":              "propose_create_supplier",
    "add_supplier":                 "propose_create_supplier",
    "delete_supplier":              "propose_delete_supplier",
    "remove_supplier":              "propose_delete_supplier",
    "create_buffer_adjustment":     "propose_create_buffer_adjustment",
    "add_buffer_adjustment":        "propose_create_buffer_adjustment",
    "add_adjustment":               "propose_create_buffer_adjustment",
    "delete_buffer_adjustment":     "propose_delete_buffer_adjustment",
    "remove_buffer_adjustment":     "propose_delete_buffer_adjustment",
    "list_pending":                 "list_pending_actions",
    "pending_actions":              "list_pending_actions",
    "pending_changes":              "list_pending_actions",
}


def resolve_tool_name(name: str) -> str:
    """Return the canonical tool name, applying alias mapping if needed."""
    if not name:
        return ""
    if name in TOOL_FUNCTIONS:
        return name
    lower = name.strip().lower()
    if lower in TOOL_FUNCTIONS:
        return lower
    return TOOL_NAME_ALIASES.get(lower, name)


def parse_inline_tool_calls(text: str) -> list[tuple[str, dict]]:
    """
    Parse <tool_call>...</tool_call> blocks that some models (Kimi, Qwen,
    some llama variants) emit as plain text instead of using OpenAI's
    structured tool_calls field.

    Accepts the common JSON shapes:
      {"name": "...", "arguments": {...}}
      {"tool_name": "...", "parameters": {...}}
      {"function": "...", "args": {...}}

    Returns a list of (canonical_name, args_dict) tuples.
    """
    import re
    if not text or "<tool_call>" not in text:
        return []
    results: list[tuple[str, dict]] = []
    for m in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL):
        blob = m.group(1).strip()
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        raw_name = obj.get("name") or obj.get("tool_name") or obj.get("function") or ""
        args     = (obj.get("arguments") or obj.get("parameters")
                    or obj.get("args") or obj.get("input") or {})
        if not isinstance(args, dict):
            args = {}
        canonical = resolve_tool_name(str(raw_name))
        if canonical:
            results.append((canonical, args))
    return results


def dispatch(name: str, *, company_id: int, model: str, api_key: str,
             user_id: int | None = None,
             arguments: dict | None = None) -> dict:
    """
    Execute a tool by name. Always returns a JSON-serialisable dict.
    Errors are returned in the result, never raised — the LLM can read them
    and adjust.
    """
    canonical = resolve_tool_name(name)
    fn = TOOL_FUNCTIONS.get(canonical)
    if fn is None:
        return {"error": f"Unknown tool: {name}",
                "hint":  f"Valid tools: {sorted(TOOL_FUNCTIONS.keys())}"}
    args = arguments or {}
    try:
        return fn(company_id=company_id, model=model, api_key=api_key,
                  user_id=user_id, **args)
    except TypeError as exc:
        return {"error": f"Bad arguments for {canonical}: {exc}"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def serialise_for_llm(result: dict, max_chars: int = 6000) -> str:
    """Compact JSON, truncated so a huge dataframe never blows the context."""
    try:
        s = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        s = str(result)
    if len(s) > max_chars:
        s = s[:max_chars] + "...[truncated]"
    return s
