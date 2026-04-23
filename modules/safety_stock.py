"""
Safety Stock & EOQ — Streamlit page.

Calculates safety stock, reorder point and economic order quantity for every
item using one of several industry-standard formulas.  Uses demand and supply
history already collected by the app to derive statistical variability.

Models available
----------------
  1. Basic Rule of Thumb        SS = ADU × DLT × SafetyFactor
  2. Demand-Variability Only    SS = Z × σ_d × √DLT
  3. King's Combined Formula    SS = Z × √(DLT × σ_d² + ADU² × σ_LT²)      ⭐
"""

import math
import statistics
from dataclasses import dataclass
from typing import List, Optional

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from database.db import get_session, Item, DemandEntry, SupplyEntry


# ─────────────────────────────────────────────────────────────────────────────
# Service level → Z factor (standard normal inverse CDF, common values)
# ─────────────────────────────────────────────────────────────────────────────

_Z_TABLE = [
    (50.0, 0.00), (75.0, 0.67), (80.0, 0.84), (85.0, 1.04),
    (90.0, 1.28), (92.5, 1.44), (95.0, 1.65), (97.5, 1.96),
    (98.0, 2.05), (99.0, 2.33), (99.5, 2.58), (99.9, 3.09),
]


def service_level_to_z(sl_pct: float) -> float:
    """Linear-interpolate Z from a table of common cycle service levels."""
    sl_pct = max(50.0, min(99.9, sl_pct))
    for (s1, z1), (s2, z2) in zip(_Z_TABLE[:-1], _Z_TABLE[1:]):
        if s1 <= sl_pct <= s2:
            if s2 == s1:
                return z1
            return z1 + (z2 - z1) * (sl_pct - s1) / (s2 - s1)
    return 1.65


# ─────────────────────────────────────────────────────────────────────────────
# Variability helpers
# ─────────────────────────────────────────────────────────────────────────────

def calculate_demand_std(item: Item, lookback_days: int = 90) -> float:
    """
    Standard deviation of daily actual demand over a lookback window.
    Days without any demand are counted as 0, so σ reflects intermittence.
    Falls back to ADU × 0.5 if fewer than 5 non-zero days.
    """
    from datetime import datetime, timedelta
    today = datetime.utcnow().date()
    start = today - timedelta(days=lookback_days)

    session = get_session()
    try:
        entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_type == "actual",
                DemandEntry.demand_date >= datetime.combine(start, datetime.min.time()),
                DemandEntry.demand_date <= datetime.combine(today,  datetime.max.time()),
            ).all()
        )
    finally:
        session.close()

    # Aggregate per day
    per_day = {}
    for e in entries:
        d = e.demand_date.date()
        per_day[d] = per_day.get(d, 0.0) + e.quantity

    # Build full daily series over the lookback (zeros for days with no demand)
    series = []
    for offset in range(lookback_days + 1):
        d = start + timedelta(days=offset)
        series.append(per_day.get(d, 0.0))

    non_zero = [x for x in series if x > 0]
    if len(non_zero) < 5 or len(series) < 2:
        return max(item.adu * 0.5, 0.0)

    try:
        return statistics.pstdev(series)
    except statistics.StatisticsError:
        return max(item.adu * 0.5, 0.0)


