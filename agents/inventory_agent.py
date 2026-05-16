"""
Inventory Manager Agent — core module.

Architecture: 7 sequential focused LLM calls, one per analysis category.
Each call receives a single skill file + focused data slice and returns signals
tagged with that category. Signals accumulate in the DB as each analysis completes.

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
from typing import Callable, Optional

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

# ---------------------------------------------------------------------------
# Analysis categories — one LLM call per category
# ---------------------------------------------------------------------------

ANALYSIS_CATEGORIES = [
    {
        "key":        "data_quality",
        "label":      "Data Quality",
        "skill_file": "skill_01_data_quality.md",
        "data_keys":  ["snapshot", "data_quality"],
        "description": "Checks for missing costs, zero ADU/DLT on buffered items, zone inconsistencies, and missing suppliers.",
    },
    {
        "key":        "buffer_nfp",
        "label":      "Buffer & NFP Analysis",
        "skill_file": "skill_02_ddmrp_buffer_analysis.md",
        "data_keys":  ["alarms", "low_nfp", "snapshot"],
        "description": "Identifies items in execution alarm (red/dark_red), low net flow position, and buffer sizing issues.",
    },
    {
        "key":        "abc_xyz",
        "label":      "ABC/XYZ Policy",
        "skill_file": "skill_03_abc_xyz_policy.md",
        "data_keys":  ["abc_xyz", "snapshot"],
        "description": "Reviews buffer policy alignment against ABC/XYZ classification matrix.",
    },
    {
        "key":        "demand_variability",
        "label":      "Demand Variability",
        "skill_file": "skill_04_demand_variability.md",
        "data_keys":  ["demand_trends", "snapshot"],
        "description": "Detects items with ADU divergence >25%, high CV, or shifting demand trends.",
    },
    {
        "key":        "safety_stock",
        "label":      "Safety Stock Optimisation",
        "skill_file": "skill_05_safety_stock_optimization.md",
        "data_keys":  ["safety_gaps", "buffer_issues", "snapshot"],
        "description": "Flags items below TOR (safety stock gap) and buffers misaligned with ADU/DLT.",
    },
    {
        "key":        "overstock",
        "label":      "Overstock & Excess",
        "skill_file": "skill_06_overstock_and_excess.md",
        "data_keys":  ["overstock", "snapshot"],
        "description": "Identifies items where on_hand > TOG and quantifies excess cash tied up.",
    },
    {
        "key":        "supplier_risk",
        "label":      "Supplier Risk",
        "skill_file": "skill_07_supplier_risk.md",
        "data_keys":  ["supplier_risk", "snapshot"],
        "description": "Highlights low-reliability suppliers and LTF/DLT configuration issues.",
    },
    {
        "key":        "value_reduction",
        "label":      "Inventory Value Reduction",
        "skill_file": "skill_08_inventory_value_reduction.md",
        "data_keys":  ["value_summary", "overstock", "safety_gaps", "abc_xyz",
                       "supplier_risk", "demand_trends", "snapshot"],
        "description": "Quantifies cash tied up in inventory and proposes ranked cash-release levers.",
    },
]

_CATEGORY_BY_KEY = {c["key"]: c for c in ANALYSIS_CATEGORIES}

# ---------------------------------------------------------------------------
# Focused system prompt (single-analysis variant)
# ---------------------------------------------------------------------------

_FOCUSED_SYSTEM_PROMPT = """\
You are an autonomous Inventory Manager Agent embedded in a DDMRP supply chain application.

You are performing ONE focused analysis: **{analysis_label}**.
{analysis_description}

The domain knowledge and rules for THIS analysis are in the SKILL section below.
Analyse ONLY the data provided in the INVENTORY DATA section.

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
7. Generate between 1 and 15 signals focused on THIS analysis category only.
   Prioritise critical first, then high, then medium.
