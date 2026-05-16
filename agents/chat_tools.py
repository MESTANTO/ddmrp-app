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
from typing import Any, Callable

from agents import inventory_agent
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
}


def dispatch(name: str, *, company_id: int, model: str, api_key: str,
             arguments: dict | None = None) -> dict:
    """
    Execute a tool by name. Always returns a JSON-serialisable dict.
    Errors are returned in the result, never raised — the LLM can read them
    and adjust.
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    args = arguments or {}
    try:
        return fn(company_id=company_id, model=model, api_key=api_key, **args)
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
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
