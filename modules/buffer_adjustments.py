"""
Buffer Adjustments — Streamlit page.

Implements the deck's Planned Adjustments (slides 73-80):
  - DAF  (Demand Adjustment Factor)    — multiplier on ADU
  - LTAF (Lead Time Adjustment Factor) — multiplier on DLT
  - ZAF  (Zone Adjustment Factors)     — per-zone multipliers (Red, Yellow, Green)

Each adjustment is bounded by a start/end date. Multiple overlapping
adjustments on the same item multiply together.
"""

from datetime import datetime, date, timedelta
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from database.db import get_session, Item, BufferAdjustment
from modules.buffer_engine import calculate_zones, get_active_factors, load_item_adjustments


def show():
    st.header("Planned Buffer Adjustments")
    st.caption(
        "DAF / ZAF / LTAF — schedule time-bounded coefficients to lift or lower "
        "ADU, DLT, and individual zones (deck slides 73-80)."
    )

    from modules.importer import (render_import_widget,
                                   build_adjustments_template, import_adjustments)
    render_import_widget(
        label="Buffer Adjustments",
        template_fn=build_adjustments_template,
        import_fn=import_adjustments,
        template_filename="DDMRP_Adjustments_Template.xlsx",
        key="buffer_adj",
    )

    tab_list, tab_add, tab_chart = st.tabs([
        "Adjustments List", "Add Adjustment", "Factor Curves",
    ])

    with tab_list:
        _show_list()

    with tab_add:
        _show_add()

    with tab_chart:
        _show_factor_chart()


# ---------------------------------------------------------------------------
# List + delete
# ---------------------------------------------------------------------------

def _show_list():
    session = get_session()
    try:
        rows = (
            session.query(BufferAdjustment, Item)
            .join(Item, BufferAdjustment.item_id == Item.id)
            .order_by(BufferAdjustment.start_date)
            .all()
        )
    finally:
        session.close()

    if not rows:
        st.info("No planned adjustments yet. Use **Add Adjustment** to create one.")
        return

    today = date.today()
    data = []
    for adj, it in rows:
        start = adj.start_date.date() if adj.start_date else None
        end   = adj.end_date.date()   if adj.end_date   else None
        is_active = (start is None or start <= today) and (end is None or today <= end)
        data.append({
            "ID": adj.id,
            "Part": it.part_number,
            "Description": it.description,
            "Start": start.isoformat() if start else "",
            "End":   end.isoformat()   if end   else "(open)",
            "DAF":  adj.daf,
            "LTAF": adj.ltaf,
            "Red ZAF":    adj.red_zaf,
            "Yellow ZAF": adj.yellow_zaf,
            "Green ZAF":  adj.green_zaf,
            "Active now": "✅" if is_active else "—",
            "Note": adj.note or "",
        })
    st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)
    st.caption(f"{len(data)} adjustment(s).")

    st.divider()
    st.markdown("**Delete an adjustment**")
    ids = [r["ID"] for r in data]
    sel_id = st.selectbox(
        "Adjustment ID",
        ids,
        format_func=lambda i: f"#{i} — {next(r for r in data if r['ID']==i)['Part']} "
                              f"({next(r for r in data if r['ID']==i)['Start']})",
        key="del_adj_id",
    )
    if st.button("🗑️ Delete selected", type="secondary"):
        session = get_session()
        try:
            adj = session.query(BufferAdjustment).get(sel_id)
            if adj:
                session.delete(adj)
                session.commit()
                st.success(f"Deleted adjustment #{sel_id}.")
                st.rerun()
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------