8. Every recommendation must name the specific item, state the action, and quantify the impact.
   BAD: "Optimize inventory."
   GOOD: "Reduce safety stock for ITEM-001 from 500 to 280 units — demand CV=0.22, coverage 210 days vs target 90. Cash release: €11,000."
9. Never recommend reducing safety stock on an item currently in red or dark_red execution.
10. Do not invent data. If a metric is not available, state that clearly in the detail.
11. If there are no findings for this analysis, return a single INFO signal with:
    signal_type="portfolio", severity="info", part_number="PORTFOLIO",
    title="No issues found in {analysis_label}", detail="All checked items pass this analysis."

=== SKILL: {skill_name} ===
{skill_content}

--- INVENTORY DATA ({analysis_label}) ---
{context}
--- END DATA ---
"""


# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------

def _load_skill(skill_file: str) -> tuple[str, str]:
    """Load a single skill file. Returns (skill_name, content)."""
    path = _SKILLS_DIR / skill_file
    if not path.exists():
        return skill_file.replace(".md", ""), "(skill file not found)"
    return path.stem, path.read_text()


# ---------------------------------------------------------------------------
# Phase 1 — Data tool functions (all company-scoped)
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


def get_low_nfp_items(company_id: int) -> list[dict]:
    """Items where NFP < top_of_yellow (order signal active)."""
    snap = get_inventory_snapshot(company_id)
    result = []
    for r in snap:
        if r["has_buffer"] and r["top_of_yellow"] > 0 and r["nfp"] < r["top_of_yellow"]:
            r["nfp_pct_of_tor"] = (r["nfp"] / r["top_of_red"] * 100) if r["top_of_red"] > 0 else 0.0
            result.append(r)
    return sorted(result, key=lambda x: x.get("nfp_pct_of_tor", 0))


def get_overstock_items(company_id: int) -> list[dict]:
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
        if abs(pct_diff) > 20:
            r["calc_tor"] = round(calc_tor, 2)
            r["pct_diff"] = round(pct_diff, 1)
            r["stale"]    = abs(pct_diff) > 40
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
    """Items whose supplier has reliability < 90%."""
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
                continue
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
        items    = session.query(Item).filter(Item.company_id == company_id).all()
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


def get_inventory_value_summary(company_id: int) -> dict:
    """
    Headline € figures for the company's inventory.

    Returns a dict with:
      - on_hand_value:        sum of on_hand × unit_cost
      - annual_usage_value:   sum of adu × 365 × unit_cost
      - avg_inventory_target: sum of (TOR + Green/2) × unit_cost
      - excess_value:         sum of (on_hand - TOG)+ × unit_cost
      - gap_value:            sum of (TOR - on_hand)+ × unit_cost
      - value_by_abc:         {"A": €, "B": €, "C": €}
      - value_by_color:       {"green": €, "yellow": €, "red": €, "dark_red": €, "over_tog": €}
      - top_items_by_value:   first 10 items by on-hand value
    """
    snap = get_inventory_snapshot(company_id)
    abc  = get_abc_xyz_classification(company_id)
    abc_by_id = {r["id"]: r for r in abc}

    on_hand_val = 0.0
    annual_val  = 0.0
    avg_target  = 0.0
    excess_val  = 0.0
    gap_val     = 0.0
    by_abc:   dict[str, float] = {"A": 0.0, "B": 0.0, "C": 0.0}
    by_color: dict[str, float] = {}
    items_rows = []

    for r in snap:
        unit_cost = r["unit_cost"] or 0.0
        oh_val    = (r["on_hand"]    or 0.0) * unit_cost
        ann_val   = (r["adu"]        or 0.0) * 365 * unit_cost
        on_hand_val += oh_val
        annual_val  += ann_val

        if r["has_buffer"]:
            tor   = r["top_of_red"]    or 0.0
            green_zone = (r["top_of_green"] or 0.0) - (r["top_of_yellow"] or 0.0)
            avg_target += (tor + green_zone / 2.0) * unit_cost
            excess_val += max(0.0, (r["on_hand"] or 0.0) - (r["top_of_green"] or 0.0)) * unit_cost
            gap_val    += max(0.0, tor - (r["on_hand"] or 0.0)) * unit_cost

        abc_cls = (abc_by_id.get(r["id"]) or {}).get("abc", "C")
        by_abc[abc_cls] = by_abc.get(abc_cls, 0.0) + oh_val

        color = r["execution_color"] or "green"
        by_color[color] = by_color.get(color, 0.0) + oh_val

        items_rows.append({
            "part_number":   r["part_number"],
            "description":   r["description"],
            "abc":           abc_cls,
            "on_hand_value": round(oh_val, 2),
            "annual_value":  round(ann_val, 2),
            "execution_color": color,
        })

    items_rows.sort(key=lambda x: x["on_hand_value"], reverse=True)

    return {
        "on_hand_value":        round(on_hand_val, 2),
        "annual_usage_value":   round(annual_val, 2),
        "avg_inventory_target": round(avg_target, 2),
        "excess_value":         round(excess_val, 2),
        "gap_value":            round(gap_val, 2),
        "value_by_abc":         {k: round(v, 2) for k, v in by_abc.items()},
        "value_by_color":       {k: round(v, 2) for k, v in by_color.items()},
        "top_items_by_value":   items_rows[:10],
        "total_items":          len(snap),
    }


# ---------------------------------------------------------------------------
# Phase 1 — Collect all raw data once
# ---------------------------------------------------------------------------

def collect_raw_data(company_id: int) -> dict:
    """
    Gather all data tool outputs upfront. Returns a dict keyed by data_key.
    Called once before the 8-analysis loop to avoid redundant DB queries.
    """
    snap = get_inventory_snapshot(company_id)
    return {
        "snapshot":      snap,
        "alarms":        get_execution_alarms(company_id),
        "low_nfp":       get_low_nfp_items(company_id),
        "overstock":     get_overstock_items(company_id),
        "demand_trends": get_demand_trends(company_id),
        "safety_gaps":   get_safety_stock_gaps(company_id),
        "buffer_issues": get_buffer_sizing_issues(company_id),
        "data_quality":  get_data_quality_issues(company_id),
        "supplier_risk": get_supplier_risk_items(company_id),
        "abc_xyz":       get_abc_xyz_classification(company_id),
        "value_summary": get_inventory_value_summary(company_id),
    }


# ---------------------------------------------------------------------------
# Phase 2a — Focused context builder (per-analysis)
# ---------------------------------------------------------------------------

def build_focused_context(category_key: str, raw_data: dict) -> str:
    """
    Build a context string containing only the data relevant to one analysis.
    Keeps tokens focused and avoids overwhelming the LLM with unrelated sections.
    """
    cat    = _CATEGORY_BY_KEY[category_key]
    lines  = []
    snap   = raw_data.get("snapshot", [])
    now    = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    total  = len(snap)
    buf_ct = sum(1 for r in snap if r["has_buffer"])

    lines.append(f"Generated: {now}  |  Total items: {total}  |  Buffered: {buf_ct}")
    lines.append("")

    data_keys = cat["data_keys"]

    # ── SNAPSHOT summary (always include a mini snapshot) ────────────────
    if "snapshot" in data_keys and snap:
        lines.append("=== ITEM SUMMARY ===")
        lines.append(f"{'Part #':<20} {'Cost€':>8} {'On-Hand':>10} {'ADU':>8} {'DLT':>5} {'Exec':>8} {'HasBuf':>7}")
        for r in snap[:30]:
            lines.append(
                f"{r['part_number']:<20} {r['unit_cost']:>8.2f} {r['on_hand']:>10.1f} "
                f"{r['adu']:>8.3f} {r['dlt']:>5.0f} {r['execution_color']:>8} "
                f"{'Y' if r['has_buffer'] else 'N':>7}"
            )
        if len(snap) > 30:
            lines.append(f"  ... ({len(snap) - 30} more items not shown)")
        lines.append("")

    # ── DATA QUALITY ─────────────────────────────────────────────────────
    if "data_quality" in data_keys:
        dq = raw_data.get("data_quality", [])
        lines.append("=== DATA QUALITY ISSUES ===")
        if dq:
            lines.append(f"{'Part #':<20} {'Issues'}")
            for r in dq[:25]:
                lines.append(f"{r['part_number']:<20} {', '.join(r['issues'])}")
        else:
            lines.append("No data quality issues detected.")
        lines.append("")

    # ── EXECUTION ALARMS ─────────────────────────────────────────────────
    if "alarms" in data_keys:
        alarms = raw_data.get("alarms", [])
        lines.append("=== EXECUTION ALARMS (red / dark_red) ===")
        if alarms:
            lines.append(f"{'Part #':<20} {'Color':<10} {'On-Hand':>10} {'TOR':>10} {'Status%':>8} {'ADU':>8} {'DLT':>6} {'Cost€':>8}")
            for r in alarms[:20]:
                lines.append(
                    f"{r['part_number']:<20} {r['execution_color']:<10} "
                    f"{r['on_hand']:>10.1f} {r['top_of_red']:>10.1f} "
                    f"{r['buffer_status_pct']:>8.1f} {r['adu']:>8.3f} "
                    f"{r['dlt']:>6.0f} {r['unit_cost']:>8.2f}"
                )
        else:
            lines.append("No execution alarms — all buffered items in yellow or green.")
        lines.append("")

    # ── LOW NFP ──────────────────────────────────────────────────────────
    if "low_nfp" in data_keys:
        low = raw_data.get("low_nfp", [])
        lines.append("=== LOW NET FLOW POSITION (NFP < TOY) ===")
        if low:
            lines.append(f"{'Part #':<20} {'NFP':>10} {'TOY':>10} {'TOG':>10} {'NFP%TOR':>8}")
            for r in low[:20]:
                lines.append(
                    f"{r['part_number']:<20} {r['nfp']:>10.1f} "
                    f"{r['top_of_yellow']:>10.1f} {r['top_of_green']:>10.1f} "
                    f"{r.get('nfp_pct_of_tor', 0):>8.1f}"
                )
        else:
            lines.append("No items with NFP below TOY.")
        lines.append("")

    # ── OVERSTOCK ────────────────────────────────────────────────────────
    if "overstock" in data_keys:
        ov = raw_data.get("overstock", [])
        lines.append("=== OVERSTOCK (on_hand > TOG) ===")
        if ov:
            lines.append(f"{'Part #':<20} {'On-Hand':>10} {'TOG':>10} {'Excess Units':>13} {'Excess €':>12} {'OH%TOG':>8}")
            for r in ov[:15]:
                lines.append(
                    f"{r['part_number']:<20} {r['on_hand']:>10.1f} "
                    f"{r['top_of_green']:>10.1f} {r['excess_units']:>13.1f} "
                    f"{r['excess_value']:>12.2f} {r['on_hand_vs_tog']:>8.1f}%"
                )
        else:
            lines.append("No items above TOG.")
        lines.append("")

    # ── SAFETY STOCK GAPS ────────────────────────────────────────────────
    if "safety_gaps" in data_keys:
        sg = raw_data.get("safety_gaps", [])
        lines.append("=== SAFETY STOCK GAPS (on_hand < TOR) ===")
        if sg:
            lines.append(f"{'Part #':<20} {'On-Hand':>10} {'TOR':>10} {'Gap Units':>10} {'Gap €':>12} {'Days2SO':>8}")
            for r in sg[:15]:
                d2s = r["days_until_stockout"]
                lines.append(
                    f"{r['part_number']:<20} {r['on_hand']:>10.1f} "
                    f"{r['top_of_red']:>10.1f} {r['gap_units']:>10.1f} "
                    f"{r['gap_value']:>12.2f} {d2s:>8.1f}"
                )
        else:
            lines.append("No items below TOR.")
        lines.append("")

    # ── BUFFER SIZING ISSUES ─────────────────────────────────────────────
    if "buffer_issues" in data_keys:
        bi = raw_data.get("buffer_issues", [])
        lines.append("=== BUFFER SIZING ISSUES (TOR diverges >20% from ADU×DLT×(LTF+VF)) ===")
        if bi:
            lines.append(f"{'Part #':<20} {'Curr TOR':>10} {'Calc TOR':>10} {'Diff%':>7} {'Stale':>6}")
            for r in bi[:15]:
                lines.append(
                    f"{r['part_number']:<20} {r['top_of_red']:>10.1f} "
                    f"{r['calc_tor']:>10.1f} {r['pct_diff']:>7.1f} {str(r['stale']):>6}"
                )
        else:
            lines.append("No significant buffer sizing misalignments.")
        lines.append("")

    # ── DEMAND TRENDS ────────────────────────────────────────────────────
    if "demand_trends" in data_keys:
        trends = raw_data.get("demand_trends", {})
        snap_map = {r["id"]: r for r in snap}
        notable = [
            (iid, t) for iid, t in trends.items()
            if t["total_entries"] >= 5 and (t["adu_divergence_pct"] > 20 or t["cv"] > 0.8)
        ]
        lines.append("=== DEMAND TRENDS (90-day, notable items only) ===")
        if notable:
            lines.append(f"{'Part #':<20} {'Stored ADU':>11} {'Recent ADU':>11} {'Div%':>6} {'CV':>6} {'Trend':>8}")
            for iid, t in sorted(notable, key=lambda x: x[1]["adu_divergence_pct"], reverse=True)[:15]:
                pn = snap_map.get(iid, {}).get("part_number", str(iid))
                lines.append(
                    f"{pn:<20} {t['stored_adu']:>11.3f} {t['recent_adu']:>11.3f} "
                    f"{t['adu_divergence_pct']:>6.1f} {t['cv']:>6.2f} {t['trend_direction']:>8}"
                )
        else:
            lines.append("No significant demand trend deviations detected.")
        lines.append("")

    # ── ABC/XYZ ──────────────────────────────────────────────────────────
    if "abc_xyz" in data_keys:
        abc = raw_data.get("abc_xyz", [])
        lines.append("=== ABC/XYZ CLASSIFICATION ===")
        if abc:
            from collections import Counter
            cell_counts: Counter = Counter()
            cell_values: dict    = defaultdict(float)
            for r in abc:
                key = r["acvs"]
                cell_counts[key] += 1
                cell_values[key] += r.get("annual_value", 0)
            lines.append(f"{'Cell':<8} {'Items':>6} {'Annual Value €':>16}")
            for cell in ["A-X", "A-Y", "A-Z", "B-X", "B-Y", "B-Z", "C-X", "C-Y", "C-Z"]:
                lines.append(f"{cell:<8} {cell_counts.get(cell, 0):>6} {cell_values.get(cell, 0):>16,.0f}")
            lines.append("")
            lines.append(f"{'Part #':<20} {'ABC':>4} {'XYZ':>4} {'Cell':>6} {'CV':>6} {'Annual €':>12}")
            for r in sorted(abc, key=lambda x: x.get("annual_value", 0), reverse=True)[:25]:
                lines.append(
                    f"{r['part_number']:<20} {r['abc']:>4} {r['xyz']:>4} {r['acvs']:>6} "
                    f"{r['cv']:>6.2f} {r.get('annual_value', 0):>12,.0f}"
                )
        else:
            lines.append("No ABC/XYZ data available.")
        lines.append("")

    # ── SUPPLIER RISK ────────────────────────────────────────────────────
    if "supplier_risk" in data_keys:
        sr = raw_data.get("supplier_risk", [])
        lines.append("=== SUPPLIER RISK (reliability < 90%) ===")
        if sr:
            lines.append(f"{'Part #':<20} {'Supplier':<25} {'Rel%':>6} {'Exec':>8} {'DLT':>5} {'SupLT':>6} {'LTGap':>7}")
            for r in sr[:15]:
                lines.append(
                    f"{r['part_number']:<20} {r['supplier_name'][:24]:<25} "
                    f"{r['reliability_pct']:>6.0f} {r['execution_color']:>8} "
                    f"{r['dlt']:>5.0f} {r['supplier_lt_days']:>6} "
                    f"{r['dlt_vs_sup_lt_gap']:>7.0f}"
                )
        else:
            lines.append("No supplier risk items (all suppliers ≥ 90% reliability).")
        lines.append("")

    # ── INVENTORY VALUE SUMMARY ─────────────────────────────────────────
    if "value_summary" in data_keys:
        vs = raw_data.get("value_summary") or {}
        lines.append("=== INVENTORY VALUE SUMMARY (€) ===")
        if vs:
            lines.append(f"  On-hand value:        € {vs.get('on_hand_value', 0):>14,.2f}")
            lines.append(f"  Annual usage value:   € {vs.get('annual_usage_value', 0):>14,.2f}")
            lines.append(f"  Avg Inventory Target: € {vs.get('avg_inventory_target', 0):>14,.2f}")
            lines.append(f"  Excess (above TOG):   € {vs.get('excess_value', 0):>14,.2f}")
            lines.append(f"  Gap (below TOR):      € {vs.get('gap_value', 0):>14,.2f}")
            by_abc = vs.get("value_by_abc", {}) or {}
            lines.append(
                f"  By ABC:  A=€{by_abc.get('A', 0):,.0f}  "
                f"B=€{by_abc.get('B', 0):,.0f}  "
                f"C=€{by_abc.get('C', 0):,.0f}"
            )
            by_color = vs.get("value_by_color", {}) or {}
            color_str = "  ".join(f"{k}=€{v:,.0f}" for k, v in by_color.items())
            if color_str:
                lines.append(f"  By execution color:  {color_str}")
            top = vs.get("top_items_by_value", []) or []
            if top:
                lines.append("  Top items by on-hand value:")
                lines.append(f"    {'Part #':<20} {'ABC':>4} {'On-hand €':>14} {'Annual €':>14}")
                for r in top:
                    lines.append(
                        f"    {r['part_number']:<20} {r['abc']:>4} "
                        f"{r['on_hand_value']:>14,.2f} {r['annual_value']:>14,.2f}"
                    )
        else:
            lines.append("  No value summary available.")
        lines.append("")

    ctx = "\n".join(lines)
    # Cap at ~8k chars per analysis to keep token usage reasonable
    if len(ctx) > 8_000:
        ctx = ctx[:8_000] + "\n\n[...context truncated...]"
    return ctx


# ---------------------------------------------------------------------------
# Phase 2b — Single focused LLM call
# ---------------------------------------------------------------------------

def call_nvidia_nim_focused(
    category_key: str,
    focused_context: str,
    model: str = "deepseek-ai/deepseek-v3-0324",
    api_key: str = "",
    max_tokens: int = 4096,
) -> tuple[str, str]:
    """
    Call the NVIDIA NIM API for one analysis category.
    Returns (raw_response_text, status) where status is 'ok' or 'error'.
    """
    if not api_key:
        return "No NVIDIA_API_KEY configured.", "error"

    cat          = _CATEGORY_BY_KEY[category_key]
    skill_name, skill_content = _load_skill(cat["skill_file"])

    prompt = _FOCUSED_SYSTEM_PROMPT.format(
        analysis_label=cat["label"],
        analysis_description=cat["description"],
        skill_name=skill_name,
        skill_content=skill_content,
        context=focused_context,
    )

    try:
        client = OpenAI(base_url=_NVIDIA_BASE, api_key=api_key, timeout=300.0)
        response = client.chat.completions.create(
            model=model.strip()[:100],
            messages=[{"role": "user", "content": prompt}],
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

def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences if present."""
    import re
    m = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_json_array(text: str) -> Optional[list]:
    """
    Best-effort extraction of a JSON array from raw LLM output.

    Tries, in order:
      1. Direct json.loads on the whole stripped+fence-stripped string
         (works when the model obeys the prompt exactly).
      2. If the result is a dict that wraps the array under "signals"
         (a common LLM mistake), unwrap it.
      3. Find the first '[' and bracket-balance forward to the matching ']'
         — this skips stray '[' / ']' that appear inside prose preambles
         or inside strings.
    Returns the parsed list, or None if nothing JSON-array-shaped was found.
    """
    stripped = _strip_markdown_fences(text.strip())

    # Attempt 1 — direct parse
    try:
        obj = json.loads(stripped)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("signals", "data", "results", "items"):
                if isinstance(obj.get(key), list):
                    return obj[key]
    except json.JSONDecodeError:
        pass

    # Attempt 2 — for each '[' position in the text, bracket-balance forward
    # to the matching ']' and try to parse the slice. The first slice that
    # parses to a list wins. Skips stray '[...]' inside prose preambles.
    pos = 0
    while True:
        start = stripped.find("[", pos)
        if start == -1:
            return None
        depth     = 0
        in_string = False
        escape    = False
        matched_end = -1
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    matched_end = i
                    break
        if matched_end == -1:
            return None
        try:
            obj = json.loads(stripped[start:matched_end + 1])
            if isinstance(obj, list):
                return obj
        except json.JSONDecodeError:
            pass
        pos = start + 1


