"""
Inventory Manager Agent — core module.

Two-phase architecture:
  Phase 1 — Python tool functions gather all relevant data from the DB.
  Phase 2 — Single NVIDIA NIM LLM call with structured context + skill files.
  Phase 3 — Parse JSON response, validate, and persist AgentRun + AgentSignal rows.

All functions take company_id as their first argument. No global implicit reads.
The LLM never writes to operational tables — only AgentSignal rows are created.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

from openai import OpenAI

from database.db import (
    SessionLocal, Item, Buffer, DemandEntry, SupplyEntry,
    Supplier, AgentRun, AgentSignal,
)
from modules.classification import compute_abc_xyz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
_SKILLS_DIR  = Path(__file__).parent / "skills"

VALID_SIGNAL_TYPES = {
    "stockout_risk", "overstock", "low_nfp", "buffer_resizing",
    "data_quality", "demand_trend", "abc_xyz_policy",
    "safety_stock_gap", "supplier_risk", "portfolio",
}
VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}

# Context section headers — skills are activated when their trigger phrase appears
_SKILL_TRIGGERS = {
    "skill_01_data_quality":       "DATA QUALITY ISSUES",
    "skill_02_ddmrp_buffer_analysis": "EXECUTION ALARMS",
    "skill_03_abc_xyz_policy":     "ABC/XYZ CLASSIFICATION",
    "skill_04_demand_variability": "DEMAND TRENDS",
    "skill_05_safety_stock_optimization": "SAFETY STOCK GAPS",
    "skill_06_overstock_and_excess":  "OVERSTOCK ITEMS",
    "skill_07_supplier_risk":      "SUPPLIER RISK",
}

_AGENT_SYSTEM_PROMPT = """\
You are an autonomous Inventory Manager Agent embedded in a DDMRP supply chain application.

You have expert knowledge of DDMRP (Demand Driven MRP) methodology. The domain knowledge
and analysis rules you must apply are provided in the SKILLS sections below.

YOUR TASK:
Analyse the INVENTORY CONTEXT section and generate a prioritised list of findings and
recommendations as a JSON array.

CRITICAL OUTPUT RULES:
1. Respond with ONLY a valid JSON array. No markdown, no preamble, no explanation outside the JSON.
2. Each element must be a signal object with EXACTLY these fields:
   {{
     "signal_type": string,
     "severity": string,
     "part_number": string,
     "title": string,
     "detail": string,
     "recommendation": string,
     "metric_name": string,
     "metric_value": number or null,
     "metric_threshold": number or null
   }}
3. signal_type must be one of:
   stockout_risk | overstock | low_nfp | buffer_resizing | data_quality |
   demand_trend | abc_xyz_policy | safety_stock_gap | supplier_risk | portfolio
4. severity must be one of: critical | high | medium | low | info
5. title must be ≤ 120 characters
6. part_number must match exactly as shown in the data, or "PORTFOLIO" for company-wide findings
7. Generate between 3 and 30 signals. Prioritise critical first, then high, then medium.
8. Every recommendation must name the specific item, state the action, and quantify the impact.
   BAD: "Optimize inventory."
   GOOD: "Reduce safety stock for ITEM-001 from 500 to 280 units — demand CV=0.22, coverage 210 days vs target 90. Cash release: €11,000."
9. Never recommend reducing safety stock on an item currently in red or dark_red execution.
10. Do not invent data. If a metric is not available, state that clearly in the detail.

{skills}

