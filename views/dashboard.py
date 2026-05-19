"""
Dashboard — Tactical Supply Chain Operations View.

Designed for a supply chain manager who needs to oversee all operations in
a single glance: spot stockout risks early, see where cash is trapped,
understand inventory value distribution by ABC class, and prioritise the
day's actions.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
from database.db import get_session, Item, Buffer
from database.auth import get_company_id
from modules.buffer_engine import recalculate_all_buffers


# ── Execution-band palette (deck slides 109-118) ───────────────────────────

EXEC_COLOR = {
    "over_tog": "#3498DB",   # blue — excess
    "green":    "#2ECC71",
    "yellow":   "#F1C40F",
    "red":      "#E74C3C",
    "dark_red": "#7B241C",   # stockout
}
EXEC_LABEL = {
    "over_tog": "Over-TOG",
    "green":    "OK",
    "yellow":   "Watch",
    "red":      "Critical",
    "dark_red": "Stockout",
}
EXEC_EMOJI = {
    "over_tog": "📘",
    "green":    "🟢",
    "yellow":   "🟡",
    "red":      "🔴",
    "dark_red": "⚫",
}


# ── Data loading (cached 60 s) ─────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _load_dashboard_data(company_id: int) -> list:
    """One-shot loader; computes every per-item € metric the dashboard needs."""
    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == company_id).all()
        buf_map = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    rows = []
    for item in items:
        buf = buf_map.get(item.id)
        adu       = float(item.adu       or 0.0)
        unit_cost = float(item.unit_cost or 0.0)
        on_hand   = float(item.on_hand   or 0.0)

        tor = float(buf.top_of_red    or 0.0) if buf else 0.0
        toy = float(buf.top_of_yellow or 0.0) if buf else 0.0
        tog = float(buf.top_of_green  or 0.0) if buf else 0.0

        excess_units    = max(0.0, on_hand - tog) if tog > 0 else 0.0
        shortfall_units = max(0.0, tor - on_hand) if tor > 0 else 0.0

        rows.append({
            "item_id":            item.id,
            "part_number":        item.part_number,
            "description":        item.description or "",
            "category":           item.category or "(unset)",
            "adu":                adu,
            "unit_cost":          unit_cost,
            "on_hand":            on_hand,
            "tor":                tor,
            "toy":                toy,
            "tog":                tog,
            "nfp":                float(buf.net_flow_position or 0.0) if buf else 0.0,
            "suggested_qty":      float(buf.suggested_order_qty or 0.0) if buf else 0.0,
            "buffer_status_pct":  float(buf.buffer_status_pct or 0.0) if buf else 0.0,
            "execution_color":    (buf.execution_color or "green") if buf else "green",
            "status":             buf.status if buf else "unknown",
            "annual_usage_value": adu * 365.0 * unit_cost,
            "on_hand_value":      on_hand * unit_cost,
            "excess_units":       excess_units,
            "excess_value":       excess_units    * unit_cost,
            "shortfall_units":    shortfall_units,
            "shortfall_value":    shortfall_units * unit_cost,
            # 7-day lost-sale exposure if the item stays below TOR
            "weekly_risk_value":  adu * 7.0 * unit_cost,
            "suggested_value":    float(buf.suggested_order_qty or 0.0) * unit_cost if buf else 0.0,
        })
    return rows


def _compute_abc(df: pd.DataFrame) -> pd.DataFrame:
    """Tag each row with A/B/C class using 80/15/5 Pareto on annual usage €."""
    if df.empty:
        df["abc"] = []
        return df
    total = df["annual_usage_value"].sum()
    if total <= 0:
        df["abc"] = "C"
        return df
    sorted_df = df.sort_values("annual_usage_value", ascending=False).copy()
    sorted_df["_cum_pct"] = sorted_df["annual_usage_value"].cumsum() / total

    def _class(p: float) -> str:
        if p <= 0.80: return "A"
        if p <= 0.95: return "B"
        return "C"

    abc_map = dict(zip(sorted_df["item_id"], sorted_df["_cum_pct"].apply(_class)))
    df["abc"] = df["item_id"].map(abc_map).fillna("C")
    return df


# ── Page entry point ──────────────────────────────────────────────────────

def show():
    st.header("Supply Chain Tactical Dashboard")
    st.caption(
        "One-screen overview for operations: where the money is, where the risk is, "
        "and what to act on today."
    )

    col_refresh, _ = st.columns([1, 5])
    with col_refresh:
        if st.button("🔄 Refresh Buffers", type="primary", use_container_width=True):
            with st.spinner("Recalculating…"):
                recalculate_all_buffers(company_id=get_company_id())
            _load_dashboard_data.clear()
            st.success("Buffers refreshed.")

    rows = _load_dashboard_data(get_company_id())
    if not rows:
        st.info("No items found. Start in **Material Master** to add items.")
        return

    df = _compute_abc(pd.DataFrame(rows))

    # ── 1. Headline KPI strip ─────────────────────────────────────────────
    st.divider()
    _headline_kpis(df)

    # ── 2. Health donut + Inventory € by ABC ──────────────────────────────
    st.divider()
    col1, col2 = st.columns([1, 1.4])
    with col1:
        _health_donut(df)
    with col2:
        _value_by_abc(df)

    # ── 3. Action items: stockout risks + overstock ──────────────────────
    st.divider()
    st.subheader("🎯 Immediate Action Items")
    st.caption("Sorted by € impact — start at the top.")
    col1, col2 = st.columns(2)
    with col1:
        _top_stockout_risks(df)
    with col2:
        _top_overstock(df)

    # ── 4. Reorder pipeline + ABC × execution heatmap ────────────────────
    st.divider()
    col1, col2 = st.columns([1, 1.2])
    with col1:
        _reorder_pipeline(df)
    with col2:
        _risk_concentration(df)

    # ── 5. Forward demand (next 30 days) ─────────────────────────────────
    st.divider()
    st.subheader("📅 Demand Horizon (Next 30 Days)")
    _demand_horizon_chart()


# ── 1. Headline KPI strip ─────────────────────────────────────────────────

def _headline_kpis(df: pd.DataFrame):
    total_value     = df["on_hand_value"].sum()
    excess_value    = df["excess_value"].sum()
    n_stockout      = int((df["execution_color"] == "dark_red").sum())
    n_critical      = int((df["execution_color"] == "red").sum())
    n_overstock     = int((df["execution_color"] == "over_tog").sum())

    # Service level proxy = items NOT below TOR / total buffered items
    buffered = df[df["tor"] > 0]
    healthy  = buffered[~buffered["execution_color"].isin(["red", "dark_red"])]
    service_pct = (len(healthy) / len(buffered) * 100) if len(buffered) > 0 else 0.0

    # Stockout exposure € — 7-day lost-sale risk for items below TOR
    at_risk = df[df["execution_color"].isin(["red", "dark_red"])]
    exposure_value = at_risk["weekly_risk_value"].sum()

    reorder_value = df["suggested_value"].sum()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric(
        "💰 Inventory Value",
        f"€{total_value:,.0f}",
        help="Σ on-hand × unit cost across all items",
    )
    c2.metric(
        "📈 Service Level",
        f"{service_pct:.0f}%",
        help="% of buffered items not in Red/Stockout",
    )
    c3.metric(
        "🔴 Items at Risk",
        f"{n_critical + n_stockout}",
        help=f"{n_critical} Critical + {n_stockout} Stockout",
    )
    c4.metric(
        "⚫ 7-day Lost-Sale Risk",
        f"€{exposure_value:,.0f}",
        help="Σ ADU × 7 × unit cost for items below TOR — revenue at risk this week",
    )
    c5.metric(
        "💸 Cash Trapped (Overstock)",
        f"€{excess_value:,.0f}",
        help=f"Σ (on-hand − TOG) × unit cost over {n_overstock} item(s) above target",
    )
    c6.metric(
        "🛒 Suggested Orders",
        f"€{reorder_value:,.0f}",
        help="Σ suggested order qty × unit cost — what to release today",
    )


# ── 2a. Inventory Health donut ────────────────────────────────────────────

def _health_donut(df: pd.DataFrame):
    st.subheader("Inventory Health")
    bands = ["dark_red", "red", "yellow", "green", "over_tog"]
    counts = df["execution_color"].value_counts().reindex(bands, fill_value=0)
    if counts.sum() == 0:
        st.info("No buffered items yet.")
        return

    fig = go.Figure(go.Pie(
        labels=[f"{EXEC_EMOJI[b]} {EXEC_LABEL[b]}" for b in bands],
        values=counts.values.tolist(),
        marker=dict(colors=[EXEC_COLOR[b] for b in bands]),
        hole=0.58,
        textinfo="value+percent",
        sort=False,
        direction="clockwise",
    ))

    # Centre annotation — % healthy (green or above)
    ok_pct = (counts.get("green", 0) + counts.get("over_tog", 0)) / counts.sum() * 100
    fig.update_layout(
        height=340,
        margin=dict(t=10, b=10, l=10, r=10),
        showlegend=True,
        legend=dict(orientation="v", x=1.02, y=0.5, font=dict(size=11)),
        annotations=[dict(
            text=f"<b style='font-size:1.6em;color:#2C3E50'>{ok_pct:.0f}%</b>"
                 f"<br><span style='font-size:0.75em;color:#7A92BB'>at or above target</span>",
            x=0.5, y=0.5, showarrow=False,
        )],
    )
    st.plotly_chart(fig, use_container_width=True)


# ── 2b. Inventory Value by ABC class ──────────────────────────────────────

def _value_by_abc(df: pd.DataFrame):
    st.subheader("Inventory Value by ABC Class")
    st.caption("Stacked by execution status — spot risk concentration where the money is.")
    if df.empty:
        st.info("No data.")
        return

    grouped = df.groupby(["abc", "execution_color"])["on_hand_value"].sum().reset_index()
    abc_order = ["A", "B", "C"]
    bands_order = ["dark_red", "red", "yellow", "green", "over_tog"]

    fig = go.Figure()
    for band in bands_order:
        sub = grouped[grouped["execution_color"] == band].set_index("abc").reindex(abc_order, fill_value=0)
        if sub["on_hand_value"].sum() == 0:
            continue
        fig.add_trace(go.Bar(
            name=f"{EXEC_EMOJI[band]} {EXEC_LABEL[band]}",
            x=abc_order,
            y=sub["on_hand_value"].values,
            marker_color=EXEC_COLOR[band],
            hovertemplate="<b>Class %{x}</b><br>" + EXEC_LABEL[band]
                          + ": €%{y:,.0f}<extra></extra>",
        ))
    fig.update_layout(
        barmode="stack",
        height=340,
        xaxis_title="ABC Class (A = top 80% of usage €)",
        yaxis_title="On-Hand Value (€)",
        legend=dict(orientation="h", y=-0.22),
        margin=dict(t=10, b=70, l=10, r=10),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Summary strip under the chart
    totals = df.groupby("abc")["on_hand_value"].sum()
    total = totals.sum()
    cols = st.columns(3)
    for col, cls in zip(cols, abc_order):
        v = totals.get(cls, 0.0)
        n = int((df["abc"] == cls).sum())
        pct = (v / total * 100) if total > 0 else 0.0
        col.metric(f"Class {cls}", f"€{v:,.0f}", f"{n} items · {pct:.0f}% of €")


# ── 3a. Top Stockout Risks ────────────────────────────────────────────────

def _top_stockout_risks(df: pd.DataFrame):
    st.markdown("**🚨 Top Stockout Risks** &nbsp; *(by 7-day lost-sale €)*")
    risks = df[df["execution_color"].isin(["red", "dark_red"])].copy()
    if risks.empty:
        st.success("✅ No items in Red or Stockout band.")
        return

    risks = risks.sort_values("weekly_risk_value", ascending=False).head(10)
    table = pd.DataFrame({
        "Status":      risks["execution_color"].map(lambda b: f"{EXEC_EMOJI.get(b, '⚪')} {EXEC_LABEL.get(b, b)}"),
        "ABC":         risks["abc"],
        "Part":        risks["part_number"],
        "Description": risks["description"].str.slice(0, 40),
        "On Hand":     risks["on_hand"].round(1),
        "TOR":         risks["tor"].round(1),
        "Order Now":   risks["suggested_qty"].round(0),
        "7d Risk (€)": risks["weekly_risk_value"].round(0),
    })
    st.dataframe(table, use_container_width=True, hide_index=True)


# ── 3b. Top Overstock — Cash Trapped ──────────────────────────────────────

def _top_overstock(df: pd.DataFrame):
    st.markdown("**💸 Top Overstock** &nbsp; *(cash trapped above TOG)*")
    over = df[df["excess_value"] > 0].copy()
    if over.empty:
        st.success("✅ No items above Top of Green.")
        return

    over = over.sort_values("excess_value", ascending=False).head(10)
    table = pd.DataFrame({
        "ABC":             over["abc"],
        "Part":            over["part_number"],
        "Description":     over["description"].str.slice(0, 40),
        "On Hand":         over["on_hand"].round(1),
        "TOG":             over["tog"].round(1),
        "Excess Units":    over["excess_units"].round(1),
        "Cash Trapped (€)": over["excess_value"].round(0),
    })
    st.dataframe(table, use_container_width=True, hide_index=True)


# ── 4a. Reorder Pipeline ─────────────────────────────────────────────────

def _reorder_pipeline(df: pd.DataFrame):
    st.subheader("🛒 Reorder Pipeline")
    st.caption("Suggested orders today, grouped by ABC class.")
    pipe = df[df["suggested_qty"] > 0].copy()
    if pipe.empty:
        st.success("✅ No replenishment orders suggested.")
        return

    summary = pipe.groupby("abc").agg(
        items=("item_id", "count"),
        units=("suggested_qty", "sum"),
        value=("suggested_value", "sum"),
    ).reindex(["A", "B", "C"], fill_value=0).reset_index()

    fig = go.Figure(go.Bar(
        x=summary["abc"],
        y=summary["value"],
        marker_color=["#2C3E50", "#5D6D7E", "#AAB7B8"],
        text=[f"€{v:,.0f}<br>{int(n)} items" for v, n in zip(summary["value"], summary["items"])],
        textposition="outside",
        hovertemplate="<b>Class %{x}</b><br>€%{y:,.0f}<extra></extra>",
    ))
    fig.update_layout(
        height=300,
        xaxis_title="ABC Class",
        yaxis_title="Suggested Order Value (€)",
        margin=dict(t=20, b=40, l=10, r=10),
        plot_bgcolor="white",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ── 4b. Risk concentration heatmap (ABC × execution band) ────────────────

def _risk_concentration(df: pd.DataFrame):
    st.subheader("🌡️ Risk Concentration — ABC × Execution Band")
    st.caption("Item counts. A-items in Red/Stockout are the highest priority.")

    bands = ["dark_red", "red", "yellow", "green", "over_tog"]
    band_labels = [f"{EXEC_EMOJI[b]} {EXEC_LABEL[b]}" for b in bands]
    abc_order = ["A", "B", "C"]

    mat = []
    for cls in abc_order:
        row = []
        for b in bands:
            row.append(int(((df["abc"] == cls) & (df["execution_color"] == b)).sum()))
        mat.append(row)

    fig = go.Figure(go.Heatmap(
        z=mat,
        x=band_labels,
        y=abc_order,
        colorscale=[[0, "#F8F9FA"], [0.5, "#F5B041"], [1, "#922B21"]],
        text=mat,
        texttemplate="%{text}",
        textfont={"size": 13, "color": "#1A1A1A"},
        hovertemplate="Class %{y} · %{x}<br>%{z} items<extra></extra>",
        showscale=False,
    ))
    fig.update_layout(
        height=300,
        margin=dict(t=10, b=30, l=10, r=10),
        xaxis=dict(side="bottom"),
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig, use_container_width=True)


# ── 5. Forward Demand Horizon ────────────────────────────────────────────

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
                    DemandEntry.demand_date <= horizon,
                    Item.company_id == get_company_id())
            .all()
        )
        if not entries:
            st.info("No demand entries in the next 30 days.")
            return
        rows = [{
            "Date":     e.demand_date.date(),
            "Part":     it.part_number,
            "Quantity": e.quantity,
        } for e, it in entries]
    finally:
        session.close()

    df_demand = pd.DataFrame(rows)
    df_demand["Date"] = pd.to_datetime(df_demand["Date"])
    fig = px.bar(
        df_demand.groupby(["Date", "Part"])["Quantity"].sum().reset_index(),
        x="Date", y="Quantity", color="Part",
        barmode="stack",
        labels={"Quantity": "Demand Qty"},
    )
    fig.update_layout(
        height=320,
        margin=dict(t=10, b=40, l=10, r=10),
        plot_bgcolor="white",
        legend=dict(orientation="h", y=-0.25),
    )
    st.plotly_chart(fig, use_container_width=True)
