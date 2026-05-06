"""
Dashboard — Streamlit page.
Visual overview of all buffer statuses, NFP trends, and the process map
overlaid with buffer health colours.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
from database.db import get_session, Item, Buffer
from database.auth import get_company_id
from modules.buffer_engine import recalculate_all_buffers, calculate_zones


STATUS_COLOR = {"red": "#E74C3C", "yellow": "#F1C40F", "green": "#2ECC71"}
STATUS_BG = {"red": "#FADBD8", "yellow": "#FDEBD0", "green": "#D5F5E3"}

# Execution colour palette (deck slides 109-118) — distinct from planning colour
EXEC_COLOR = {
    "over_tog": "#3498DB",   # blue — excess inventory above TOG
    "green":    "#2ECC71",
    "yellow":   "#F1C40F",
    "red":      "#E74C3C",
    "dark_red": "#7B241C",   # critical / stockout
}
EXEC_LABEL = {
    "over_tog": "📘 Over-TOG",
    "green":    "🟢 OK",
    "yellow":   "🟡 Watch",
    "red":      "🔴 Critical",
    "dark_red": "⚫ Stockout",
}


@st.cache_data(ttl=60)
def _load_dashboard_data(company_id: int) -> list:
    """Load items, buffers and calculate zones — cached 60 s."""
    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == company_id).all()
        buf_map = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    rows = []
    for item in items:
        buf = buf_map.get(item.id)
        try:
            z = calculate_zones(item)
        except Exception:
            z = None
        rows.append({
            "item_id": item.id,
            "part_number": item.part_number,
            "description": item.description,
            "on_hand": item.on_hand,
            "status": buf.status if buf else "unknown",
            "nfp": buf.net_flow_position if buf else 0.0,
            "tor": buf.top_of_red if buf else 0.0,
            "toy": buf.top_of_yellow if buf else 0.0,
            "tog": buf.top_of_green if buf else 0.0,
            "suggested_qty": buf.suggested_order_qty if buf else 0.0,
            "last_calc": buf.last_calculated if buf else None,
            "buffer_status_pct": (buf.buffer_status_pct or 0.0) if buf else 0.0,
            "execution_color":  (buf.execution_color or "green") if buf else "green",
            "avg_inv_target":      z.avg_inventory_target     if z else 0.0,
            "order_freq_days":     z.avg_order_frequency_days if z else 0.0,
            "safety_days":         z.safety_days              if z else 0.0,
            "avg_active_orders":   z.avg_active_orders        if z else 0.0,
        })
    return rows


def show():
    st.header("DDMRP Dashboard")
    st.caption("Live overview of all buffer levels across the manufacturing process.")

    col_refresh, col_ts = st.columns([1, 3])
    with col_refresh:
        if st.button("Refresh Buffers", type="primary", use_container_width=True):
            with st.spinner("Recalculating..."):
                recalculate_all_buffers(company_id=get_company_id())
            _load_dashboard_data.clear()   # invalidate cache so new data shows immediately
            st.success("Buffers refreshed.")

    rows = _load_dashboard_data(get_company_id())

    if not rows:
        st.info("No items found. Start in **Material Master** to add items.")
        return

    df = pd.DataFrame(rows)

    # -----------------------------------------------------------------------
    # KPI Row
    # -----------------------------------------------------------------------
    st.divider()
    _kpi_row(df)

    # -----------------------------------------------------------------------
    # Canonical DDMRP KPIs (slide 59 / 92)
    # -----------------------------------------------------------------------
    _ddmrp_kpi_row(df)

    # -----------------------------------------------------------------------
    # Execution view — Buffer Status % (deck slides 109-118)
    # -----------------------------------------------------------------------
    _execution_row(df)

    # -----------------------------------------------------------------------
    # Buffer Status Board
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("Buffer Status Board")
    _buffer_status_board(df)

    # -----------------------------------------------------------------------
    # NFP vs Zones Bar Chart
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("Net Flow Position vs Buffer Zones")
    _nfp_zone_chart(df)

    # -----------------------------------------------------------------------
    # Demand Horizon
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("Demand Horizon (Next 30 Days)")
    _demand_horizon_chart()


# ---------------------------------------------------------------------------
# KPI Row
# ---------------------------------------------------------------------------

def _kpi_row(df: pd.DataFrame):
    total = len(df)
    red = len(df[df.status == "red"])
    yellow = len(df[df.status == "yellow"])
    green = len(df[df.status == "green"])
    reorder_val = df[df.status.isin(["red", "yellow"])]["suggested_qty"].sum()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Items", total)
    c2.metric("🔴 Critical", red)
    c3.metric("🟡 Attention", yellow)
    c4.metric("🟢 OK", green)
    c5.metric("Total Reorder Qty", f"{reorder_val:,.0f}")


def _ddmrp_kpi_row(df: pd.DataFrame):
    """
    Canonical DDMRP fleet-level KPIs (deck slides 59 / 92):
      - Avg Inventory Target (sum across items) = Σ (Red + Green/2)
      - Avg Order Frequency (median across items) — Green / ADU
      - Avg Safety Days (median across items)    — Red / ADU
      - Avg Active Orders (mean across items)    — Yellow / Green
    """
    if df.empty:
        return

    total_avg_inv = df["avg_inv_target"].sum()
    valid_freq    = df.loc[df["order_freq_days"]   > 0, "order_freq_days"]
    valid_safety  = df.loc[df["safety_days"]       > 0, "safety_days"]
    valid_active  = df.loc[df["avg_active_orders"] > 0, "avg_active_orders"]

    median_freq   = valid_freq.median()   if not valid_freq.empty   else 0.0
    median_safety = valid_safety.median() if not valid_safety.empty else 0.0
    mean_active   = valid_active.mean()   if not valid_active.empty else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📦 Avg Inventory Target", f"{total_avg_inv:,.0f} units",
              help="Σ (Red + Green/2) across all items — DDMRP canonical inventory target")
    c2.metric("🔁 Median Order Frequency", f"{median_freq:.1f} days",
              help="Green / ADU — median days between orders across the fleet")
    c3.metric("🛡️ Median Safety Days", f"{median_safety:.1f} days",
              help="Red / ADU — median days of safety stock across the fleet")
    c4.metric("📨 Avg Active Orders", f"{mean_active:.2f}",
              help="Yellow / Green — mean number of simultaneously open replenishment orders")


def _execution_row(df: pd.DataFrame):
    """
    Execution-side dashboard row (deck slides 109-118).
    Shows Buffer Status % distribution across the 5 execution colour bands:
      over_tog | green | yellow | red | dark_red
    """
    if df.empty:
        return

    counts = df["execution_color"].value_counts().to_dict()
    n_over = counts.get("over_tog", 0)
    n_grn  = counts.get("green",    0)
    n_yel  = counts.get("yellow",   0)
    n_red  = counts.get("red",      0)
    n_drk  = counts.get("dark_red", 0)

    valid = df[df["tor"] > 0]
    avg_pct    = (valid["buffer_status_pct"].mean() * 100) if not valid.empty else 0.0
    median_pct = (valid["buffer_status_pct"].median() * 100) if not valid.empty else 0.0

    st.divider()
    st.subheader("Execution View — Buffer Status %")
    st.caption("On-Hand / Top-of-Red. 100% = at TOR; <50% triggers a Current-Stock alarm.")

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("📘 Over-TOG", n_over, help="On-Hand above Top of Green (excess)")
    c2.metric("🟢 OK",       n_grn)
    c3.metric("🟡 Watch",    n_yel, help="On-Hand between 50% and 100% of TOR")
    c4.metric("🔴 Critical", n_red, help="On-Hand below 50% of TOR")
    c5.metric("⚫ Stockout", n_drk, help="On-Hand <= 0")
    c6.metric("Avg Status %",    f"{avg_pct:.0f}%")
    c7.metric("Median Status %", f"{median_pct:.0f}%")


# ---------------------------------------------------------------------------
# Buffer Status Board (card-style)
# ---------------------------------------------------------------------------

def _buffer_status_board(df: pd.DataFrame):
    priority = {"red": 0, "yellow": 1, "green": 2, "unknown": 3}
    sorted_df = df.sort_values("status", key=lambda s: s.map(priority))

    cols_per_row = 4
    rows_data = [sorted_df.iloc[i:i + cols_per_row]
                 for i in range(0, len(sorted_df), cols_per_row)]

    for row_data in rows_data:
        cols = st.columns(cols_per_row)
        for col, (_, r) in zip(cols, row_data.iterrows()):
            status = r["status"]
            bg = STATUS_BG.get(status, "#F8F9FA")
            emoji = {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(status, "⚪")

            exec_band = r.get("execution_color", "green")
            exec_lbl  = EXEC_LABEL.get(exec_band, exec_band)
            exec_clr  = EXEC_COLOR.get(exec_band, "#888")
            pct       = (r.get("buffer_status_pct") or 0.0) * 100.0

            col.markdown(
                f"""
                <div style="background:{bg}; border-radius:8px; padding:12px;
                            border-left:4px solid {STATUS_COLOR.get(status,'#ccc')};">
                  <b style="font-size:1.0em">{emoji} {r['part_number']}</b><br>
                  <span style="font-size:0.8em; color:#555">{r['description'][:30]}</span><br>
                  <span style="font-size:0.9em">NFP: <b>{r['nfp']:.1f}</b></span><br>
                  <span style="font-size:0.8em">TOG: {r['tog']:.1f} | TOR: {r['tor']:.1f}</span><br>
                  <span style="font-size:0.8em">Status %: <b style="color:{exec_clr}">{pct:.0f}%</b> &middot; {exec_lbl}</span><br>
                  {'<span style="color:#E74C3C; font-size:0.85em"><b>Order: ' + str(round(r["suggested_qty"],1)) + ' units</b></span>' if r["suggested_qty"] > 0 else '<span style="color:#27AE60; font-size:0.85em">No action needed</span>'}
                </div>
                """,
                unsafe_allow_html=True,
            )


# ---------------------------------------------------------------------------
# NFP vs Zones horizontal bar chart
# ---------------------------------------------------------------------------

def _nfp_zone_chart(df: pd.DataFrame):
    if df.empty:
        return

    sorted_df = df.sort_values("part_number")

    fig = go.Figure()

    # Red zone bar (baseline)
    fig.add_trace(go.Bar(
        name="Red Zone", x=sorted_df["tor"], y=sorted_df["part_number"],
        orientation="h", marker_color="#FADBD8", marker_line_color="#E74C3C",
        marker_line_width=1,
    ))
    # Yellow zone increment
    fig.add_trace(go.Bar(
        name="Yellow Zone",
        x=sorted_df["toy"] - sorted_df["tor"],
        y=sorted_df["part_number"],
        orientation="h", marker_color="#FDEBD0", marker_line_color="#F39C12",
        marker_line_width=1,
    ))
    # Green zone increment
    fig.add_trace(go.Bar(
        name="Green Zone",
        x=sorted_df["tog"] - sorted_df["toy"],
        y=sorted_df["part_number"],
        orientation="h", marker_color="#D5F5E3", marker_line_color="#27AE60",
        marker_line_width=1,
    ))

    # NFP marker
    fig.add_trace(go.Scatter(
        name="Net Flow Position",
        x=sorted_df["nfp"], y=sorted_df["part_number"],
        mode="markers",
        marker=dict(size=12, color="#2C3E50", symbol="diamond"),
    ))

    fig.update_layout(
        barmode="stack",
        height=max(300, len(df) * 50),
        xaxis_title="Units",
        yaxis_title="",
        legend=dict(orientation="h", y=-0.15),
        margin=dict(l=10, r=10, t=10, b=60),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Diamond marker = Net Flow Position. Coloured bars = buffer zones.")


# ---------------------------------------------------------------------------
# Demand Horizon
# ---------------------------------------------------------------------------

def _demand_horizon_chart():
    from database.db import DemandEntry

    session = get_session()
    try:
        today = datetime.utcnow()
        horizon = today + timedelta(days=30)
        entries = (
            session.query(DemandEntry, Item)
            .join(Item, DemandEntry.item_id == Item.id)
            .filter(DemandEntry.demand_date >= today,
                    DemandEntry.demand_date <= horizon)
            .all()
        )
        if not entries:
            st.info("No demand entries in the next 30 days.")
            return

        rows = [{
            "Date": e.demand_date.date(),
            "Part": it.part_number,
            "Quantity": e.quantity,
            "Type": e.demand_type,
        } for e, it in entries]
    finally:
        session.close()

    df_demand = pd.DataFrame(rows)
    df_demand["Date"] = pd.to_datetime(df_demand["Date"])

    fig = px.bar(
        df_demand.groupby(["Date", "Part"])["Quantity"].sum().reset_index(),
        x="Date", y="Quantity", color="Part",
        barmode="group",
        labels={"Quantity": "Demand Qty"},
    )
    fig.update_layout(height=350, margin=dict(t=10, b=40, l=10, r=10))
    st.plotly_chart(fig, use_container_width=True)