def calculate_lead_time_std(item: Item) -> float:
    """
    Std. deviation of actual lead times from supply history.
    Lead time per order = due_date − created_at (days).
    Falls back to DLT × 0.2 if fewer than 5 orders.
    """
    session = get_session()
    try:
        entries = session.query(SupplyEntry).filter_by(item_id=item.id).all()
    finally:
        session.close()

    lts = []
    for e in entries:
        if e.due_date and e.created_at:
            lt = (e.due_date - e.created_at).days
            if lt > 0:
                lts.append(lt)

    if len(lts) < 5:
        return max(item.dlt * 0.2, 0.0)

    try:
        return statistics.pstdev(lts)
    except statistics.StatisticsError:
        return max(item.dlt * 0.2, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SafetyStockResult:
    item_id: int
    part_number: str
    description: str
    category: str
    uom: str

    adu: float
    dlt: float
    unit_cost: float
    ordering_cost: float
    holding_cost_pct: float

    # Statistical inputs
    sigma_demand: float
    sigma_lt: float
    z: float
    model: str

    # Outputs
    safety_stock: float
    reorder_point: float
    eoq: float
    avg_cycle_stock: float              # EOQ / 2
    avg_inventory_qty: float            # cycle + safety
    avg_inventory_value: float          # qty × unit_cost
    annual_holding_cost: float          # avg qty × C × h
    annual_ordering_cost: float         # (D/EOQ) × K
    annual_total_cost: float            # holding + ordering

    # DDMRP comparison
    current_top_of_red: float
    delta_vs_ddmrp: float               # SS − TOR

    # Diagnostics
    demand_samples: int
    supply_samples: int
    warnings: list


# ─────────────────────────────────────────────────────────────────────────────
# Core calculations
# ─────────────────────────────────────────────────────────────────────────────

def _calc_ss(model: str, adu: float, dlt: float, z: float,
             sigma_d: float, sigma_lt: float, safety_factor: float) -> float:
    if model == "basic":
        return adu * dlt * safety_factor
    if model == "demand_only":
        return z * sigma_d * math.sqrt(max(dlt, 0.0))
    if model == "kings":
        return z * math.sqrt(max(dlt, 0.0) * sigma_d**2 + adu**2 * sigma_lt**2)
    return 0.0


def _calc_eoq(annual_demand: float, ordering_cost: float,
              holding_pct: float, unit_cost: float) -> float:
    denom = holding_pct * unit_cost
    if denom <= 0 or annual_demand <= 0 or ordering_cost <= 0:
        return 0.0
    return math.sqrt(2 * annual_demand * ordering_cost / denom)


def calculate_for_item(
    item: Item, *,
    model: str,
    service_level: float,
    safety_factor: float,
    default_ordering_cost: float,
    default_holding_pct: float,
    lookback_days: int,
) -> SafetyStockResult:
    """Run the selected SS model + EOQ for a single item."""
    from modules.buffer_engine import calculate_zones

    warnings = []
    adu = max(item.adu, 0.0)
    dlt = max(item.dlt, 0.0)

    # Resolve cost parameters (per-item override > global default)
    unit_cost = item.unit_cost or 0.0
    ordering_cost = item.ordering_cost if item.ordering_cost > 0 else default_ordering_cost
    holding_pct   = item.holding_cost_pct if item.holding_cost_pct > 0 else default_holding_pct

    if unit_cost <= 0:
        warnings.append("Unit cost = 0 — EOQ and inventory value cannot be calculated.")

    # Variability
    sigma_d  = calculate_demand_std(item, lookback_days=lookback_days)
    sigma_lt = calculate_lead_time_std(item)
    z = service_level_to_z(service_level)

    # Samples for data-quality indicator
    session = get_session()
    try:
        demand_samples = (
            session.query(DemandEntry)
            .filter_by(item_id=item.id, demand_type="actual").count()
        )
        supply_samples = (
            session.query(SupplyEntry).filter_by(item_id=item.id).count()
        )
    finally:
        session.close()

    if demand_samples < 10:
        warnings.append(f"Only {demand_samples} actual demand entries — σ_demand may be unreliable.")
    if supply_samples < 5 and model == "kings":
        warnings.append(f"Only {supply_samples} supply orders — σ_leadtime is approximated.")

    # Safety stock
    ss = _calc_ss(model, adu, dlt, z, sigma_d, sigma_lt, safety_factor)
    ss = max(ss, 0.0)

    # Reorder point
    rop = adu * dlt + ss

    # EOQ
    annual_demand = adu * 365.0
    eoq = _calc_eoq(annual_demand, ordering_cost, holding_pct, unit_cost)
    if item.min_order_qty and eoq < item.min_order_qty:
        eoq = item.min_order_qty

    # Inventory metrics
    avg_cycle = eoq / 2.0 if eoq > 0 else 0.0
    avg_qty   = avg_cycle + ss
    avg_value = avg_qty * unit_cost

    annual_holding = avg_qty * unit_cost * holding_pct if holding_pct > 0 else 0.0
    annual_orders  = (annual_demand / eoq) * ordering_cost if eoq > 0 and ordering_cost > 0 else 0.0
    annual_total   = annual_holding + annual_orders

    # DDMRP comparison
    zones = calculate_zones(item)
    delta = ss - zones.top_of_red

    return SafetyStockResult(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description,
        category=item.category,
        uom=item.unit_of_measure,
        adu=adu, dlt=dlt,
        unit_cost=unit_cost,
        ordering_cost=ordering_cost,
        holding_cost_pct=holding_pct,
        sigma_demand=round(sigma_d, 3),
        sigma_lt=round(sigma_lt, 3),
        z=round(z, 3),
        model=model,
        safety_stock=round(ss, 2),
        reorder_point=round(rop, 2),
        eoq=round(eoq, 2),
        avg_cycle_stock=round(avg_cycle, 2),
        avg_inventory_qty=round(avg_qty, 2),
        avg_inventory_value=round(avg_value, 2),
        annual_holding_cost=round(annual_holding, 2),
        annual_ordering_cost=round(annual_orders, 2),
        annual_total_cost=round(annual_total, 2),
        current_top_of_red=round(zones.top_of_red, 2),
        delta_vs_ddmrp=round(delta, 2),
        demand_samples=demand_samples,
        supply_samples=supply_samples,
        warnings=warnings,
    )


def calculate_for_all(*, model, service_level, safety_factor,
                      default_ordering_cost, default_holding_pct,
                      lookback_days) -> List[SafetyStockResult]:
    session = get_session()
    try:
        items = session.query(Item).order_by(Item.part_number).all()
    finally:
        session.close()

    results = []
    for it in items:
        try:
            results.append(calculate_for_item(
                it, model=model, service_level=service_level,
                safety_factor=safety_factor,
                default_ordering_cost=default_ordering_cost,
                default_holding_pct=default_holding_pct,
                lookback_days=lookback_days,
            ))
        except Exception as e:
            print(f"SS calc error for {it.part_number}: {e}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────────────────────────────────────────────

MODEL_OPTIONS = {
    "basic":        "Basic Rule of Thumb  (ADU × DLT × SafetyFactor)",
    "demand_only":  "Demand Variability Only  (Z × σ_d × √DLT)",
    "kings":        "King's Combined Formula  ⭐  (demand + lead-time variability)",
}

MODEL_DESCRIPTIONS = {
    "basic": (
        "The simplest method — safety stock is a flat fraction of the demand "
        "during lead time. Requires **no demand history**. Typically used when "
        "no statistical data is available."
    ),
    "demand_only": (
        "Classic textbook formula assuming constant lead time. Safety stock is "
        "proportional to the standard deviation of daily demand, the desired "
        "service level (Z factor) and the square root of lead time."
    ),
    "kings": (
        "Industry standard. Handles **both demand variability AND lead-time "
        "variability**. Recommended whenever you have at least 10 actual demand "
        "entries and 5 supply orders in history."
    ),
}


def show():
    st.header("🛡️ Safety Stock & EOQ")
    st.caption(
        "Calculates safety stock, reorder point and economic order quantity "
        "per item using the formula you select. Requires unit cost set in "
        "Material Master for EOQ and inventory-value outputs."
    )

    # ── Global parameters ────────────────────────────────────────────────────
    with st.expander("⚙️ Global parameters", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            service_level = st.number_input(
                "Service Level (%)", min_value=50.0, max_value=99.9,
                value=95.0, step=0.5,
                help="Cycle service level — probability of no stockout per cycle. 95% (Z=1.65) is standard.",
            )
            z_preview = service_level_to_z(service_level)
            st.caption(f"→ Z = **{z_preview:.2f}**")
        with c2:
            safety_factor = st.number_input(
                "Safety Factor (basic model)", min_value=0.1, max_value=2.0,
                value=0.5, step=0.1,
                help="Used only by the basic ADU × DLT × SF formula.",
            )
        with c3:
            default_ordering_cost = st.number_input(
                "Default Ordering Cost (€/order)", min_value=0.0,
                value=50.0, step=5.0,
                help="Used for EOQ when the item has no per-item override.",
            )
        with c4:
            default_holding_pct = st.number_input(
                "Default Holding Cost (% / year)", min_value=0.0, max_value=100.0,
                value=25.0, step=1.0,
                help="Annual inventory carrying cost as % of unit cost. 25% is typical.",
            ) / 100.0

        c5, c6 = st.columns([1, 3])
        with c5:
            lookback_days = st.number_input(
                "Demand lookback (days)", min_value=14, max_value=365,
                value=90, step=7,
                help="Window used to compute σ of daily demand.",
            )

    # ── Model selector ───────────────────────────────────────────────────────
    st.divider()
    st.markdown("**Step 1 — Choose a calculation model**")
    model = st.selectbox(
        "Safety stock formula",
        options=list(MODEL_OPTIONS.keys()),
        format_func=lambda k: MODEL_OPTIONS[k],
        index=2,
    )
    st.info(MODEL_DESCRIPTIONS[model])

    st.markdown("**Step 2 — Run the calculation**")
    if st.button("📊 Calculate Safety Stock & EOQ", type="primary"):
        with st.spinner("Analysing demand and supply history…"):
            results = calculate_for_all(
                model=model,
                service_level=service_level,
                safety_factor=safety_factor,
                default_ordering_cost=default_ordering_cost,
                default_holding_pct=default_holding_pct,
                lookback_days=int(lookback_days),
            )
        st.session_state["ss_results"] = results
        st.session_state["ss_model"]   = model

    results: Optional[List[SafetyStockResult]] = st.session_state.get("ss_results")
    if not results:
        st.info("Configure the parameters and click **Calculate Safety Stock & EOQ**.")
        return

    st.divider()
    _render_kpis(results)
    st.divider()
    _render_results_table(results)
    st.divider()
    _render_value_chart(results)
    st.divider()
    _render_ddmrp_comparison(results)


# ─────────────────────────────────────────────────────────────────────────────
# KPI strip
# ─────────────────────────────────────────────────────────────────────────────

def _render_kpis(results):
    total_items   = len(results)
    total_ss_qty  = sum(r.safety_stock for r in results)
    total_inv_val = sum(r.avg_inventory_value for r in results)
    total_hold    = sum(r.annual_holding_cost for r in results)
    total_order   = sum(r.annual_ordering_cost for r in results)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Items analysed",            total_items)
    c2.metric("Σ Safety Stock (units)",    f"{total_ss_qty:,.0f}")
    c3.metric("Σ Avg Inventory Value",     f"€ {total_inv_val:,.0f}")
    c4.metric("Σ Annual Holding Cost",     f"€ {total_hold:,.0f}")
    c5.metric("Σ Annual Ordering Cost",    f"€ {total_order:,.0f}")


# ─────────────────────────────────────────────────────────────────────────────
# Main results table
# ─────────────────────────────────────────────────────────────────────────────

def _render_results_table(results):
    st.subheader("Results by Item")

    rows = []
    for r in results:
        rows.append({
            "Part Number":     r.part_number,
            "Description":     r.description,
            "ADU":             r.adu,
            "DLT":             r.dlt,
            "σ demand":        r.sigma_demand,
            "σ LT":            r.sigma_lt,
            "Z":               r.z,
            "Safety Stock":    r.safety_stock,
            "Reorder Point":   r.reorder_point,
            "EOQ":             r.eoq,
            "Avg Inv Qty":     r.avg_inventory_qty,
            "Unit Cost (€)":   r.unit_cost,
            "Avg Inv Value €": r.avg_inventory_value,
            "Holding €/yr":    r.annual_holding_cost,
            "Ordering €/yr":   r.annual_ordering_cost,
            "Total €/yr":      r.annual_total_cost,
            "Data":            ("✅" if not r.warnings else f"⚠️ {len(r.warnings)}"),
        })

    df = pd.DataFrame(rows)

    def _style(row):
        bg = "#FFFFFF"
        if row["Data"].startswith("⚠️"):
            bg = "#FEF9E7"
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    st.dataframe(df.style.apply(_style, axis=1),
                 use_container_width=True, hide_index=True)

    # Warnings expander
    warned = [r for r in results if r.warnings]
    if warned:
        with st.expander(f"⚠️ {len(warned)} item(s) with data-quality warnings"):
            for r in warned:
                st.markdown(f"**{r.part_number}** — {r.description}")
                for w in r.warnings:
                    st.caption(f"  • {w}")


# ─────────────────────────────────────────────────────────────────────────────
# Inventory-value chart
# ─────────────────────────────────────────────────────────────────────────────

def _render_value_chart(results):
    st.subheader("Optimal Average Inventory Value per Item")
    sorted_r = sorted(results, key=lambda r: r.avg_inventory_value, reverse=True)[:25]

    if not sorted_r or all(r.avg_inventory_value == 0 for r in sorted_r):
        st.info("No unit-cost data available — chart cannot be rendered. "
                "Set **Unit Cost** in Material Master.")
        return

    parts  = [r.part_number for r in sorted_r]
    cycle  = [r.avg_cycle_stock * r.unit_cost for r in sorted_r]
    safety = [r.safety_stock    * r.unit_cost for r in sorted_r]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=parts, y=cycle, name="Cycle stock value",
        marker_color="#3498DB",
        hovertemplate="<b>%{x}</b><br>Cycle value: € %{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=parts, y=safety, name="Safety stock value",
        marker_color="#E67E22",
        hovertemplate="<b>%{x}</b><br>Safety value: € %{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        barmode="stack",
        height=440,
        xaxis_title="Part",
        yaxis_title="€",
        margin=dict(t=30, b=100, l=40, r=20),
        plot_bgcolor="white",
        legend=dict(orientation="h", y=-0.3),
    )
    st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# DDMRP comparison
# ─────────────────────────────────────────────────────────────────────────────

def _render_ddmrp_comparison(results):
    st.subheader("Calculated Safety Stock vs DDMRP Red Zone (TOR)")
    st.caption(
        "Positive delta = the statistical safety stock is **higher** than the current "
        "DDMRP Red Zone (your buffer may be undersized). Negative = oversized."
    )

    sorted_r = sorted(results, key=lambda r: r.delta_vs_ddmrp, reverse=True)

    rows = [{
        "Part Number":     r.part_number,
        "SS (calculated)": r.safety_stock,
        "TOR (DDMRP)":     r.current_top_of_red,
        "Δ (SS − TOR)":    r.delta_vs_ddmrp,
        "Verdict":         ("🔴 Undersized" if r.delta_vs_ddmrp >  0.1 * max(r.current_top_of_red, 1)
                            else "🟢 Oversized"   if r.delta_vs_ddmrp < -0.1 * max(r.current_top_of_red, 1)
                            else "✅ Aligned"),
    } for r in sorted_r]

    df = pd.DataFrame(rows)

    def _style(row):
        verdict = row["Verdict"]
        if "Undersized" in verdict: bg = "#FADBD8"
        elif "Oversized" in verdict: bg = "#D5F5E3"
        else: bg = "#EBF5FB"
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    st.dataframe(df.style.apply(_style, axis=1),
                 use_container_width=True, hide_index=True)
