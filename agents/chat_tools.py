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
    DemandEntry,
    Item,
    SessionLocal,
    Supplier,
)
from modules.param_calculator import calculate_params as _calc_params
from modules.buffer_engine import (
    calculate_zones as _calc_zones,
    calculate_net_flow_position as _calc_nfp,
    execution_color as _exec_color,
    recalculate_buffer as _recalc_buffer,
    recalculate_all_buffers as _recalc_all_buffers,
    project_buffer_forward as _project_forward,
    plan_replenishment_orders as _plan_replenishment,
)
from modules.safety_stock import calculate_for_item as _calc_ss
from modules.bom_engine import compute_dlt as _compute_dlt
from modules.positioning_engine import (
    compute_cumulative_lt as _cumulative_lt,
    score_positioning as _score_positioning,
)
from modules.classification import compute_abc_xyz as _compute_abc_xyz


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


def _dc_to_dict(obj) -> dict:
    """Serialize a dataclass (possibly nested) to a JSON-safe dict."""
    import dataclasses
    import datetime as _dt
    d = dataclasses.asdict(obj)
    def _fix(v):
        if isinstance(v, dict):  return {k: _fix(w) for k, w in v.items()}
        if isinstance(v, list):  return [_fix(i) for i in v]
        if isinstance(v, (_dt.date, _dt.datetime)): return v.isoformat()
        return v
    return _fix(d)


