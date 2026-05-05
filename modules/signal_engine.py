"""
Signal Engine — Streamlit page.
Tabs:
  1. Replenishment Signals  — today's buffer status + first trigger alert
  2. Planned Orders         — ALL orders needed across the full horizon to stay green
  3. Projection Charts      — per-item NFP chart: unplanned vs planned lines
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, datetime, timedelta
from database.db import get_session, Item, Buffer
from modules.buffer_engine import (
    recalculate_all_buffers,
    project_all_buffers,
    plan_all_items,
    project_buffer_forward,
    plan_replenishment_orders,
    is_buffer_stale,
    calculate_dynamic_adu,
    ADU_WINDOW_DAYS,
    BufferStatus,
    ReplenishmentSignal,
    PlanningResult,
    PlannedOrder,
)

STATUS_COLOR = {"red": "#E74C3C", "yellow": "#F39C12", "green": "#27AE60"}
STATUS_BG    = {"red": "#FADBD8", "yellow": "#FDEBD0", "green": "#D5F5E3"}
STATUS_EMOJI = {"red": "🔴",      "yellow": "🟡",      "green": "🟢"}

# Execution-side colour bands (deck slides 109-118)
EXEC_EMOJI = {
    "over_tog": "📘",
    "green":    "🟢",
    "yellow":   "🟡",
    "red":      "🔴",
    "dark_red": "⚫",
}
EXEC_LABEL = {
    "over_tog": "Over-TOG",
    "green":    "OK",
    "yellow":   "Watch",
    "red":      "Critical",
    "dark_red": "Stockout",
}


def _execution_band(on_hand: float, tor: float, tog: float) -> str:
    """Compute the 5-band execution colour from on-hand, TOR, TOG."""
    if tor <= 0:
        return "green"
    if tog > 0 and on_hand > tog:
        return "over_tog"
    pct = on_hand / tor
    if pct < 0:
        return "dark_red"
    if pct < 0.50:
        return "red"
    if pct < 1.00:
        return "yellow"
    return "green"


def show():
    st.header("Replenishment Signals")
    st.caption(
        "Buffer limits are recalculated **weekly** using a dynamic ADU "
        f"(rolling {ADU_WINDOW_DAYS}-day actual demand window). "
        "Run the calculation to refresh all buffer zones and generate the full order plan."
    )

    # ── Staleness banner ──────────────────────────────────────────────────────
    _staleness_banner()

    # ── Controls ──────────────────────────────────────────────────────────────
    col_run, col_horizon, col_window, _ = st.columns([1, 1, 1, 2])
    with col_run:
        run = st.button("▶  Run Calculation", type="primary", use_container_width=True)
    with col_horizon:
        horizon = st.number_input(
            "Projection horizon (days)", min_value=7, max_value=365,
            value=60, step=7,
        )
    with col_window:
        window = st.number_input(
            "ADU window (days)", min_value=1, max_value=90,
            value=ADU_WINDOW_DAYS, step=1,
            help=f"Rolling window for dynamic ADU. Default = {ADU_WINDOW_DAYS} days (1 week).",
        )

    if run:
        with st.spinner(
            f"Recalculating buffer zones with dynamic ADU "
            f"(last {int(window)} days of demand)…"
        ):
            recalculate_all_buffers(window_days=int(window))
            signals  = project_all_buffers(horizon_days=int(horizon))
            planning = plan_all_items(horizon_days=int(horizon))
        st.session_state["signals"]  = signals
        st.session_state["planning"] = planning
        st.session_state["horizon"]  = int(horizon)
        st.session_state["window"]   = int(window)
        st.success(
            f"✅ Buffer zones recalculated for {len(signals)} item(s) using "
            f"a {int(window)}-day dynamic ADU. Next recalculation due in 7 days."
        )

    signals  = st.session_state.get("signals")
    planning = st.session_state.get("planning")
    h        = st.session_state.get("horizon", int(horizon))

    if signals is None:
        signals  = _load_signals(h)
        planning = _load_planning(h)

    if not signals:
        st.info("No items found. Add items in **Material Master** first, then run the calculation.")
        return

    # ── KPI row ───────────────────────────────────────────────────────────────
    _kpi_row(signals, planning)
    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_signals, tab_planned, tab_charts = st.tabs([
        "🚦 Replenishment Signals",
        "📋 Planned Orders",
        "📈 Projection Charts",
    ])

    with tab_signals:
        _signal_table(signals)

    with tab_planned:
        _planned_orders_section(planning)

    with tab_charts:
        _projection_charts_section(signals, planning)


# ─────────────────────────────────────────────────────────────────────────────
# Staleness banner
# ─────────────────────────────────────────────────────────────────────────────

def _staleness_banner():
    """
    Query all Buffer records and warn if any have not been recalculated
    within the dynamic ADU window (ADU_WINDOW_DAYS days).

    Shows:
      - An orange warning if some buffers are stale (listing the items).
      - A green success message if all buffers are fresh.
      - Nothing if there are no buffers yet (first run).
    """
    session = get_session()
    try:
        buffers = session.query(Buffer).all()
        if not buffers:
            return  # no buffers yet — first-run state, skip banner
        # Batch-load all items in one query instead of one session per stale buffer
        items_by_id = {it.id: it for it in session.query(Item).all()}
    finally:
        session.close()

    stale = []
    for buf in buffers:
        if is_buffer_stale(buf, window_days=ADU_WINDOW_DAYS):
            item = items_by_id.get(buf.item_id)
            label = item.part_number if item else f"item #{buf.item_id}"
            age_days = (
                (datetime.utcnow() - buf.last_calculated).days
                if buf.last_calculated else None
            )

            age_str = f"{age_days}d ago" if age_days is not None else "never"
            stale.append(f"**{label}** (last recalc: {age_str})")

    if stale:
        item_list = ", ".join(stale)
        st.warning(
            f"🔄 **{len(stale)} buffer(s) are stale** (not recalculated in the last "
            f"{ADU_WINDOW_DAYS} days): {item_list}.  \n"
            "Click **▶ Run Calculation** to refresh buffer zones with the latest demand data."
        )
    else:
        # All buffers are within the recalculation window
        newest = min(
            (buf.last_calculated for buf in buffers if buf.last_calculated),
            default=None,
        )
        if newest:
            age_h = int((datetime.utcnow() - newest).total_seconds() // 3600)
            age_label = f"{age_h}h ago" if age_h < 48 else f"{age_h // 24}d ago"
            st.success(
                f"✅ All buffer zones are up-to-date (oldest recalculation: {age_label})."
            )


# ─────────────────────────────────────────────────────────────────────────────
# KPI row
# ─────────────────────────────────────────────────────────────────────────────

def _kpi_row(signals, planning):
    total      = len(signals)
    red_now    = sum(1 for s in signals if s.today_status == "red")
    yel_now    = sum(1 for s in signals if s.today_status == "yellow")
    grn_now    = sum(1 for s in signals if s.today_status == "green")
    all_orders = [o for p in (planning or []) for o in p.planned_orders]
    urgent     = sum(1 for o in all_orders if o.is_urgent)
    total_ord  = len(all_orders)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Items",        total)
    c2.metric("🔴 Critical Now",    red_now)
    c3.metric("🟡 Attention Now",   yel_now)
    c4.metric("🟢 OK Now",          grn_now)
    c5.metric("📋 Planned Orders",  total_ord,
              help="Total orders needed across all items to stay green.")
    c6.metric("🚨 Urgent (today)",  urgent,
              help="Orders that must be placed today.")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 1 — Replenishment Signals (today's snapshot)
# ─────────────────────────────────────────────────────────────────────────────

def _signal_table(signals):
    st.subheader("Today's Buffer Status")
    today = date.today()

    rows = []
    for s in signals:
        urgency = _urgency(s, today)
        tor_v = s.zones.top_of_red   if s.zones else 0.0
        tog_v = s.zones.top_of_green if s.zones else 0.0
        exec_band = _execution_band(s.today_on_hand, tor_v, tog_v)
        status_pct = (s.today_on_hand / tor_v * 100.0) if tor_v > 0 else 0.0
        rows.append({
            "_urgency":        urgency,
            "Status":          f"{STATUS_EMOJI.get(s.today_status,'⚪')} {s.today_status.upper()}",
            "Exec":            f"{EXEC_EMOJI.get(exec_band,'⚪')} {EXEC_LABEL.get(exec_band, exec_band)}",
            "Status %":        f"{status_pct:.0f}%",
            "Part Number":     s.part_number,
            "Description":     s.description,
            "NFP Today":       s.today_nfp,
            "On Hand":         s.today_on_hand,
            "On Order":        s.today_on_order,
            "TOR":             round(tor_v, 1) if s.zones else "—",
            "TOY":             round(s.zones.top_of_yellow, 1) if s.zones else "—",
            "TOG":             round(tog_v, 1) if s.zones else "—",
            "First Trigger":   s.trigger_date.strftime("%Y-%m-%d") if s.trigger_date else "—",
            "📅 Order By":     s.order_by_date.strftime("%Y-%m-%d") if s.order_by_date else "—",
            "📦 Receipt":      s.receipt_date.strftime("%Y-%m-%d") if s.receipt_date else "—",
            "1st Order Qty":   s.order_quantity if s.order_quantity > 0 else "—",
            "Action":          _action_label(s, today),
        })

    df = pd.DataFrame(rows).sort_values("_urgency").drop(columns=["_urgency"])

    def _row_style(row):
        label = row["Status"].split()[1].lower()
        bg = STATUS_BG.get(label, "#F8F9FA")
        if label == "green" and row["First Trigger"] != "—":
            bg = "#FEF9E7"
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    st.dataframe(df.style.apply(_row_style, axis=1),
                 use_container_width=True, hide_index=True)

    if st.checkbox("Show only items requiring action", key="filter_signals"):
        action_df = df[df["1st Order Qty"] != "—"]
        st.dataframe(action_df, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 2 — Planned Orders
# ─────────────────────────────────────────────────────────────────────────────

def _planned_orders_section(planning):
    if not planning:
        st.info("Run the calculation first.")
        return

    all_orders: list[PlannedOrder] = []
    for p in planning:
        all_orders.extend(p.planned_orders)

    if not all_orders:
        st.success("✅ No replenishment orders needed — all items will remain in the green zone throughout the entire horizon.")
        return

    today = date.today()

    st.subheader("All Planned Orders")
    st.caption(
        "Every replenishment order needed across the full horizon to keep all buffers in the green zone. "
        "Orders are sorted by urgency (order date)."
    )

    # ── Summary metrics ──
    urgent_orders = [o for o in all_orders if o.is_urgent]
    total_qty     = sum(o.order_quantity for o in all_orders)

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("Total Planned Orders", len(all_orders))
    mc2.metric("🚨 Place Today",        len(urgent_orders))
    mc3.metric("Items Needing Orders",
               len({o.part_number for o in all_orders}))
    mc4.metric("Total Quantity Planned", f"{total_qty:,.0f}")

    st.divider()

    # ── Filter controls ──
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_part = st.multiselect(
            "Filter by Part Number",
            sorted({o.part_number for o in all_orders}),
            key="po_filter_part",
        )
    with col_f2:
        filter_urgent = st.checkbox("Show urgent orders only (place today)", key="po_urgent")
    with col_f3:
        filter_status = st.multiselect(
            "Filter by Trigger Status",
            ["red", "yellow"],
            default=[],
            key="po_status",
        )

    # Apply filters
    filtered = all_orders
    if filter_part:
        filtered = [o for o in filtered if o.part_number in filter_part]
    if filter_urgent:
        filtered = [o for o in filtered if o.is_urgent]
    if filter_status:
        filtered = [o for o in filtered if o.trigger_status in filter_status]

    # Sort by order date, then urgency
    filtered = sorted(filtered, key=lambda o: (o.order_date, o.part_number))

    # ── Build table ──
    rows = []
    for i, o in enumerate(filtered, start=1):
        days_left = (o.order_date - today).days
        if days_left < 0:
            urgency_label = "🚨 OVERDUE"
        elif days_left == 0:
            urgency_label = "🚨 TODAY"
        elif days_left <= 7:
            urgency_label = f"⚡ In {days_left}d"
        else:
            urgency_label = f"📋 In {days_left}d"

        rows.append({
            "#":                 i,
            "Urgency":           urgency_label,
            "Part Number":       o.part_number,
            "Description":       o.description,
            "📅 Order Date":     o.order_date.strftime("%Y-%m-%d"),
            "📦 Receipt Date":   o.receipt_date.strftime("%Y-%m-%d"),
            "Order Qty":         round(o.order_quantity, 1),
            "NFP Before Order":  o.nfp_before,
            "NFP After Order":   o.nfp_after,
            "Trigger":           f"{STATUS_EMOJI.get(o.trigger_status,'⚪')} {o.trigger_status.upper()}",
            "Days Until Order":  max(0, days_left),
        })

    df_po = pd.DataFrame(rows)

    def _po_row_style(row):
        label  = row["Trigger"].split()[1].lower() if row["Trigger"] != "—" else "green"
        bg     = STATUS_BG.get(label, "#F8F9FA")
        urgency = row["Urgency"]
        if "OVERDUE" in urgency or "TODAY" in urgency:
            bg = "#FADBD8"
        elif "In 7d" in urgency or any(f"In {i}d" in urgency for i in range(1, 8)):
            bg = "#FDEBD0"
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    st.dataframe(
        df_po.style.apply(_po_row_style, axis=1),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"Showing {len(rows)} of {len(all_orders)} planned orders.")

    # ── Per-item summary table ──
    st.divider()
    st.subheader("Planned Orders — Summary by Item")

    summary_rows = []
    for p in planning:
        if not p.planned_orders:
            summary_rows.append({
                "Part Number":   p.part_number,
                "Description":   p.description,
                "# Orders":      0,
                "Total Qty":     0,
                "First Order":   "—",
                "Last Order":    "—",
                "Status":        "✅ No orders needed",
            })
        else:
            orders = sorted(p.planned_orders, key=lambda o: o.order_date)
            summary_rows.append({
                "Part Number":   p.part_number,
                "Description":   p.description,
                "# Orders":      len(orders),
                "Total Qty":     round(sum(o.order_quantity for o in orders), 1),
                "First Order":   orders[0].order_date.strftime("%Y-%m-%d"),
                "Last Order":    orders[-1].order_date.strftime("%Y-%m-%d"),
                "Status":        f"{'🚨' if any(o.is_urgent for o in orders) else '📋'} {len(orders)} order(s) planned",
            })

    df_sum = pd.DataFrame(summary_rows)

    def _sum_style(row):
        if row["# Orders"] == 0:
            return ["background-color: #D5F5E3; color: #1A1A1A"] * len(row)
        elif "🚨" in str(row["Status"]):
            return ["background-color: #FADBD8; color: #1A1A1A"] * len(row)
        return ["background-color: #FDEBD0; color: #1A1A1A"] * len(row)

    st.dataframe(df_sum.style.apply(_sum_style, axis=1),
                 use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 3 — Projection Charts
# ─────────────────────────────────────────────────────────────────────────────

def _projection_charts_section(signals, planning):
    st.caption(
        "**Grey dashed line** = NFP without any new orders (raw forecast).  "
        "**Coloured line** = NFP with all planned orders applied.  "
        "**▼ markers** = planned order receipts."
    )

    # Build lookup: part_number → PlanningResult
    plan_map = {p.part_number: p for p in (planning or [])}

    options = ["— All items (grid) —"] + [
        f"{s.part_number} — {s.description}" for s in signals
    ]
    choice = st.selectbox("Select item for detailed chart", options, key="chart_sel")

    if choice == "— All items (grid) —":
        cols_per_row = 2
        chunks = [signals[i:i + cols_per_row] for i in range(0, len(signals), cols_per_row)]
        for chunk in chunks:
            cols = st.columns(cols_per_row)
            for col, sig in zip(cols, chunk):
                with col:
                    pr = plan_map.get(sig.part_number)
                    fig = _build_chart(sig, pr, height=340)
                    st.plotly_chart(fig, use_container_width=True)
    else:
        part = choice.split(" — ")[0]
        sig  = next((s for s in signals if s.part_number == part), None)
        pr   = plan_map.get(part)
        if sig:
            fig = _build_chart(sig, pr, height=520)
            st.plotly_chart(fig, use_container_width=True)
            if pr:
                _daily_detail_table(pr)


def _build_chart(sig: ReplenishmentSignal, pr: PlanningResult, height: int = 400):
    if not sig.daily or not sig.zones:
        return go.Figure()

    zones = sig.zones
    tor   = zones.top_of_red
    toy   = zones.top_of_yellow
    tog   = zones.top_of_green

    planned_orders: list[PlannedOrder] = []

    if pr and pr.daily_unplanned and pr.daily_planned:
        # ── Use the PlanningResult simulation for BOTH lines ──
        # daily_unplanned = same engine as daily_planned, orders suppressed
        # daily_planned   = same engine, orders generated whenever NFP ≤ TOY
        # This ensures apples-to-apples comparison; divergence shows exactly
        # where planned orders lift the buffer above what it would have been.
        dates_raw   = [d.date.isoformat() for d in pr.daily_unplanned]
        nfps_raw    = [d.nfp               for d in pr.daily_unplanned]
        dates_plan  = [d.date.isoformat() for d in pr.daily_planned]
        nfps_plan   = [d.nfp               for d in pr.daily_planned]
        plan_colors = [STATUS_COLOR.get(d.status, "#2C3E50") for d in pr.daily_planned]
        planned_orders = pr.planned_orders
    else:
        # Fallback: only ReplenishmentSignal available — show single unplanned line
        dates_raw   = [d.date.isoformat() for d in sig.daily]
        nfps_raw    = [d.nfp               for d in sig.daily]
        dates_plan  = dates_raw
        nfps_plan   = nfps_raw
        plan_colors = [STATUS_COLOR.get(d.status, "#2C3E50") for d in sig.daily]

    all_nfps = nfps_raw + nfps_plan
    y_max = max(max(all_nfps) * 1.15, tog * 1.1, 1) if all_nfps else tog * 1.2

    fig = go.Figure()

    # ── Zone bands ──
    fig.add_hrect(y0=0,   y1=tor,   fillcolor="#FADBD8", opacity=0.35, line_width=0, layer="below")
    fig.add_hrect(y0=tor, y1=toy,   fillcolor="#FDEBD0", opacity=0.35, line_width=0, layer="below")
    fig.add_hrect(y0=toy, y1=tog,   fillcolor="#D5F5E3", opacity=0.35, line_width=0, layer="below")
    fig.add_hrect(y0=tog, y1=y_max, fillcolor="#EBF5FB", opacity=0.25, line_width=0, layer="below")

    # ── Zone boundary labels (shapes + annotations, avoids Plotly 6 crash) ──
    x0, x1 = dates_plan[0], dates_plan[-1]
    for y_val, zlabel, color in [
        (tor, "TOR", "#E74C3C"),
        (toy, "TOY", "#F39C12"),
        (tog, "TOG", "#27AE60"),
    ]:
        fig.add_shape(type="line", x0=x0, x1=x1, y0=y_val, y1=y_val,
                      line=dict(color=color, width=1.2, dash="dot"), layer="above")
        fig.add_annotation(x=x1, y=y_val, text=f"<b>{zlabel}</b>",
                           xanchor="left", showarrow=False,
                           font=dict(color=color, size=10))

    # ── Unplanned NFP line (grey dashed) — what happens with NO new orders ──
    fig.add_trace(go.Scatter(
        x=dates_raw, y=nfps_raw,
        mode="lines",
        name="NFP (no new orders)",
        line=dict(color="#AEB6BF", width=1.8, dash="dash"),
        hovertemplate="<b>%{x}</b><br>NFP (no orders): %{y:.1f}<extra></extra>",
    ))

    # ── Planned NFP line (coloured by status) — with all DDMRP orders applied ──
    fig.add_trace(go.Scatter(
        x=dates_plan, y=nfps_plan,
        mode="lines+markers",
        name="NFP (with planned orders)",
        line=dict(color="#2C3E50", width=2.5),
        marker=dict(size=4, color=plan_colors),
        hovertemplate="<b>%{x}</b><br>NFP (planned): %{y:.1f}<extra></extra>",
    ))

    # ── Planned order receipt markers (triangles on planned line) ──
    if planned_orders:
        rx = [o.receipt_date.isoformat() for o in planned_orders]
        ry = []
        plan_date_map = {d.date.isoformat(): d.nfp for d in (pr.daily_planned if pr else sig.daily)}
        for o in planned_orders:
            ry.append(plan_date_map.get(o.receipt_date.isoformat(), o.nfp_after))

        fig.add_trace(go.Scatter(
            x=rx, y=ry,
            mode="markers",
            name="Planned receipt",
            marker=dict(symbol="triangle-up", size=14,
                        color="#27AE60", line=dict(color="white", width=1.5)),
            hovertemplate=(
                "<b>Receipt: %{x}</b><br>"
                "NFP after receipt: %{y:.1f}<br>"
                "<extra></extra>"
            ),
        ))

        # Vertical lines for order placement dates
        for o in planned_orders:
            color  = "#E74C3C" if o.is_urgent else "#8E44AD"
            x_str  = o.order_date.isoformat()
            label  = f"{'🚨' if o.is_urgent else '📋'} {o.order_date.strftime('%d-%b')} qty={o.order_quantity:.0f}"
            fig.add_shape(type="line", x0=x_str, x1=x_str, y0=0, y1=y_max * 0.92,
                          line=dict(color=color, width=1.5, dash="dot"), layer="above")
            fig.add_annotation(x=x_str, y=y_max * 0.93,
                               text=label, showarrow=False,
                               xanchor="center", font=dict(color=color, size=9),
                               bgcolor="white", opacity=0.85)

    # ── Title ──
    emoji = STATUS_EMOJI.get(sig.today_status, "⚪")
    n_orders = len(planned_orders)
    title = f"{emoji} {sig.part_number} — {sig.description}  |  {n_orders} planned order(s)"

    fig.update_layout(
        title=dict(text=title, font=dict(size=13)),
        height=height,
        xaxis=dict(title="Date", showgrid=True, gridcolor="#ECF0F1", type="date"),
        yaxis=dict(title="Units", showgrid=True, gridcolor="#ECF0F1", range=[0, y_max]),
        legend=dict(orientation="h", y=-0.22),
        margin=dict(t=50, b=70, l=50, r=90),
        plot_bgcolor="white",
        hovermode="x unified",
    )
    return fig


def _daily_detail_table(pr: PlanningResult):
    with st.expander("View daily planning data"):
        rows = [{
            "Date":              d.date.strftime("%Y-%m-%d"),
            "Status":            f"{STATUS_EMOJI.get(d.status,'⚪')} {d.status.upper()}",
            "NFP (planned)":     d.nfp,
            "On Hand":           d.projected_on_hand,
            "On Order":          d.on_order_remaining,
            "Supply In":         d.supply_received,
            "Demand Out":        d.demand_consumed,
        } for d in pr.daily_planned]

        df = pd.DataFrame(rows)

        def _s(row):
            label = row["Status"].split()[1].lower()
            bg = STATUS_BG.get(label, "#FFFFFF")
            return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

        st.dataframe(df.style.apply(_s, axis=1), use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _urgency(s: ReplenishmentSignal, today: date) -> int:
    if s.today_status == "red":    return 0
    if s.today_status == "yellow": return 1
    if s.trigger_date:
        return 2 + (s.trigger_date - today).days
    return 9999


def _action_label(s: ReplenishmentSignal, today: date) -> str:
    if s.today_status == "red":    return "🚨 ORDER NOW"
    if s.today_status == "yellow": return "⚡ ORDER TODAY"
    if s.order_by_date:
        days = (s.order_by_date - today).days
        return "⚡ ORDER TODAY" if days <= 0 else f"📋 Order in {days}d"
    return "✅ No action"


def _load_signals(horizon: int) -> list:
    session = get_session()
    try:
        items = session.query(Item).all()
    finally:
        session.close()
    results = []
    for item in items:
        try:
            results.append(project_buffer_forward(item, horizon_days=horizon))
        except Exception as e:
            print(f"Signal load error {item.part_number}: {e}")
    return results


def _load_planning(horizon: int) -> list:
    session = get_session()
    try:
        items = session.query(Item).all()
    finally:
        session.close()
    results = []
    for item in items:
        try:
            results.append(plan_replenishment_orders(item, horizon_days=horizon))
        except Exception as e:
            print(f"Planning load error {item.part_number}: {e}")
    return results