def parse_llm_signals(
    raw_response:        str,
    run_id:              int,
    company_id:          int,
    item_id_by_part_num: dict[str, int],
    analysis_category:   str = "",
) -> list[AgentSignal]:
    """
    Extract JSON array from raw LLM response and return AgentSignal objects
    tagged with the given analysis_category.
    On any parse failure returns a single error signal.
    """
    data = _extract_json_array(raw_response or "")
    if data is None:
        return [_error_signal(run_id, company_id, analysis_category,
                              "LLM did not return a JSON array.",
                              (raw_response or "")[:500])]
    if not isinstance(data, list):
        return [_error_signal(run_id, company_id, analysis_category,
                              "LLM returned JSON but not an array.", "")]

    signals = []
    for obj in data:
        if not isinstance(obj, dict):
            continue

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

        item_id = item_id_by_part_num.get(part_num)

        signals.append(AgentSignal(
            run_id            = run_id,
            company_id        = company_id,
            item_id           = item_id,
            part_number       = part_num,
            analysis_category = analysis_category,
            signal_type       = sig_type,
            severity          = severity,
            title             = title or f"{sig_type} signal for {part_num}",
            detail            = detail or "No detail provided.",
            recommendation    = rec,
            metric_name       = m_name,
            metric_value      = m_value,
            metric_threshold  = m_thresh,
        ))

    if not signals:
        return [_error_signal(run_id, company_id, analysis_category,
                              "LLM returned an empty array.", "")]

    _order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    signals.sort(key=lambda s: _order.get(s.severity, 5))
    return signals