--- INVENTORY CONTEXT ---
{context}
--- END CONTEXT ---
"""


# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------

def load_skills(context_text: str) -> str:
    """
    Load only the skill files relevant to the sections present in the context.
    Returns formatted skill content to inject into the system prompt.
    """
    if not _SKILLS_DIR.exists():
        return ""

    active_skills = []
    for skill_name, trigger in _SKILL_TRIGGERS.items():
        if trigger in context_text:
            skill_path = _SKILLS_DIR / f"{skill_name}.md"
            if skill_path.exists():
                active_skills.append((skill_name, skill_path))

    # Always include buffer analysis and data quality
    for must_have in ("skill_01_data_quality", "skill_02_ddmrp_buffer_analysis"):
        if not any(s[0] == must_have for s in active_skills):
            p = _SKILLS_DIR / f"{must_have}.md"
            if p.exists():
                active_skills.insert(0, (must_have, p))

    if not active_skills:
        return ""

    parts = []
    for skill_name, skill_path in active_skills:
        parts.append(f"\n\n=== SKILL: {skill_name.upper()} ===\n{skill_path.read_text()}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Phase 1 — Data tool functions
# ---------------------------------------------------------------------------

def get_inventory_snapshot(company_id: int) -> list[dict]:
    """All items with buffer data for this company."""
    session = SessionLocal()
    try:
        items = (session.query(Item)
                 .filter(Item.company_id == company_id)
                 .order_by(Item.part_number)
                 .all())
        buffers = {b.item_id: b for b in
                   session.query(Buffer)
                   .filter(Buffer.item_id.in_([i.id for i in items]))
                   .all()} if items else {}

        result = []
        for it in items:
            buf = buffers.get(it.id)
            result.append({
                "id":            it.id,
                "part_number":   it.part_number,
                "description":   it.description,
                "item_type":     it.item_type,
                "category":      it.category or "",
                "unit_cost":     it.unit_cost or 0.0,
                "on_hand":       it.on_hand or 0.0,
                "adu":           it.adu or 0.0,
                "dlt":           it.dlt or 0.0,
                "ltf":           it.lead_time_factor or 0.5,
                "vf":            it.variability_factor or 0.5,
                "moq":           it.min_order_qty or 0.0,
                "order_cycle":   it.order_cycle or 0.0,
                "default_supplier_id": it.default_supplier_id,
                "top_of_red":    buf.top_of_red    if buf else 0.0,
                "top_of_yellow": buf.top_of_yellow if buf else 0.0,
                "top_of_green":  buf.top_of_green  if buf else 0.0,
                "nfp":           buf.net_flow_position if buf else 0.0,
                "buffer_status_pct": buf.buffer_status_pct if buf else 0.0,
                "execution_color":   buf.execution_color  if buf else "green",
                "has_buffer":    buf is not None,
            })
        return result
    finally:
        session.close()


def get_execution_alarms(company_id: int) -> list[dict]:
    """Items with red or dark_red execution color."""
    snap = get_inventory_snapshot(company_id)
    return [r for r in snap if r["execution_color"] in ("red", "dark_red")]


def get_low_nfp_items(company_id: int, threshold_pct: float = 1.0) -> list[dict]:
    """Items where NFP < top_of_yellow (order signal active)."""
    snap = get_inventory_snapshot(company_id)
    result = []
    for r in snap:
        if r["has_buffer"] and r["top_of_yellow"] > 0 and r["nfp"] < r["top_of_yellow"]:
            r["nfp_pct_of_tor"] = (r["nfp"] / r["top_of_red"] * 100) if r["top_of_red"] > 0 else 0.0
            result.append(r)
    return sorted(result, key=lambda x: x.get("nfp_pct_of_tor", 0))


def get_overstock_items(company_id: int, threshold_pct: float = 1.0) -> list[dict]:
    """Items where on_hand > top_of_green."""
    snap = get_inventory_snapshot(company_id)
    result = []
    for r in snap:
        if r["has_buffer"] and r["top_of_green"] > 0 and r["on_hand"] > r["top_of_green"]:
            r["excess_units"]   = r["on_hand"] - r["top_of_green"]
            r["excess_value"]   = r["excess_units"] * r["unit_cost"]
            r["on_hand_vs_tog"] = r["on_hand"] / r["top_of_green"] * 100
            result.append(r)
    return sorted(result, key=lambda x: x.get("excess_value", 0), reverse=True)


def get_demand_trends(company_id: int, lookback_days: int = 90) -> dict[int, dict]:
    """Weekly demand stats per item over the lookback window."""
    session = SessionLocal()
    try:
        items = (session.query(Item)
                 .filter(Item.company_id == company_id)
                 .all())
        if not items:
            return {}
        item_ids = [i.id for i in items]
        cutoff   = datetime.utcnow() - timedelta(days=lookback_days)
        demands  = (session.query(DemandEntry)
                    .filter(DemandEntry.item_id.in_(item_ids),
                            DemandEntry.demand_date >= cutoff)
                    .all())
        adu_map  = {i.id: (i.adu or 0.0) for i in items}

        demand_by_item: dict = defaultdict(list)
        for d in demands:
            demand_by_item[d.item_id].append(d)

        result: dict = {}
        for item in items:
            item_d = demand_by_item.get(item.id, [])
            if not item_d:
                result[item.id] = {
                    "weekly_totals": [], "mean": 0.0, "std": 0.0,
                    "cv": 0.0, "trend_direction": "flat", "trend_slope": 0.0,
                    "total_entries": 0,
                    "recent_adu": 0.0,
                    "stored_adu": adu_map.get(item.id, 0.0),
                    "adu_divergence_pct": 0.0,
                }
                continue

            weekly: dict = defaultdict(float)
            for d in item_d:
                wk = d.demand_date.isocalendar()[:2]
                weekly[wk] += d.quantity

            vals = list(weekly.values())
            w_mean = mean(vals) if vals else 0.0
            w_std  = stdev(vals) if len(vals) >= 2 else 0.0
            cv     = (w_std / w_mean) if w_mean > 0 else 0.0

            # Simple trend: compare first half vs second half
            half = max(1, len(vals) // 2)
            first_half  = mean(vals[:half])  if vals[:half]  else 0.0
            second_half = mean(vals[half:])  if vals[half:]  else 0.0
            slope = second_half - first_half
            if abs(slope) < first_half * 0.10:
                direction = "flat"
            elif slope > 0:
                direction = "up"
            else:
                direction = "down"

            recent_adu     = sum(d.quantity for d in item_d) / lookback_days
            stored_adu     = adu_map.get(item.id, 0.0)
            adu_divergence = (abs(recent_adu - stored_adu) / stored_adu * 100
                              if stored_adu > 0 else 0.0)

            result[item.id] = {
                "weekly_totals":      vals,
                "mean":               w_mean,
                "std":                w_std,
                "cv":                 cv,
                "trend_direction":    direction,
                "trend_slope":        slope,
                "total_entries":      len(item_d),
                "recent_adu":         round(recent_adu, 4),
                "stored_adu":         stored_adu,
                "adu_divergence_pct": round(adu_divergence, 1),
            }
        return result
    finally:
        session.close()


def get_safety_stock_gaps(company_id: int) -> list[dict]:
    """Items where on_hand < top_of_red."""
    snap = get_inventory_snapshot(company_id)
    result = []
    for r in snap:
        if r["has_buffer"] and r["on_hand"] < r["top_of_red"]:
            r["gap_units"] = r["top_of_red"] - r["on_hand"]
            r["gap_value"] = r["gap_units"] * r["unit_cost"]
            r["days_until_stockout"] = (r["on_hand"] / r["adu"]
                                        if r["adu"] > 0 else 999)
            result.append(r)
    return sorted(result, key=lambda x: x.get("days_until_stockout", 999))


def get_buffer_sizing_issues(company_id: int) -> list[dict]:
    """Items where buffer zones appear significantly misaligned with current ADU/DLT."""
    snap = get_inventory_snapshot(company_id)
    result = []
    for r in snap:
        if not r["has_buffer"] or r["adu"] <= 0 or r["dlt"] <= 0:
            continue
        calc_tor = r["adu"] * r["dlt"] * (r["ltf"] + r["vf"])
        if calc_tor <= 0:
            continue
        pct_diff = (r["top_of_red"] - calc_tor) / calc_tor * 100
        if abs(pct_diff) > 20:   # more than 20% off
            r["calc_tor"]  = round(calc_tor, 2)
            r["pct_diff"]  = round(pct_diff, 1)
            r["stale"]     = abs(pct_diff) > 40
            result.append(r)
    return sorted(result, key=lambda x: abs(x.get("pct_diff", 0)), reverse=True)


def get_data_quality_issues(company_id: int) -> list[dict]:
    """Items with missing or inconsistent master data."""
    snap = get_inventory_snapshot(company_id)
    result = []
    for r in snap:
        issues = []
        if r["unit_cost"] <= 0:
            issues.append("missing_unit_cost")
        if r["adu"] <= 0 and r["has_buffer"]:
            issues.append("zero_adu_with_buffer")
        if r["dlt"] <= 0 and r["has_buffer"]:
            issues.append("zero_dlt_with_buffer")
        if r["has_buffer"]:
            if r["top_of_red"] >= r["top_of_yellow"] > 0:
                issues.append("buffer_zones_inconsistent")
            if r["top_of_yellow"] >= r["top_of_green"] > 0:
                issues.append("buffer_zones_inconsistent")
        if r["item_type"] == "P" and not r["default_supplier_id"]:
            issues.append("missing_supplier_for_purchased_item")
        if issues:
            r["issues"] = issues
            result.append(r)
    return result


def get_supplier_risk_items(company_id: int) -> list[dict]:
    """Items whose supplier has reliability < 90% — especially those in red execution."""
    session = SessionLocal()
    try:
        items = (session.query(Item)
                 .filter(Item.company_id == company_id,
                         Item.default_supplier_id.isnot(None))
                 .all())
        if not items:
            return []

        supplier_ids = list({i.default_supplier_id for i in items if i.default_supplier_id})
        suppliers    = {s.id: s for s in
                        session.query(Supplier)
                        .filter(Supplier.id.in_(supplier_ids))
                        .all()}
        buffers      = {b.item_id: b for b in
                        session.query(Buffer)
                        .filter(Buffer.item_id.in_([i.id for i in items]))
                        .all()}

        result = []
        for it in items:
            sup = suppliers.get(it.default_supplier_id)
            if not sup:
                continue
            rel = sup.reliability_pct or 100.0
            if rel >= 90:
                continue  # acceptable reliability
            buf = buffers.get(it.id)
            result.append({
                "id":               it.id,
                "part_number":      it.part_number,
                "description":      it.description,
                "unit_cost":        it.unit_cost or 0.0,
                "on_hand":          it.on_hand or 0.0,
                "adu":              it.adu or 0.0,
                "dlt":              it.dlt or 0.0,
                "ltf":              it.lead_time_factor or 0.5,
                "vf":               it.variability_factor or 0.5,
                "supplier_id":      sup.id,
                "supplier_name":    sup.name,
                "supplier_code":    sup.code,
                "supplier_lt_days": sup.lead_time_days or 0,
                "reliability_pct":  rel,
                "execution_color":  buf.execution_color if buf else "green",
                "top_of_red":       buf.top_of_red      if buf else 0.0,
                "has_buffer":       buf is not None,
                "dlt_vs_sup_lt_gap": (sup.lead_time_days or 0) - (it.dlt or 0),
            })
        return sorted(result, key=lambda x: x["reliability_pct"])
    finally:
        session.close()


def get_abc_xyz_classification(company_id: int) -> list[dict]:
    """Return ABC/XYZ classification records for all items."""
    session = SessionLocal()
    try:
        items   = session.query(Item).filter(Item.company_id == company_id).all()
        item_ids = [i.id for i in items]
        demands  = (session.query(DemandEntry)
                    .filter(DemandEntry.item_id.in_(item_ids))
                    .all()) if item_ids else []
    finally:
        session.close()

    if not items:
        return []

    df = compute_abc_xyz(items, demands)
    if df.empty:
        return []

    return df[["id", "part_number", "abc", "xyz", "acvs",
               "annual_value", "cv", "unit_cost"]].to_dict(orient="records")


# ---------------------------------------------------------------------------
# Phase 2 — Context builder
# ---------------------------------------------------------------------------

def build_agent_context(company_id: int) -> tuple[str, dict]:
    """
    Collect all tool outputs and format as a structured text context for the LLM.
    Returns (context_text, raw_data_dict).
    """
    raw: dict = {}

    raw["snapshot"]        = get_inventory_snapshot(company_id)
    raw["alarms"]          = get_execution_alarms(company_id)
    raw["low_nfp"]         = get_low_nfp_items(company_id)
    raw["overstock"]       = get_overstock_items(company_id)
    raw["demand_trends"]   = get_demand_trends(company_id)
    raw["safety_gaps"]     = get_safety_stock_gaps(company_id)
    raw["buffer_issues"]   = get_buffer_sizing_issues(company_id)
    raw["data_quality"]    = get_data_quality_issues(company_id)
    raw["supplier_risk"]   = get_supplier_risk_items(company_id)
    raw["abc_xyz"]         = get_abc_xyz_classification(company_id)

    lines = []
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    total = len(raw["snapshot"])

    lines.append(f"=== INVENTORY MANAGER AGENT — ANALYSIS CONTEXT ===")
    lines.append(f"Generated: {now}")
    lines.append(f"Company ID: {company_id}")
    lines.append(f"Total items: {total}  |  Items with buffers: {sum(1 for r in raw['snapshot'] if r['has_buffer'])}")
    lines.append("")

    # ── EXECUTION ALARMS ─────────────────────────────────────────────────
    lines.append("=== CRITICAL EXECUTION ALARMS ===")
    if raw["alarms"]:
        lines.append(f"{'Part #':<20} {'Color':<10} {'On-Hand':>10} {'TOR':>10} {'Status%':>8} {'ADU':>8} {'DLT':>6} {'Cost€':>10}")
        for r in raw["alarms"][:20]:
            lines.append(f"{r['part_number']:<20} {r['execution_color']:<10} "
                         f"{r['on_hand']:>10.1f} {r['top_of_red']:>10.1f} "
                         f"{r['buffer_status_pct']:>8.1f} {r['adu']:>8.3f} "
                         f"{r['dlt']:>6.0f} {r['unit_cost']:>10.2f}")
    else:
        lines.append("None — all items with buffers are in yellow or green execution.")
    lines.append("")

    # ── LOW NFP ──────────────────────────────────────────────────────────
    lines.append("=== LOW NET FLOW POSITION ITEMS (NFP < TOY) ===")
    if raw["low_nfp"]:
        lines.append(f"{'Part #':<20} {'NFP':>10} {'TOY':>10} {'TOG':>10} {'NFP%TOR':>8} {'ADU':>8} {'DLT':>6}")
        for r in raw["low_nfp"][:20]:
            lines.append(f"{r['part_number']:<20} {r['nfp']:>10.1f} "
                         f"{r['top_of_yellow']:>10.1f} {r['top_of_green']:>10.1f} "
                         f"{r.get('nfp_pct_of_tor', 0):>8.1f} {r['adu']:>8.3f} {r['dlt']:>6.0f}")
    else:
        lines.append("None — all buffered items have NFP above TOY.")
    lines.append("")

    # ── OVERSTOCK ────────────────────────────────────────────────────────
    lines.append("=== OVERSTOCK ITEMS (on_hand > TOG) ===")
    if raw["overstock"]:
        lines.append(f"{'Part #':<20} {'On-Hand':>10} {'TOG':>10} {'Excess Units':>13} {'Excess €':>12} {'OH%TOG':>8}")
        for r in raw["overstock"][:15]:
            lines.append(f"{r['part_number']:<20} {r['on_hand']:>10.1f} "
                         f"{r['top_of_green']:>10.1f} {r['excess_units']:>13.1f} "
                         f"{r['excess_value']:>12.2f} {r['on_hand_vs_tog']:>8.1f}%")
    else:
        lines.append("None — no items above TOG.")
    lines.append("")

    # ── SAFETY STOCK GAPS ────────────────────────────────────────────────
    lines.append("=== SAFETY STOCK GAPS (on_hand < TOR) ===")
    if raw["safety_gaps"]:
        lines.append(f"{'Part #':<20} {'On-Hand':>10} {'TOR':>10} {'Gap Units':>10} {'Gap €':>12} {'Days2SO':>8}")
        for r in raw["safety_gaps"][:15]:
            d2s = r["days_until_stockout"]
            lines.append(f"{r['part_number']:<20} {r['on_hand']:>10.1f} "
                         f"{r['top_of_red']:>10.1f} {r['gap_units']:>10.1f} "
                         f"{r['gap_value']:>12.2f} {d2s:>8.1f}")
    else:
        lines.append("None — all items with buffers are above TOR.")
    lines.append("")

    # ── DEMAND TRENDS ────────────────────────────────────────────────────
    lines.append("=== DEMAND TRENDS (90-day window) ===")
    trend_items = [
        (item_id, t) for item_id, t in raw["demand_trends"].items()
        if t["total_entries"] >= 5 and (t["adu_divergence_pct"] > 20 or t["cv"] > 0.8)
    ]
    if trend_items:
        snap_map = {r["id"]: r for r in raw["snapshot"]}
        lines.append(f"{'Part #':<20} {'Stored ADU':>11} {'Recent ADU':>11} {'Div%':>6} {'CV':>6} {'Trend':>8}")
        for item_id, t in sorted(trend_items, key=lambda x: x[1]["adu_divergence_pct"], reverse=True)[:15]:
            pn = snap_map.get(item_id, {}).get("part_number", str(item_id))
            lines.append(f"{pn:<20} {t['stored_adu']:>11.3f} {t['recent_adu']:>11.3f} "
                         f"{t['adu_divergence_pct']:>6.1f} {t['cv']:>6.2f} {t['trend_direction']:>8}")
    else:
        lines.append("No significant demand trend deviations detected.")
    lines.append("")

    # ── ABC/XYZ ──────────────────────────────────────────────────────────
    lines.append("=== ABC/XYZ CLASSIFICATION ===")
    if raw["abc_xyz"]:
        from collections import Counter
        cell_counts: Counter = Counter()
        cell_values: dict    = defaultdict(float)
        for r in raw["abc_xyz"]:
            key = r["acvs"]
            cell_counts[key] += 1
            cell_values[key] += r["annual_value"]
        lines.append(f"{'Cell':<8} {'Items':>6} {'Annual Value €':>16}")
        for cell in ["A-X", "A-Y", "A-Z", "B-X", "B-Y", "B-Z", "C-X", "C-Y", "C-Z"]:
            lines.append(f"{cell:<8} {cell_counts.get(cell, 0):>6} {cell_values.get(cell, 0):>16,.0f}")
    else:
        lines.append("No ABC/XYZ data available (no items or missing cost/demand data).")
    lines.append("")

    # ── BUFFER SIZING ISSUES ─────────────────────────────────────────────
    lines.append("=== BUFFER SIZING ISSUES (TOR diverges >20% from formula) ===")
    if raw["buffer_issues"]:
        lines.append(f"{'Part #':<20} {'Curr TOR':>10} {'Calc TOR':>10} {'Diff%':>7} {'Stale':>6}")
        for r in raw["buffer_issues"][:15]:
            lines.append(f"{r['part_number']:<20} {r['top_of_red']:>10.1f} "
                         f"{r['calc_tor']:>10.1f} {r['pct_diff']:>7.1f} {str(r['stale']):>6}")
    else:
        lines.append("No significant buffer sizing misalignments detected.")
    lines.append("")

    # ── DATA QUALITY ─────────────────────────────────────────────────────
    lines.append("=== DATA QUALITY ISSUES ===")
    if raw["data_quality"]:
        lines.append(f"{'Part #':<20} {'Issues'}")
        for r in raw["data_quality"][:20]:
            lines.append(f"{r['part_number']:<20} {', '.join(r['issues'])}")
    else:
        lines.append("No data quality issues detected.")
    lines.append("")

    # ── SUPPLIER RISK ────────────────────────────────────────────────────
    lines.append("=== SUPPLIER RISK ===")
    if raw["supplier_risk"]:
        lines.append(f"{'Part #':<20} {'Supplier':<25} {'Rel%':>6} {'Exec':>8} {'DLT':>5} {'SupLT':>6} {'LTGap':>7}")
        for r in raw["supplier_risk"][:15]:
            lines.append(f"{r['part_number']:<20} {r['supplier_name'][:24]:<25} "
                         f"{r['reliability_pct']:>6.0f} {r['execution_color']:>8} "
                         f"{r['dlt']:>5.0f} {r['supplier_lt_days']:>6} "
                         f"{r['dlt_vs_sup_lt_gap']:>7.0f}")
    else:
        lines.append("No supplier risk items detected (all suppliers have reliability ≥ 90%).")
    lines.append("")

    context_text = "\n".join(lines)

    # Cap context to avoid token overflow (~12k chars)
    if len(context_text) > 12_000:
        context_text = context_text[:12_000] + "\n\n[...context truncated to fit token limit...]"

    return context_text, raw


# ---------------------------------------------------------------------------
# Phase 2 — LLM call
# ---------------------------------------------------------------------------

def call_nvidia_nim(
    context: str,
    model:   str   = "deepseek-ai/deepseek-v3-0324",
    api_key: str   = "",
    max_tokens: int = 8192,
) -> tuple[str, str]:
    """
    Call the NVIDIA NIM API with the assembled context + skills.
    Returns (raw_response_text, status) where status is 'ok' or 'error'.
    """
    if not api_key:
        return "No NVIDIA_API_KEY configured.", "error"

    # Sanitise model slug
    model = model.strip()[:100]

    skills_text  = load_skills(context)
    system_prompt = _AGENT_SYSTEM_PROMPT.format(
        skills=skills_text if skills_text else "(no specific skill files loaded)",
        context="{context}",   # placeholder — context injected below
    ).replace("{context}", context)  # safe: context is trusted internal data

    try:
        client = OpenAI(base_url=_NVIDIA_BASE, api_key=api_key, timeout=300.0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": system_prompt}],
            temperature=0.2,
            top_p=0.95,
            max_tokens=max_tokens,
            stream=False,
        )
        raw = response.choices[0].message.content or ""
        return raw, "ok"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", "error"


# ---------------------------------------------------------------------------
# Phase 3 — Signal parser
# ---------------------------------------------------------------------------

def parse_llm_signals(
    raw_response:        str,
    run_id:              int,
    company_id:          int,
    item_id_by_part_num: dict[str, int],
) -> list[AgentSignal]:
    """
    Extract JSON array from raw LLM response and return AgentSignal objects.
    On any parse failure, returns a single error signal.
    """
    # Try to find a JSON array in the response (handle preamble/postamble)
    text = raw_response.strip()
    start = text.find("[")
    end   = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return [_error_signal(run_id, company_id, "LLM did not return a JSON array.",
                              raw_response[:500])]

    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        return [_error_signal(run_id, company_id, f"JSON parse error: {e}",
                              text[start:start + 300])]

    if not isinstance(data, list):
        return [_error_signal(run_id, company_id, "LLM returned JSON but not an array.", "")]

    signals = []
    for obj in data:
        if not isinstance(obj, dict):
            continue

        # Validate and sanitise signal_type and severity
        sig_type = str(obj.get("signal_type", "portfolio")).strip().lower()
        if sig_type not in VALID_SIGNAL_TYPES:
            sig_type = "portfolio"

        severity = str(obj.get("severity", "medium")).strip().lower()
        if severity not in VALID_SEVERITIES:
            severity = "medium"

        part_num = str(obj.get("part_number", "PORTFOLIO")).strip()[:50]
        title    = str(obj.get("title", "")).strip()[:120]
        detail   = str(obj.get("detail", "")).strip()
        rec      = str(obj.get("recommendation", "")).strip()
        m_name   = str(obj.get("metric_name", "")).strip()[:100]

        try:
            m_value = float(obj["metric_value"]) if obj.get("metric_value") is not None else None
        except (TypeError, ValueError):
            m_value = None
        try:
            m_thresh = float(obj["metric_threshold"]) if obj.get("metric_threshold") is not None else None
        except (TypeError, ValueError):
            m_thresh = None

        # Resolve item_id — None for portfolio-level signals
        item_id = item_id_by_part_num.get(part_num)

        signals.append(AgentSignal(
            run_id          = run_id,
            company_id      = company_id,
            item_id         = item_id,
            part_number     = part_num,
            signal_type     = sig_type,
            severity        = severity,
            title           = title or f"{sig_type} signal for {part_num}",
            detail          = detail or "No detail provided.",
            recommendation  = rec,
            metric_name     = m_name,
            metric_value    = m_value,
            metric_threshold= m_thresh,
        ))

    if not signals:
        return [_error_signal(run_id, company_id, "LLM returned an empty array.", "")]

    # Sort: critical first, then high, medium, low, info
    _order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    signals.sort(key=lambda s: _order.get(s.severity, 5))
    return signals


def _error_signal(run_id: int, company_id: int, msg: str, detail: str) -> AgentSignal:
    return AgentSignal(
        run_id=run_id, company_id=company_id, part_number="PORTFOLIO",
        signal_type="portfolio", severity="info",
        title=f"Agent parse error: {msg[:80]}",
        detail=detail or msg,
        recommendation="Check the raw LLM response in the run detail panel.",
    )


# ---------------------------------------------------------------------------
# Orchestrator — public entry point
# ---------------------------------------------------------------------------

def run_inventory_agent(
    company_id: int,
    user_id:    int,
    model:      str = "deepseek-ai/deepseek-v3-0324",
    api_key:    str = "",
) -> tuple[AgentRun, list[AgentSignal]]:
    """
    Full agent orchestration. Creates an AgentRun, collects data, calls the LLM,
    parses and persists signals, then updates the run record.

    Returns (AgentRun, [AgentSignal]) — all already committed to the DB.
    """
    if not isinstance(company_id, int) or company_id is None:
        raise ValueError("company_id must be a non-None integer")

    session = SessionLocal()
    t_start = time.time()

    # Create run record
    run = AgentRun(
        company_id    = company_id,
        triggered_by  = user_id,
        model_used    = model.strip()[:200],
        status        = "running",
    )
    session.add(run)
    session.flush()   # get run.id
    run_id = run.id

    try:
        # Phase 1 — build context
        context_text, raw_data = build_agent_context(company_id)

        # Phase 2 — call LLM
        raw_response, status = call_nvidia_nim(
            context=context_text, model=model, api_key=api_key
        )

        # Phase 3 — parse signals
        item_id_map = {r["part_number"]: r["id"] for r in raw_data.get("snapshot", [])}
        signals = parse_llm_signals(raw_response, run_id, company_id, item_id_map)

        # Persist signals
        for sig in signals:
            session.add(sig)

        # Update run record
        duration = time.time() - t_start
        run.status            = "completed" if status == "ok" else "failed"
        run.items_analysed    = len(raw_data.get("snapshot", []))
        run.signals_generated = len(signals)
        run.duration_seconds  = round(duration, 2)
        run.context_snapshot  = context_text[:8000]   # truncate for storage
        run.llm_raw_response  = raw_response[:16000]
        if status == "error":
            run.error_message = raw_response[:500]

        session.commit()

        # Re-query signals after commit so relationships are loaded
        persisted_signals = (session.query(AgentSignal)
                             .filter(AgentSignal.run_id == run_id)
                             .order_by(AgentSignal.id)
                             .all())
        # Detach safely
        result_run = {c.name: getattr(run, c.name) for c in run.__table__.columns}
        result_signals = [
            {c.name: getattr(s, c.name) for c in s.__table__.columns}
            for s in persisted_signals
        ]
        return result_run, result_signals

    except Exception as exc:
        try:
            run.status        = "failed"
            run.error_message = str(exc)[:500]
            run.duration_seconds = round(time.time() - t_start, 2)
            session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()
