"""
Execution Alarms — Streamlit page (deck slides 109-118).

Five DDMRP execution alarms:
  1. Buffer Status   — on-hand vs TOR / TOG (5-band % view)
  2. Current Stock   — items at/below half-TOR right now
  3. Projected Stock — items expected to fall below TOR within the horizon
  4. Material Sync   — supply due-dates that don't align with demand timing
  5. Lead Time       — open supply orders running past expected receipt
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, datetime, timedelta

from database.db import get_session, Item, Buffer, SupplyEntry, DemandEntry
from database.auth import get_company_id
from modules.buffer_engine import (
    project_buffer_forward,
    calculate_zones,
    execution_color,
    ADU_WINDOW_DAYS,
)


# ---------------------------------------------------------------------------
# Palette / labels
# ---------------------------------------------------------------------------

EXEC_COLOR = {
    "over_tog": "#3498DB",
    "green":    "#2ECC71",
    "yellow":   "#F1C40F",
    "red":      "#E74C3C",
    "dark_red": "#7B241C",
}
EXEC_BG = {
    "over_tog": "#D6EAF8",
    "green":    "#D5F5E3",
    "yellow":   "#FDEBD0",
    "red":      "#FADBD8",
    "dark_red": "#F2D7D5",
}
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


# ---------------------------------------------------------------------------
# Page entry point
# ---------------------------------------------------------------------------

def show():
    st.header("🚨 Execution Alarms")
    st.caption(
        "DDMRP execution-side alarms (deck slides 109-118). "
        "Buffer Status % = On-Hand / TOR — the canonical execution KPI."
    )

    horizon = st.number_input(
        "Projection horizon (days) — used by Projected Stock & Sync alarms",
        min_value=7, max_value=180, value=30, step=7,
    )

    rows, signals = _load_state(int(horizon))
    if not rows:
        st.info("No items found. Add items in **Material Master** first.")
        return

    df = pd.DataFrame(rows)

    # ── KPI strip ────────────────────────────────────────────────────────────
    _alarm_summary(df, signals)
    st.divider()

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "🚦 Buffer Status",
        "📦 Current Stock",
        "📈 Projected Stock",
        "🔁 Material Sync",
        "⏱️ Lead Time",
    ])

    with tab1:
        _buffer_status_alarm(df)
    with tab2:
        _current_stock_alarm(df)
    with tab3:
        _projected_stock_alarm(df, signals)
    with tab4:
        _material_sync_alarm(df, signals, int(horizon))
    with tab5:
        _lead_time_alarm(df)


# ---------------------------------------------------------------------------
# Data loading — one pass for the whole page
# ---------------------------------------------------------------------------

def _load_state(horizon: int):
    """
    Build the per-item snapshot used by all five tabs:
      • on_hand, TOR, TOG, status %, exec band
      • forward projection (for Projected Stock & Sync tabs)
    """
    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == get_company_id()).all()
        buffers = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    rows = []
    signals = {}
    for item in items:
        buf = buffers.get(item.id)

        # Prefer persisted zones; fall back to fresh calc if buffer missing
        if buf:
            tor = buf.top_of_red or 0.0
            toy = buf.top_of_yellow or 0.0
            tog = buf.top_of_green or 0.0
            status_pct = (buf.buffer_status_pct or 0.0)
            exec_band  = (buf.execution_color or "green")
            nfp = buf.net_flow_position or 0.0
        else:
            try:
                z = calculate_zones(item)
                tor, toy, tog = z.top_of_red, z.top_of_yellow, z.top_of_green
            except Exception:
                tor = toy = tog = 0.0
            band, pct = execution_color(item.on_hand, _zones_proxy(tor, toy, tog))
            status_pct = pct
            exec_band  = band
            nfp = item.on_hand

        # Forward projection for tabs 3 & 4
        try:
            sig = project_buffer_forward(item, horizon_days=horizon)
            signals[item.id] = sig
            min_proj = min((d.projected_on_hand for d in sig.daily), default=item.on_hand)
            min_nfp  = min((d.nfp               for d in sig.daily), default=nfp)
            first_below_tor = next(
                (d.date for d in sig.daily if d.projected_on_hand < tor),
                None,
            )
        except Exception:
            sig = None
            min_proj = item.on_hand
            min_nfp  = nfp
            first_below_tor = None

        rows.append({
            "item_id":      item.id,
            "part_number":  item.part_number,
            "description":  item.description,
            "on_hand":      item.on_hand,
            "dlt":          item.dlt or 0.0,
            "tor":          tor,
            "toy":          toy,
            "tog":          tog,
            "nfp":          nfp,
            "status_pct":   status_pct,
            "exec_band":    exec_band,
            "min_proj_oh":  min_proj,
            "min_nfp":      min_nfp,
            "first_below_tor": first_below_tor,
        })

    return rows, signals


class _zones_proxy:
    """Minimal stand-in for BufferZones when only TOR/TOY/TOG are known."""
    def __init__(self, tor, toy, tog):
        self.top_of_red = tor
        self.top_of_yellow = toy
        self.top_of_green = tog


# ---------------------------------------------------------------------------
# Top-of-page summary
# ---------------------------------------------------------------------------

def _alarm_summary(df: pd.DataFrame, signals: dict):
    n_dark = (df["exec_band"] == "dark_red").sum()
    n_red  = (df["exec_band"] == "red").sum()
    n_yel  = (df["exec_band"] == "yellow").sum()
    n_grn  = (df["exec_band"] == "green").sum()
    n_over = (df["exec_band"] == "over_tog").sum()

    n_proj_break = (df["min_proj_oh"] < df["tor"]).sum()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("⚫ Stockouts",     int(n_dark))
    c2.metric("🔴 Critical",      int(n_red))
    c3.metric("🟡 Watch",          int(n_yel))
    c4.metric("🟢 OK",             int(n_grn))
    c5.metric("📘 Over-TOG",       int(n_over))
    c6.metric("📈 Projected break", int(n_proj_break),
              help="Items projected to drop below TOR within the horizon.")


# ---------------------------------------------------------------------------
# Tab 1 — Buffer Status alarm
# ---------------------------------------------------------------------------

def _buffer_status_alarm(df: pd.DataFrame):
    st.subheader("Buffer Status % — On-Hand / TOR")
    st.caption(
        "Slide 109. Items below 100% are inside the buffer; below 50% triggers the "
        "Current-Stock alarm. Above TOG = excess inventory."
    )

    sorted_df = df.copy()
    band_priority = {"dark_red": 0, "red": 1, "yellow": 2, "green": 3, "over_tog": 4}
    sorted_df["_p"] = sorted_df["exec_band"].map(band_priority).fillna(9)
    sorted_df = sorted_df.sort_values(["_p", "status_pct"]).drop(columns=["_p"])

    # Horizontal bar chart of status_pct
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=sorted_df["status_pct"] * 100.0,
        y=sorted_df["part_number"],
        orientation="h",
        marker=dict(color=[EXEC_COLOR.get(b, "#888") for b in sorted_df["exec_band"]]),
        text=[f"{v*100:.0f}%" for v in sorted_df["status_pct"]],
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>Status %: %{x:.1f}%<extra></extra>",
    ))
    fig.add_vline(x=50,  line_width=1.2, line_dash="dot", line_color="#E74C3C",
                  annotation_text="50% (Critical)")
    fig.add_vline(x=100, line_width=1.2, line_dash="dot", line_color="#F39C12",
                  annotation_text="100% (TOR)")
    fig.update_layout(
        height=max(280, len(sorted_df) * 32),
        xaxis=dict(title="Buffer Status %", range=[0, max(140, sorted_df["status_pct"].max() * 110)]),
        margin=dict(l=10, r=40, t=10, b=40),
        plot_bgcolor="white",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)

    # Detail table
    table = sorted_df.assign(
        Band=lambda d: d["exec_band"].map(lambda b: f"{EXEC_EMOJI.get(b,'⚪')} {EXEC_LABEL.get(b,b)}"),
        **{"Status %": lambda d: (d["status_pct"] * 100).round(0).astype(int).astype(str) + "%"}
    )[["Band", "part_number", "description", "on_hand", "tor", "tog", "Status %"]].rename(columns={
        "part_number": "Part Number",
        "description": "Description",
        "on_hand":     "On Hand",
        "tor":         "TOR",
        "tog":         "TOG",
    })
    st.dataframe(_styled(table, "Band"), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab 2 — Current Stock alarm
# ---------------------------------------------------------------------------

def _current_stock_alarm(df: pd.DataFrame):
    st.subheader("Current Stock — items below half-TOR right now")
    st.caption("Slide 111. Filters items whose Buffer Status % < 50% (red or stockout band).")

    crit = df[df["exec_band"].isin(["red", "dark_red"])].copy()
    if crit.empty:
        st.success("✅ No items are currently below 50% of TOR.")
        return

    crit = crit.sort_values("status_pct")
    table = crit.assign(
        Band=lambda d: d["exec_band"].map(lambda b: f"{EXEC_EMOJI.get(b,'⚪')} {EXEC_LABEL.get(b,b)}"),
        **{"Status %": lambda d: (d["status_pct"] * 100).round(0).astype(int).astype(str) + "%"},
        Shortfall=lambda d: (d["tor"] - d["on_hand"]).round(1),
    )[["Band", "part_number", "description", "on_hand", "tor", "Shortfall", "Status %"]].rename(columns={
        "part_number": "Part Number",
        "description": "Description",
        "on_hand":     "On Hand",
        "tor":         "TOR",
    })
    st.dataframe(_styled(table, "Band"), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab 3 — Projected Stock alarm
# ---------------------------------------------------------------------------

def _projected_stock_alarm(df: pd.DataFrame, signals: dict):
    st.subheader("Projected Stock — items projected to fall below TOR")
    st.caption(
        "Slide 113. For each item, simulates the next N days and flags those whose "
        "projected on-hand drops below TOR before any new order would arrive."
    )

    breaches = df[df["min_proj_oh"] < df["tor"]].copy()
    if breaches.empty:
        st.success("✅ No projected breaches over the chosen horizon.")
        return

    breaches = breaches.sort_values("first_below_tor", na_position="last")
    today = date.today()
    breaches["Days to Breach"] = breaches["first_below_tor"].apply(
        lambda d: (d - today).days if isinstance(d, date) else None
    )

    table = breaches.assign(
        Band=lambda d: d["exec_band"].map(lambda b: f"{EXEC_EMOJI.get(b,'⚪')} {EXEC_LABEL.get(b,b)}"),
        **{
            "First Breach": breaches["first_below_tor"].apply(
                lambda d: d.strftime("%Y-%m-%d") if isinstance(d, date) else "—"
            ),
            "Min Proj OH": breaches["min_proj_oh"].round(1),
        }
    )[["Band", "part_number", "description", "on_hand", "tor",
       "Min Proj OH", "First Breach", "Days to Breach"]].rename(columns={
        "part_number": "Part Number",
        "description": "Description",
        "on_hand":     "On Hand Today",
        "tor":         "TOR",
    })
    st.dataframe(_styled(table, "Band"), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab 4 — Material Sync alarm
# ---------------------------------------------------------------------------

def _material_sync_alarm(df: pd.DataFrame, signals: dict, horizon: int):
    st.subheader("Material Sync — supply that arrives after stock breaks the red zone")
    st.caption(
        "Slide 115. Flags items where projected on-hand drops below TOR before the "
        "next supply order is due to arrive — i.e. supply timing is out of sync with demand."
    )

    today_dt = datetime.utcnow()
    horizon_end = today_dt + timedelta(days=horizon)

    session = get_session()
    try:
        supply = (
            session.query(SupplyEntry)
            .filter(SupplyEntry.due_date >= today_dt,
                    SupplyEntry.due_date <= horizon_end)
            .all()
        )
    finally:
        session.close()

    next_supply: dict[int, date] = {}
    for s in supply:
        d = s.due_date.date()
        if s.item_id not in next_supply or d < next_supply[s.item_id]:
            next_supply[s.item_id] = d

    rows = []
    for r in df.to_dict("records"):
        breach = r["first_below_tor"]
        nxt    = next_supply.get(r["item_id"])
        if breach is None:
            continue  # no projected breach → nothing to sync
        if nxt is None:
            rows.append({**r, "next_supply": None, "gap_days": None,
                         "note": "No open supply within horizon"})
            continue
        if nxt > breach:
            rows.append({**r, "next_supply": nxt,
                         "gap_days": (nxt - breach).days,
                         "note": "Supply arrives after stock breaks TOR"})

    if not rows:
        st.success("✅ All projected breaches are covered by an earlier supply receipt.")
        return

    out = pd.DataFrame(rows).sort_values("first_below_tor", na_position="last")
    table = out.assign(
        Band=lambda d: d["exec_band"].map(lambda b: f"{EXEC_EMOJI.get(b,'⚪')} {EXEC_LABEL.get(b,b)}"),
        **{
            "Breach Date":    out["first_below_tor"].apply(
                lambda d: d.strftime("%Y-%m-%d") if isinstance(d, date) else "—"
            ),
            "Next Supply":    out["next_supply"].apply(
                lambda d: d.strftime("%Y-%m-%d") if isinstance(d, date) else "—"
            ),
            "Gap (days)":     out["gap_days"].fillna("—"),
        }
    )[["Band", "part_number", "description", "Breach Date", "Next Supply",
       "Gap (days)", "note"]].rename(columns={
        "part_number": "Part Number",
        "description": "Description",
        "note":        "Note",
    })
    st.dataframe(_styled(table, "Band"), use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tab 5 — Lead Time alarm
# ---------------------------------------------------------------------------

def _lead_time_alarm(df: pd.DataFrame):
    st.subheader("Lead Time — open supply orders past their expected receipt window")
    st.caption(
        "Slide 117. Open supply orders whose due-date is more than the item's DLT into "
        "the future are running long — historical placements that haven't landed yet."
    )

    today = date.today()
    today_dt = datetime.utcnow()

    session = get_session()
    try:
        rows = (
            session.query(SupplyEntry, Item)
            .join(Item, SupplyEntry.item_id == Item.id)
            .filter(SupplyEntry.due_date >= today_dt)
            .all()
        )
    finally:
        session.close()

    flagged = []
    for s, item in rows:
        dlt_days = int(round(item.dlt)) if item.dlt else 0
        days_out = (s.due_date.date() - today).days
        # Order is "running long" if its remaining wait already exceeds DLT —
        # i.e. it should not still be sitting that far out if placed correctly.
        if dlt_days > 0 and days_out > dlt_days:
            flagged.append({
                "Part Number":     item.part_number,
                "Description":     item.description,
                "Order Reference": s.order_reference or "—",
                "Quantity":        s.quantity,
                "Due Date":        s.due_date.strftime("%Y-%m-%d"),
                "Days Out":        days_out,
                "DLT":             dlt_days,
                "Excess (days)":   days_out - dlt_days,
            })

    if not flagged:
        st.success("✅ No open supply orders running past their lead-time window.")
        return

    df_lt = pd.DataFrame(flagged).sort_values("Excess (days)", ascending=False)
    st.dataframe(df_lt, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _styled(table: pd.DataFrame, band_col: str):
    """Pandas Styler that colours rows by execution band emoji in `band_col`."""
    def _row_style(row):
        emoji = str(row[band_col]).split()[0] if row[band_col] else ""
        bg = "#FFFFFF"
        for band, e in EXEC_EMOJI.items():
            if emoji == e:
                bg = EXEC_BG.get(band, "#FFFFFF")
                break
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    return table.style.apply(_row_style, axis=1)
