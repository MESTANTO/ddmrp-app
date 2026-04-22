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
from modules.buffer_engine import recalculate_all_buffers


STATUS_COLOR = {"red": "#E74C3C", "yellow": "#F1C40F", "green": "#2ECC71"}
STATUS_BG = {"red": "#FADBD8", "yellow": "#FDEBD0", "green": "#D5F5E3"}


def show():
    st.header("DDMRP Dashboard")
    st.caption("Live overview of all buffer levels across the manufacturing process.")

    col_refresh, col_ts = st.columns([1, 3])
    with col_refresh:
        if st.button("Refresh Buffers", type="primary", use_container_width=True):
            with st.spinner("Recalculating..."):
                recalculate_all_buffers()
            st.success("Buffers refreshed.")

    session = get_session()
    try:
        items = session.query(Item).all()
        buffers = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    if not items:
        st.info("No items found. Start in **Material Master** to add items.")
        return

    # Build summary dataframe
    rows = []
    for item in items:
        buf = buffers.get(item.id)
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
        })

    df = pd.DataFrame(rows)

    # -----------------------------------------------------------------------
    # KPI Row
    # -----------------------------------------------------------------------
    st.divider()
    _kpi_row(df)

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
            col.markdown(
                f"""
                <div style="background:{bg}; border-radius:8px; padding:12px;
                            border-left:4px solid {STATUS_COLOR.get(status,'#ccc')};">
                  <b style="font-size:1.0em">{emoji} {r['part_number']}</b><br>
                  <span style="font-size:0.8em; color:#555">{r['description'][:30]}</span><br>
                  <span style="font-size:0.9em">NFP: <b>{r['nfp']:.1f}</b></span><br>
                  <span style="font-size:0.8em">TOG: {r['tog']:.1f} | TOR: {r['tor']:.1f}</span><br>
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