def _invalidate_caches() -> None:
    """Clear page-level Streamlit caches after a buffer write."""
    try:
        from views.dashboard import _load_dashboard_data
        _load_dashboard_data.clear()
    except Exception:
        pass
    try:
        from views.inventory_manager import _load_state_cached
        _load_state_cached.clear()
    except Exception:
        pass


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
            "name": "lookup_item",
            "description": (
                "Resolve an item's numeric id from its part_number. Returns "
                "{id, part_number, description, adu, dlt, on_hand, unit_cost, "
                "default_supplier_id, ...} for the matching row, or "
                "{error: 'not found'} if no item matches. Use this before "
                "calling propose_update_item / propose_delete_item / "
                "propose_create_buffer_adjustment when you only have the "
                "part_number from a read tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "part_number": {"type": "string",
                                    "description": "Exact part number (case-insensitive)."},
                },
                "required": ["part_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_supplier",
            "description": (
                "Resolve a supplier's numeric id from its code. Returns "
                "{id, code, name, reliability_pct, lead_time_days, ...} or "
                "{error: 'not found'}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_items",
            "description": (
                "List items with their numeric id + part_number + description "
                "for the company. Use this when you need to find an item id "
                "but `lookup_item` by exact part_number didn't match. Supports "
                "optional substring filter on part_number/description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string",
                               "description": "Optional substring filter on part_number or description."},
                    "limit":  {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": [],
            },
        },
    },
    # -----------------------------------------------------------------------
    # CALCULATION TOOLS — trigger module computations from the chat
    # -----------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "calculate_item_params",
            "description": (
                "Compute what the latest ADU, DLT, lead_time_factor (LTF), and "
                "variability_factor (VF) would be for an item based on its recent "
                "demand and supply history. Does NOT write to the database — use "
                "propose_apply_item_params to queue the update for approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":      {"type": "integer"},
                    "lookback_days":{"type": "integer", "default": 60,
                                     "description": "Days of demand history to use."},
                    "forward_days": {"type": "integer", "default": 30,
                                     "description": "Days of forward demand to blend."},
                    "adu_method":   {"type": "string", "default": "blended",
                                     "enum": ["blended", "past_only", "forward_only"]},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_item_buffer",
            "description": (
                "Show the current buffer zones (TOR/TOY/TOG), Net Flow Position, "
                "execution color, and suggested order quantity for one item — "
                "computed live from the item's stored parameters. Does NOT write."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_item_buffer",
            "description": (
                "Day-by-day NFP projection for an item over a forward horizon. "
                "Returns the date the item hits Red (trigger date), the order-by "
                "date, suggested order quantity, and daily NFP trace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":      {"type": "integer"},
                    "horizon_days": {"type": "integer", "default": 60,
                                     "minimum": 1, "maximum": 365},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_item_replenishment",
            "description": (
                "Generate a full replenishment plan (list of planned orders) for "
                "an item over a forward horizon, keeping NFP in the green zone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":      {"type": "integer"},
                    "horizon_days": {"type": "integer", "default": 60,
                                     "minimum": 1, "maximum": 365},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_item_safety_stock",
            "description": (
                "Compute safety stock, reorder point, EOQ, and total inventory "
                "cost for one item using statistical models. Compares the result "
                "against the current DDMRP Top of Red."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":       {"type": "integer"},
                    "model":         {"type": "string", "default": "basic",
                                      "enum": ["basic", "demand_only", "kings"],
                                      "description": "SS calculation model."},
                    "service_level": {"type": "number", "default": 95.0,
                                      "description": "Target service level %."},
                    "safety_factor": {"type": "number", "default": 1.0},
                    "ordering_cost": {"type": "number", "default": 50.0,
                                      "description": "Fixed cost per order (€)."},
                    "holding_pct":   {"type": "number", "default": 0.25,
                                      "description": "Annual holding cost as fraction of value."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_item_dlt",
            "description": (
                "Compute the Decoupled Lead Time (DLT) for an item by traversing "
                "the BOM graph and finding the longest unprotected path. Returns "
                "computed DLT and the critical path as a list of part numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_item_positioning",
            "description": (
                "Score an item across 6 DDMRP positioning factors (CTT, MPLT, "
                "SOVH, variability, leverage, critical operation) and return "
                "a recommendation: DDMRP or MRP. Includes estimated inventory "
                "value saving if switched to DDMRP."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":   {"type": "integer"},
                    "threshold": {"type": "integer", "default": 30,
                                  "description": "Score threshold for DDMRP recommendation."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_abc_xyz_classification",
            "description": (
                "Run ABC/XYZ classification for all company items. Returns a "
                "9-cell matrix (A-X through C-Z) with item counts and annual € "
                "values per cell. Optionally override the default thresholds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "abc_a":  {"type": "number", "default": 0.8,
                               "description": "Cumulative value % threshold for A class."},
                    "abc_ab": {"type": "number", "default": 0.95,
                               "description": "Cumulative value % threshold for A+B."},
                    "xyz_x":  {"type": "number", "default": 0.5,
                               "description": "CV threshold for X class (low variability)."},
                    "xyz_y":  {"type": "number", "default": 1.0,
                               "description": "CV threshold for Y class."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_item_buffer",
            "description": (
                "Recalculate and persist the buffer zones + NFP for one item "
                "(equivalent to pressing Refresh in the UI for a single item). "
                "Writes directly to the Buffer table — no approval needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_all_buffers",
            "description": (
                "Recalculate and persist buffer zones + NFP for ALL items in the "
                "company (equivalent to the Dashboard '🔄 Refresh Buffers' button). "
                "Writes directly — no approval needed."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_apply_item_params",
            "description": (
                "Compute the latest ADU/DLT/VF/LTF for an item from its demand "
                "history and queue the update for human approval (goes to Pending "
                "Changes tab). The approval card shows the before vs computed "
                "values so the user can review before applying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id":      {"type": "integer"},
                    "lookback_days":{"type": "integer", "default": 60},
                    "forward_days": {"type": "integer", "default": 30},
                    "adu_method":   {"type": "string", "default": "blended",
                                     "enum": ["blended", "past_only", "forward_only"]},
                    "reason":       {"type": "string",
                                     "description": "Why you're proposing this update."},
                },
                "required": ["item_id", "reason"],
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


def _t_calculate_item_params(company_id: int, item_id: int = 0,
                             lookback_days: int = 60, forward_days: int = 30,
                             adu_method: str = "blended", **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        result = _calc_params(item, lookback_days=int(lookback_days),
                              forward_days=int(forward_days), adu_method=adu_method)
        return _dc_to_dict(result)
    finally:
        session.close()


def _t_preview_item_buffer(company_id: int, item_id: int = 0, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        zones = _calc_zones(item)
        nfp   = _calc_nfp(item)
        color, pct = _exec_color(item.on_hand or 0, zones)
        from modules.buffer_engine import calculate_suggested_order, determine_status
        status  = determine_status(nfp, zones)
        suggest = calculate_suggested_order(nfp, zones)
        return {
            "item_id":          item.id,
            "part_number":      item.part_number,
            "on_hand":          item.on_hand,
            "top_of_red":       zones.top_of_red,
            "top_of_yellow":    zones.top_of_yellow,
            "top_of_green":     zones.top_of_green,
            "net_flow_position":nfp,
            "status":           status,
            "execution_color":  color,
            "buffer_pct":       round(pct, 2),
            "suggested_order":  suggest,
            "adu":              zones.adu,
            "dlt":              zones.dlt,
        }
    finally:
        session.close()


def _t_project_item_buffer(company_id: int, item_id: int = 0,
                            horizon_days: int = 60, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        result = _project_forward(item, horizon_days=int(horizon_days))
        d = _dc_to_dict(result)
        # Truncate daily projection to keep response small
        if "daily" in d and len(d["daily"]) > 14:
            d["daily"] = d["daily"][:14]
            d["daily_truncated_at"] = 14
        return d
    finally:
        session.close()


def _t_plan_item_replenishment(company_id: int, item_id: int = 0,
                                horizon_days: int = 60, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        result = _plan_replenishment(item, horizon_days=int(horizon_days))
        d = _dc_to_dict(result)
        # Drop daily arrays to keep payload manageable
        d.pop("daily_planned", None)
        d.pop("daily_unplanned", None)
        return d
    finally:
        session.close()


def _t_calculate_item_safety_stock(company_id: int, item_id: int = 0,
                                    model: str = "basic", service_level: float = 95.0,
                                    safety_factor: float = 1.0,
                                    ordering_cost: float = 50.0,
                                    holding_pct: float = 0.25,
                                    lookback_days: int = 90, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        result = _calc_ss(
            item,
            model=model,
            service_level=float(service_level),
            safety_factor=float(safety_factor),
            default_ordering_cost=float(ordering_cost),
            default_holding_pct=float(holding_pct),
            lookback_days=int(lookback_days),
        )
        return _dc_to_dict(result)
    finally:
        session.close()


def _t_calculate_item_dlt(company_id: int, item_id: int = 0, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        result = _compute_dlt(item)
        return _dc_to_dict(result)
    finally:
        session.close()


def _t_score_item_positioning(company_id: int, item_id: int = 0,
                               threshold: int = 30, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        cumulative_lt = _cumulative_lt(item)
        result = _score_positioning(item, cumulative_lt, threshold=int(threshold))
        return _dc_to_dict(result)
    finally:
        session.close()


def _t_run_abc_xyz_classification(company_id: int,
                                   abc_a: float = 0.8, abc_ab: float = 0.95,
                                   xyz_x: float = 0.5, xyz_y: float = 1.0,
                                   **_) -> dict:
    session = SessionLocal()
    try:
        items   = session.query(Item).filter(Item.company_id == company_id).all()
        demands = (session.query(DemandEntry)
                   .filter(DemandEntry.item_id.in_([i.id for i in items]))
                   .all())
        df = _compute_abc_xyz(items, demands,
                              abc_a_thr=float(abc_a), abc_ab_thr=float(abc_ab),
                              xyz_x_thr=float(xyz_x), xyz_y_thr=float(xyz_y))
        from collections import defaultdict
        counts: dict = defaultdict(int)
        values: dict = defaultdict(float)
        for _, row in df.iterrows():
            cell = row.get("acvs", "??")
            counts[cell] += 1
            values[cell] += float(row.get("annual_value", 0) or 0)
        cells = ["A-X", "A-Y", "A-Z", "B-X", "B-Y", "B-Z", "C-X", "C-Y", "C-Z"]
        return {
            "cells": [
                {"cell": c, "count": counts.get(c, 0),
                 "annual_value": round(values.get(c, 0), 2)}
                for c in cells
            ],
            "total_items":        sum(counts.values()),
            "total_annual_value": round(sum(values.values()), 2),
        }
    finally:
        session.close()


def _t_refresh_item_buffer(company_id: int, item_id: int = 0, **_) -> dict:
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
    finally:
        session.close()
    status = _recalc_buffer(item)
    _invalidate_caches()
    d = _dc_to_dict(status)
    d.pop("zones", None)  # keep response compact
    d["refreshed"] = True
    return d


def _t_refresh_all_buffers(company_id: int, **_) -> dict:
    results = _recalc_all_buffers(company_id=company_id)
    _invalidate_caches()
    return {
        "refreshed": True,
        "count":     len(results),
        "summary":   f"Recalculated buffers for {len(results)} items.",
    }


def _t_propose_apply_item_params(company_id: int, user_id: int | None = None,
                                  item_id: int = 0, lookback_days: int = 60,
                                  forward_days: int = 30, adu_method: str = "blended",
                                  reason: str = "", **_) -> dict:
    rl = _check_rate_limit(company_id)
    if rl: return rl
    if not item_id:
        return {"error": "item_id is required"}
    session = SessionLocal()
    try:
        item = session.query(Item).get(int(item_id))
        if item is None or item.company_id != company_id:
            return {"error": "Item not found or cross-company access blocked"}
        calc = _calc_params(item, lookback_days=int(lookback_days),
                            forward_days=int(forward_days), adu_method=adu_method)
        fields = {
            "adu":               calc.adu,
            "dlt":               calc.dlt,
            "lead_time_factor":  calc.lead_time_factor,
            "variability_factor":calc.variability_factor,
        }
        auto_reason = (
            reason or
            f"Computed from {calc.n_demand_days}d demand history "
            f"(ADU {calc.current_adu:.2f}→{calc.adu:.2f}, "
            f"DLT {calc.current_dlt:.1f}→{calc.dlt:.1f})"
        )
        # Delegate to the existing propose_update_item logic
        return _t_propose_update_item(
            company_id=company_id, user_id=user_id,
            item_id=item_id, fields=fields, reason=auto_reason,
        )
    finally:
        session.close()


def _t_lookup_item(company_id: int, part_number: str = "", **_) -> dict:
    """Resolve an item by exact part_number (case-insensitive)."""
    if not part_number:
        return {"error": "part_number is required"}
    pn = str(part_number).strip().upper()
    session = SessionLocal()
    try:
        from sqlalchemy import func as _func
        it = (session.query(Item)
              .filter(Item.company_id == company_id,
                      _func.upper(Item.part_number) == pn)
              .first())
        if it is None:
            return {"error": "not found", "part_number": pn,
                    "hint": "Try list_items(filter=...) to search by substring."}
        return {
            "id":                  it.id,
            "part_number":         it.part_number,
            "description":         it.description,
            "adu":                 it.adu,
            "dlt":                 it.dlt,
            "lead_time_factor":    it.lead_time_factor,
            "variability_factor":  it.variability_factor,
            "min_order_qty":       it.min_order_qty,
            "order_cycle":         it.order_cycle,
            "on_hand":             it.on_hand,
            "unit_cost":           it.unit_cost,
            "category":            it.category,
            "item_type":           it.item_type,
            "default_supplier_id": it.default_supplier_id,
            "buffer_profile_id":   it.buffer_profile_id,
        }
    finally:
        session.close()


def _t_lookup_supplier(company_id: int, code: str = "", **_) -> dict:
    """Resolve a supplier by exact code (case-insensitive)."""
    if not code:
        return {"error": "code is required"}
    c = str(code).strip().upper()
    session = SessionLocal()
    try:
        from sqlalchemy import func as _func
        s = (session.query(Supplier)
             .filter(Supplier.company_id == company_id,
                     _func.upper(Supplier.code) == c)
             .first())
        if s is None:
            return {"error": "not found", "code": c}
        return {
            "id":               s.id,
            "code":             s.code,
            "name":             s.name,
            "reliability_pct":  s.reliability_pct,
            "lead_time_days":   s.lead_time_days,
            "payment_terms":    s.payment_terms,
            "contact_email":    s.contact_email,
        }
    finally:
        session.close()


def _t_list_items(company_id: int, filter: str = "", limit: int = 50, **_) -> dict:
    """List items for the company, optionally filtering by part_number/description substring."""
    session = SessionLocal()
    try:
        q = session.query(Item.id, Item.part_number, Item.description).filter(
            Item.company_id == company_id
        )
        if filter:
            like = f"%{filter.strip()}%"
            from sqlalchemy import or_
            q = q.filter(
                or_(Item.part_number.ilike(like), Item.description.ilike(like))
            )
        rows = q.order_by(Item.part_number).limit(max(1, int(limit))).all()
        return {
            "count": len(rows),
            "items": [{"id": r.id, "part_number": r.part_number,
                        "description": r.description} for r in rows],
        }
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
    # Lookup / search tools — bridge part_number → numeric id
    "lookup_item":                      _t_lookup_item,
    "lookup_supplier":                  _t_lookup_supplier,
    "list_items":                       _t_list_items,
    # Calculation tools — trigger module computations
    "calculate_item_params":            _t_calculate_item_params,
    "preview_item_buffer":              _t_preview_item_buffer,
    "project_item_buffer":              _t_project_item_buffer,
    "plan_item_replenishment":          _t_plan_item_replenishment,
    "calculate_item_safety_stock":      _t_calculate_item_safety_stock,
    "calculate_item_dlt":               _t_calculate_item_dlt,
    "score_item_positioning":           _t_score_item_positioning,
    "run_abc_xyz_classification":       _t_run_abc_xyz_classification,
    "refresh_item_buffer":              _t_refresh_item_buffer,
    "refresh_all_buffers":              _t_refresh_all_buffers,
    "propose_apply_item_params":        _t_propose_apply_item_params,
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
    # calculation tool aliases
    "calc_item_params":             "calculate_item_params",
    "recalculate_item_params":      "calculate_item_params",
    "get_item_buffer":              "preview_item_buffer",
    "item_buffer":                  "preview_item_buffer",
    "buffer_zones":                 "preview_item_buffer",
    "project_buffer":               "project_item_buffer",
    "buffer_projection":            "project_item_buffer",
    "replenishment_plan":           "plan_item_replenishment",
    "plan_replenishment":           "plan_item_replenishment",
    "safety_stock":                 "calculate_item_safety_stock",
    "calc_safety_stock":            "calculate_item_safety_stock",
    "item_dlt":                     "calculate_item_dlt",
    "dlt":                          "calculate_item_dlt",
    "positioning_score":            "score_item_positioning",
    "ddmrp_score":                  "score_item_positioning",
    "abc_xyz":                      "run_abc_xyz_classification",
    "classify_items":               "run_abc_xyz_classification",
    "refresh_buffer":               "refresh_item_buffer",
    "recalculate_buffer":           "refresh_item_buffer",
    "refresh_buffers":              "refresh_all_buffers",
    "recalculate_all_buffers":      "refresh_all_buffers",
    "apply_item_params":            "propose_apply_item_params",
    "update_params":                "propose_apply_item_params",
    # lookup aliases
    "get_item":                     "lookup_item",
    "find_item":                    "lookup_item",
    "resolve_item":                 "lookup_item",
    "get_supplier":                 "lookup_supplier",
    "find_supplier":                "lookup_supplier",
    "resolve_supplier":             "lookup_supplier",
    "search_items":                 "list_items",
    "get_items":                    "list_items",
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