def _show_add():
    session = get_session()
    try:
        items = session.query(Item).order_by(Item.part_number).all()
    finally:
        session.close()

    if not items:
        st.info("No items in Material Master yet — add an item before scheduling adjustments.")
        return

    item_options = {f"{it.part_number} — {it.description}": it.id for it in items}

    with st.form("add_adjustment_form", clear_on_submit=True):
        st.subheader("New Planned Adjustment")

        sel_label = st.selectbox("Item *", list(item_options.keys()))
        item_id = item_options[sel_label]

        c1, c2 = st.columns(2)
        with c1:
            start_d = st.date_input("Start date *", value=date.today())
        with c2:
            open_ended = st.checkbox("Open-ended (no end date)", value=False)
            end_d = st.date_input(
                "End date", value=date.today() + timedelta(days=30),
                disabled=open_ended,
            )

        st.markdown("**Demand & Lead Time factors**")
        d1, d2 = st.columns(2)
        with d1:
            daf = st.number_input(
                "DAF — Demand Adjustment Factor", min_value=0.0, max_value=10.0,
                value=1.0, step=0.1,
                help="Multiplier on ADU during the date window. 1.0 = neutral; "
                     "1.5 = +50% demand (e.g. promotion); 0.5 = -50% (phase-out).",
            )
        with d2:
            ltaf = st.number_input(
                "LTAF — Lead Time Adjustment Factor", min_value=0.0, max_value=10.0,
                value=1.0, step=0.1,
                help="Multiplier on DLT (and therefore on Red Base, Yellow). "
                     "1.0 = neutral; 2.0 = lead time doubles (e.g. supplier disruption).",
            )

        st.markdown("**Zone Adjustment Factors (per-zone overrides)**")
        z1, z2, z3 = st.columns(3)
        with z1:
            red_zaf = st.number_input(
                "Red ZAF", min_value=0.0, max_value=10.0, value=1.0, step=0.1,
                help="Multiplier on Red Zone only. >1.0 = extra safety stock.",
            )
        with z2:
            yellow_zaf = st.number_input(
                "Yellow ZAF", min_value=0.0, max_value=10.0, value=1.0, step=0.1,
                help="Multiplier on Yellow Zone only.",
            )
        with z3:
            green_zaf = st.number_input(
                "Green ZAF", min_value=0.0, max_value=10.0, value=1.0, step=0.1,
                help="Multiplier on Green Zone only. >1.0 = larger order quantities.",
            )

        note = st.text_input("Note (optional)", placeholder="e.g. Q4 promotion, supplier strike, …")

        submitted = st.form_submit_button("Add Adjustment", type="primary")

    if submitted:
        # Validate
        if not open_ended and end_d < start_d:
            st.error("End date must be on or after the start date.")
            return
        if daf == 1.0 and ltaf == 1.0 and red_zaf == 1.0 and yellow_zaf == 1.0 and green_zaf == 1.0:
            st.warning("All factors are 1.0 — this adjustment has no effect.")
            return

        session = get_session()
        try:
            adj = BufferAdjustment(
                item_id=item_id,
                start_date=datetime.combine(start_d, datetime.min.time()),
                end_date=None if open_ended else datetime.combine(end_d, datetime.min.time()),
                daf=daf, ltaf=ltaf,
                red_zaf=red_zaf, yellow_zaf=yellow_zaf, green_zaf=green_zaf,
                note=note.strip(),
            )
            session.add(adj)
            session.commit()
            st.success(f"Adjustment #{adj.id} added for {sel_label}.")

            # Preview the impact on zones today
            item = session.query(Item).get(item_id)
            z = calculate_zones(item, on_date=start_d)
            cols = st.columns(4)
            cols[0].metric("Red Zone (TOR)", f"{z.top_of_red:.1f}")
            cols[1].metric("Top of Yellow",  f"{z.top_of_yellow:.1f}")
            cols[2].metric("Top of Green",   f"{z.top_of_green:.1f}")
            cols[3].metric("Avg Inv Target", f"{z.avg_inventory_target:.1f}")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Factor curves chart (next 90 days)
# ---------------------------------------------------------------------------

def _show_factor_chart():
    session = get_session()
    try:
        items = session.query(Item).order_by(Item.part_number).all()
    finally:
        session.close()

    if not items:
        st.info("No items in Material Master yet.")
        return

    item_options = {f"{it.part_number} — {it.description}": it.id for it in items}
    sel_label = st.selectbox("Item", list(item_options.keys()), key="fac_item")
    item_id = item_options[sel_label]
    horizon = st.slider("Horizon (days)", 30, 180, 90, 30)

    session = get_session()
    try:
        item = session.query(Item).get(item_id)
    finally:
        session.close()

    adjustments = load_item_adjustments(item_id)
    if not adjustments:
        st.info("No adjustments scheduled for this item — all factors flat at 1.0.")
        return

    today = date.today()
    days, daf_curve, ltaf_curve, rz, yz, gz = [], [], [], [], [], []
    for d in range(horizon + 1):
        dd = today + timedelta(days=d)
        f = get_active_factors(item, dd, adjustments)
        days.append(dd)
        daf_curve.append(f.daf)
        ltaf_curve.append(f.ltaf)
        rz.append(f.red_zaf)
        yz.append(f.yellow_zaf)
        gz.append(f.green_zaf)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=days, y=daf_curve,  name="DAF",        line=dict(color="#2C3E50", width=3)))
    fig.add_trace(go.Scatter(x=days, y=ltaf_curve, name="LTAF",       line=dict(color="#8E44AD", width=2, dash="dash")))
    fig.add_trace(go.Scatter(x=days, y=rz,         name="Red ZAF",    line=dict(color="#E74C3C", width=2)))
    fig.add_trace(go.Scatter(x=days, y=yz,         name="Yellow ZAF", line=dict(color="#F39C12", width=2)))
    fig.add_trace(go.Scatter(x=days, y=gz,         name="Green ZAF",  line=dict(color="#27AE60", width=2)))
    fig.add_hline(y=1.0, line=dict(color="#95A5A6", dash="dot"))
    fig.update_layout(
        height=420,
        xaxis_title="Date",
        yaxis_title="Factor",
        hovermode="x unified",
        margin=dict(t=10, b=40, l=10, r=10),
        legend=dict(orientation="h", y=-0.2),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("1.0 = neutral. Values >1 lift the underlying parameter; <1 reduce it. "
               "Overlapping adjustments multiply together.")
