"""
Model Velocity — Streamlit page (deck slide 131).

Model Velocity = Green / ADU  −  Actual number of orders in the period.

A positive value means fewer actual orders than the buffer model expects
  (item is running "too slow" — demand lower than modelled, buffer oversized).
A negative value means more actual orders than expected
  (item is running "too fast" — demand higher than modelled, buffer undersized).

DDS&OP (Demand-Driven Sales & Operations Planning) uses Model Velocity to:
  • Flag candidates for Dynamic Buffer Adjustment (DBA) up / down.
  • Detect systemic over-buffering or under-buffering across the fleet.
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta, date
from database.db import get_session, Item, Buffer, SupplyEntry


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def compute_model_velocity(
    window_days: int = 30,
) -> list[dict]:
    """
    For each item with a calculated buffer:
      model_order_freq  = Green / ADU          (days between orders, per model)
      model_orders_expected = window_days / model_order_freq
      actual_orders     = supply orders placed (created) within last window_days
      velocity          = actual_orders - model_orders_expected
                          > 0 → too fast (more orders than model)
                          < 0 → too slow (fewer orders than model)
    """
    cutoff = datetime.utcnow() - timedelta(days=window_days)

    session = get_session()
    try:
        items    = {it.id: it for it in session.query(Item).all()}
        buffers  = {b.item_id: b for b in session.query(Buffer).all()}
        # Count supply orders placed within the window by item
        supply   = (
            session.query(SupplyEntry)
            .filter(SupplyEntry.due_date >= cutoff)
            .all()
        )
    finally:
        session.close()

    order_counts: dict[int, int] = {}
    for s in supply:
        order_counts[s.item_id] = order_counts.get(s.item_id, 0) + 1

    rows = []
    for iid, it in items.items():
        buf = buffers.get(iid)
        if buf is None:
            continue

        green = buf.green_zone or 0.0
        adu   = buf.dynamic_adu or it.adu or 0.0

        if adu <= 0 or green <= 0:
            model_freq = None
            expected   = None
        else:
            model_freq = green / adu   # days between orders per model
            expected   = window_days / model_freq

        actual  = order_counts.get(iid, 0)
        velocity = (actual - expected) if expected is not None else None

        rows.append({
            "item_id":       iid,
            "part_number":   it.part_number,
            "description":   it.description,
            "adu":           round(adu, 3),
            "green":         round(green, 1),
            "tor":           round(buf.top_of_red or 0.0, 1),
            "tog":           round(buf.top_of_green or 0.0, 1),
            "model_freq":    round(model_freq, 1) if model_freq else None,
            "expected":      round(expected, 2) if expected else None,
            "actual":        actual,
            "velocity":      round(velocity, 2) if velocity is not None else None,
        })

    return rows


# ---------------------------------------------------------------------------
# Streamlit page
# ---------------------------------------------------------------------------

def show():
    st.header("Model Velocity")
    st.caption(
        "**Model Velocity = Actual orders − Expected orders** in the review period "
        "(deck slide 131). Flags items running faster or slower than the buffer model predicts, "
        "driving DBA (Dynamic Buffer Adjustment) decisions in DDS&OP."
    )

    window = st.number_input(
        "Review window (days)", min_value=7, max_value=180, value=30, step=7,
        key="mv_window",
    )

    rows = compute_model_velocity(int(window))

    if not rows:
        st.info("No items with calculated buffers found. Run buffer calculations first.")
        return

    df = pd.DataFrame(rows)

    # ── KPI strip ────────────────────────────────────────────────────────────
    valid = df.dropna(subset=["velocity"])
    n_fast   = (valid["velocity"] > 0.5).sum()
    n_slow   = (valid["velocity"] < -0.5).sum()
    n_ok     = len(valid) - n_fast - n_slow
    n_nodata = df["velocity"].isna().sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("⚡ Too Fast",     int(n_fast),   help="More actual orders than model expected")
    c2.metric("🐢 Too Slow",     int(n_slow),   help="Fewer actual orders than model expected")
    c3.metric("✅ On Model",      int(n_ok))
    c4.metric("❓ No Data / ADU=0", int(n_nodata))

    st.divider()

    tab_chart, tab_table, tab_dba = st.tabs([
        "📊 Velocity Chart",
        "📋 Detail Table",
        "🔧 DBA Recommendations",
    ])

    with tab_chart:
        _velocity_chart(df)

    with tab_table:
        _detail_table(df, int(window))

    with tab_dba:
        _dba_recommendations(df)


def _velocity_chart(df: pd.DataFrame):
    valid = df.dropna(subset=["velocity"]).sort_values("velocity")
    if valid.empty:
        st.info("No velocity data available.")
        return

    colors = [
        "#E74C3C" if v > 0.5 else ("#3498DB" if v < -0.5 else "#2ECC71")
        for v in valid["velocity"]
    ]

    fig = go.Figure(go.Bar(
        x=valid["part_number"],
        y=valid["velocity"],
        marker_color=colors,
        text=[f"{v:+.1f}" for v in valid["velocity"]],
        textposition="outside",
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Velocity: %{y:+.2f}<br>"
            "<extra></extra>"
        ),
    ))
    fig.add_hline(y=0,    line_color="#2C3E50", line_width=1.5)
    fig.add_hline(y=0.5,  line_dash="dot", line_color="#E74C3C",
                  annotation_text="+0.5 (too fast)",  annotation_position="top right")
    fig.add_hline(y=-0.5, line_dash="dot", line_color="#3498DB",
                  annotation_text="-0.5 (too slow)", annotation_position="bottom right")

    fig.update_layout(
        height=400,
        xaxis_title="Item",
        yaxis_title="Model Velocity (actual − expected orders)",
        margin=dict(t=20, b=60, l=50, r=80),
        plot_bgcolor="white",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "🔴 = too fast (demand higher than modelled — consider increasing ADU/buffer). "
        "🔵 = too slow (demand lower than modelled — consider DBA down or buffer reduction). "
        "🟢 = on model."
    )


def _detail_table(df: pd.DataFrame, window: int):
    st.subheader(f"Model Velocity — last {window} days")

    display = df.copy()
    display["velocity_label"] = display["velocity"].apply(
        lambda v: "⚡ Too Fast" if v is not None and v > 0.5
        else ("🐢 Too Slow" if v is not None and v < -0.5
              else ("✅ On Model" if v is not None else "❓ N/A"))
    )
    display["model_freq"]  = display["model_freq"].apply(lambda v: f"{v:.1f} d" if v else "—")
    display["expected"]    = display["expected"].apply(lambda v: f"{v:.1f}" if v else "—")
    display["velocity"]    = display["velocity"].apply(lambda v: f"{v:+.2f}" if v is not None else "—")

    cols = ["velocity_label", "part_number", "description", "adu",
            "green", "model_freq", "expected", "actual", "velocity"]
    rename = {
        "velocity_label": "Assessment",
        "part_number":    "Part Number",
        "description":    "Description",
        "adu":            "ADU",
        "green":          "Green Zone",
        "model_freq":     "Model Freq",
        "expected":       "Expected Orders",
        "actual":         "Actual Orders",
        "velocity":       "Velocity",
    }

    def _sty(row):
        lbl = str(row["Assessment"])
        if "Fast" in lbl:
            return ["background-color: #FADBD8; color: #1A1A1A"] * len(row)
        if "Slow" in lbl:
            return ["background-color: #D6EAF8; color: #1A1A1A"] * len(row)
        if "Model" in lbl:
            return ["background-color: #D5F5E3; color: #1A1A1A"] * len(row)
        return [""] * len(row)

    st.dataframe(
        display[cols].rename(columns=rename).style.apply(_sty, axis=1),
        use_container_width=True,
        hide_index=True,
    )


def _dba_recommendations(df: pd.DataFrame):
    """
    Dynamic Buffer Adjustment guidance based on velocity.
    Too fast → DBA up (increase ADU or green zone multiplier).
    Too slow → DBA down (decrease ADU or consider buffer reduction).
    """
    st.subheader("DBA Recommendations")
    st.caption(
        "Based on Model Velocity deviations. "
        "Apply as DAF (Demand Adjustment Factor) in the Buffer Adjustments page."
    )

    valid = df.dropna(subset=["velocity"])
    fast  = valid[valid["velocity"] >  0.5].sort_values("velocity", ascending=False)
    slow  = valid[valid["velocity"] < -0.5].sort_values("velocity")

    if fast.empty and slow.empty:
        st.success("✅ All items are within ±0.5 orders of the model. No DBA adjustments recommended.")
        return

    if not fast.empty:
        st.markdown("#### ⚡ Increase buffer (DBA up) — Too Fast")
        st.caption("These items are receiving more orders than the model expects. "
                   "Consider applying a DAF > 1.0 in Buffer Adjustments.")
        rows = []
        for _, r in fast.iterrows():
            # Suggested DAF = actual / expected (ratio of demand vs model)
            suggested_daf = round((r["actual"] / r["expected"]), 2) if r["expected"] and r["expected"] > 0 else "—"
            rows.append({
                "Part Number":    r["part_number"],
                "Description":    r["description"],
                "Actual Orders":  int(r["actual"]),
                "Expected":       f"{r['expected']:.1f}",
                "Velocity":       f"{r['velocity']:+.2f}",
                "Suggested DAF":  suggested_daf,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if not slow.empty:
        st.markdown("#### 🐢 Reduce buffer (DBA down) — Too Slow")
        st.caption("These items are receiving fewer orders than the model expects. "
                   "Consider applying a DAF < 1.0 in Buffer Adjustments.")
        rows = []
        for _, r in slow.iterrows():
            suggested_daf = round((r["actual"] / r["expected"]), 2) if r["expected"] and r["expected"] > 0 else "—"
            rows.append({
                "Part Number":    r["part_number"],
                "Description":    r["description"],
                "Actual Orders":  int(r["actual"]),
                "Expected":       f"{r['expected']:.1f}",
                "Velocity":       f"{r['velocity']:+.2f}",
                "Suggested DAF":  suggested_daf,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