def _error_signal(
    run_id: int, company_id: int,
    analysis_category: str, msg: str, detail: str,
) -> AgentSignal:
    return AgentSignal(
        run_id=run_id, company_id=company_id,
        part_number="PORTFOLIO",
        analysis_category=analysis_category,
        signal_type="portfolio", severity="info",
        title=f"Agent parse error: {msg[:80]}",
        detail=detail or msg,
        recommendation="Check the raw LLM response in the run detail panel.",
    )


# ---------------------------------------------------------------------------
# Orchestrator — public entry point
# ---------------------------------------------------------------------------

def run_inventory_agent(
    company_id:        int,
    user_id:           int,
    model:             str = "deepseek-ai/deepseek-v3-0324",
    api_key:           str = "",
    progress_callback: Optional[Callable[[str, str, int], None]] = None,
) -> tuple[dict, list[dict]]:
    """
    Full agent orchestration — 8 sequential focused analyses.

    progress_callback(category_key, status, n_signals):
        Called after each analysis completes.
        status = "running" | "done" | "error"

    Returns (run_dict, signal_dicts) — plain dicts, session already closed.
    """
    if not isinstance(company_id, int) or company_id is None:
        raise ValueError("company_id must be a non-None integer")

    session = SessionLocal()
    t_start = time.time()

    planned_keys = [c["key"] for c in ANALYSIS_CATEGORIES]

    # Create run record
    run = AgentRun(
        company_id       = company_id,
        triggered_by     = user_id,
        model_used       = model.strip()[:200],
        status           = "running",
        analyses_planned = json.dumps(planned_keys),
        analyses_done    = json.dumps([]),
    )
    session.add(run)
    session.flush()
    run_id = run.id

    try:
        # Phase 1 — collect all data once
        raw_data    = collect_raw_data(company_id)
        item_id_map = {r["part_number"]: r["id"] for r in raw_data.get("snapshot", [])}
        total_items = len(raw_data["snapshot"])

        all_signal_dicts: list[dict] = []
        done_keys: list[str]         = []
        total_signals                = 0

        # Phase 2+3 — one focused LLM call per analysis
        for cat in ANALYSIS_CATEGORIES:
            cat_key   = cat["key"]
            cat_label = cat["label"]

            if progress_callback:
                progress_callback(cat_key, "running", 0)

            focused_ctx = build_focused_context(cat_key, raw_data)
            raw_resp, status = call_nvidia_nim_focused(
                category_key=cat_key,
                focused_context=focused_ctx,
                model=model,
                api_key=api_key,
            )

            if status == "error":
                # API-level failure (timeout, auth, quota, bad model slug, ...).
                # Surface the actual error rather than running it through the
                # JSON parser, which would only mis-report it as a parse error.
                signals = [_error_signal(
                    run_id            = run_id,
                    company_id        = company_id,
                    analysis_category = cat_key,
                    msg               = f"API call failed for {cat_label}",
                    detail            = raw_resp[:500],
                )]
            else:
                signals = parse_llm_signals(
                    raw_response=raw_resp,
                    run_id=run_id,
                    company_id=company_id,
                    item_id_by_part_num=item_id_map,
                    analysis_category=cat_key,
                )

            # Persist signals for this analysis immediately
            for sig in signals:
                session.add(sig)
            session.flush()

            n_sigs       = len(signals)
            total_signals += n_sigs
            done_keys.append(cat_key)

            # Update run progress
            run.analyses_done    = json.dumps(done_keys)
            run.signals_generated = total_signals
            session.flush()

            if progress_callback:
                cb_status = "done" if status == "ok" else "error"
                progress_callback(cat_key, cb_status, n_sigs)

        # Finalise run record
        duration = time.time() - t_start
        run.status           = "completed"
        run.items_analysed   = total_items
        run.signals_generated = total_signals
        run.duration_seconds = round(duration, 2)
        run.context_snapshot = json.dumps({k: len(v) if isinstance(v, (list, dict)) else str(v)
                                           for k, v in raw_data.items()})[:4000]
        session.commit()

        # Re-query signals after commit
        persisted = (session.query(AgentSignal)
                     .filter(AgentSignal.run_id == run_id)
                     .order_by(AgentSignal.analysis_category, AgentSignal.id)
                     .all())
        result_run     = {c.name: getattr(run, c.name) for c in run.__table__.columns}
        result_signals = [{c.name: getattr(s, c.name) for c in s.__table__.columns}
                          for s in persisted]
        return result_run, result_signals

    except Exception as exc:
        try:
            run.status           = "failed"
            run.error_message    = str(exc)[:500]
            run.duration_seconds = round(time.time() - t_start, 2)
            session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()
