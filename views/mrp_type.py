"""
MRP Type — Strategic Buffer Positioning view.

Tab 1 · Per-item analysis table: 6-factor scores, recommendation, benefits.
Tab 2 · Portfolio summary: pie chart, top opportunities, aggregate KPIs.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from database.auth import get_company_id
from modules.positioning_engine import (
    analyze_all_items,
    apply_recommendation,
    DEFAULT_THRESHOLD,
    MAX_SCORE,
    PositioningResult,
)


# ── Page entry point ──────────────────────────────────────────────────────

def show():
    st.header("🎯 MRP Type — Strategic Buffer Positioning")
    st.caption(
        "Decides which items should be **DDMRP-buffered** (decoupling points) vs left as "
        "**standard MRP**, based on the 6 positioning factors from the DDMRP theory "
        "(Ptak & Smith, Ch. 6). Items missing positioning data are flagged as "
        "**❓ Incomplete data** until the fields are filled in Material Master."
    )

    # ── Controls ──────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        threshold = st.slider(
            "DDMRP threshold (0-70)",
            min_value=0, max_value=MAX_SCORE,
            value=DEFAULT_THRESHOLD, step=5,
            help="Total positioning score at or above which an item is recommended for DDMRP.",
        )
    with c2:
        if st.button("🔄 Recalculate", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    with c3:
        st.markdown(
            "<div style='padding-top:1.5rem;font-size:0.78rem;color:#7A92BB'>"
            "Edit positioning fields in <strong>Material Master → Add / Edit Item → "
            "Strategic Positioning</strong>."
            "</div>",
            unsafe_allow_html=True,
        )

    with st.spinner("Scoring items against the 6 positioning factors…"):
        results = _load_results(get_company_id(), threshold)

    if not results:
        st.info("No items yet. Add items in **Material Master** first.")
        return

    tab_table, tab_summary = st.tabs(["📋 Per-Item Analysis", "📊 Portfolio Summary"])

    with tab_table:
        _render_table(results)

    with tab_summary:
        _render_summary(results)


# ── Data loading (cached per threshold) ──────────────────────────────────

@st.cache_data(ttl=120, show_spinner=False)
def _load_results(company_id: int, threshold: int) -> list:
    """Cache-wrapped wrapper — converts dataclasses to plain dicts for caching."""
    raw = analyze_all_items(company_id, threshold=threshold)
    return [_result_to_dict(r) for r in raw]


def _result_to_dict(r: PositioningResult) -> dict:
    return {
        "item_id":           r.item_id,
        "part_number":       r.part_number,
        "description":       r.description,
        "cumulative_lt":     r.cumulative_lt,
        "decoupled_lt":      r.decoupled_lt,
        "scores":            dict(r.scores),
        "total_score":       r.total_score,
        "max_score":         r.max_score,
        "incomplete_fields": list(r.incomplete_fields),
        "recommended_type":  r.recommended_type,
        "override_type":     r.override_type,
        "effective_type":    r.effective_type,
        "reasons":           list(r.reasons),
        "lt_reduction_days": r.lt_reduction_days,
        "lt_reduction_pct":  r.lt_reduction_pct,
        "mrp_inv_value":     r.mrp_inv_value,
        "ddmrp_inv_value":   r.ddmrp_inv_value,
        "value_saving":      r.value_saving,
    }


# ── Tab 1: Per-item table ────────────────────────────────────────────────

def _render_table(results: list[dict]):
    # Filter controls
    fc1, fc2, fc3 = st.columns([2, 2, 3])
    with fc1:
        type_filter = st.selectbox(
            "Filter by recommendation",
            ["All", "DDMRP only", "MRP only", "❓ Incomplete only"],
        )
    with fc2:
        sort_by = st.selectbox(
            "Sort by",
            ["Total score (desc)", "Value saving (desc)", "Part number"],
        )
    with fc3:
        st.caption("")  # spacer

    # Filter
    filtered = results
    if type_filter == "DDMRP only":
        filtered = [r for r in results if r["recommended_type"] == "DDMRP"]
    elif type_filter == "MRP only":
        filtered = [r for r in results if r["recommended_type"] == "MRP"]
    elif type_filter == "❓ Incomplete only":
        filtered = [r for r in results if r["recommended_type"] == "INCOMPLETE"]

    # Sort
    if sort_by == "Total score (desc)":
        filtered = sorted(filtered, key=lambda r: -r["total_score"])
    elif sort_by == "Value saving (desc)":
        filtered = sorted(filtered, key=lambda r: -r["value_saving"])
    else:
        filtered = sorted(filtered, key=lambda r: r["part_number"])

    if not filtered:
        st.info("No items match the current filter.")
        return

    # Build DataFrame
    rows = []
    for r in filtered:
        rec_emoji = {"DDMRP": "🟢 DDMRP", "MRP": "🔵 MRP",
                     "INCOMPLETE": "❓ Incomplete"}[r["recommended_type"]]
        rows.append({
            "Part Number":     r["part_number"],
            "Description":     r["description"],
            "CTT":             r["scores"].get("Customer Tolerance Time", 0),
            "MPLT":            r["scores"].get("Market Potential LT", 0),
            "SOVH":            r["scores"].get("Sales Order Visibility", 0),
            "Dem Var":         r["scores"].get("Demand Variability", 0),
            "Sup Var":         r["scores"].get("Supply Variability", 0),
            "Leverage":        r["scores"].get("Inventory Leverage", 0),
            "Crit Op":         r["scores"].get("Critical Operation", 0),
            "Total / 70":      r["total_score"],
            "Recommended":     rec_emoji,
            "Override":        r["override_type"],
            "Cum LT (d)":      round(r["cumulative_lt"], 1),
            "DLT (d)":         round(r["decoupled_lt"], 1),
            "LT Saved (d)":    round(r["lt_reduction_days"], 1),
            "MRP Inv €":       round(r["mrp_inv_value"], 0),
            "DDMRP Inv €":     round(r["ddmrp_inv_value"], 0),
            "€ Saving":        round(r["value_saving"], 0),
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()

    # ── Per-item details + Apply button ───────────────────────────────────
    st.subheader("Apply Recommendation")
    st.caption(
        "Pick an item, see its score breakdown, then write the recommendation "
        "back to its master data."
    )

    item_labels = {f"{r['part_number']} — {r['description']}": r for r in filtered}
    sel_label = st.selectbox("Item", list(item_labels.keys()), key="mrp_apply_sel")
    if not sel_label:
        return
    sel = item_labels[sel_label]

    # Show breakdown
    bcol1, bcol2 = st.columns([2, 1])
    with bcol1:
        # Bar chart of per-factor scores
        score_df = pd.DataFrame({
            "Factor": list(sel["scores"].keys()),
            "Score":  list(sel["scores"].values()),
        })
        fig = px.bar(
            score_df, x="Factor", y="Score",
            range_y=[0, 10],
            title=f"Positioning factor scores — {sel['part_number']}",
            color="Score",
            color_continuous_scale=[(0, "#1E3356"), (0.5, "#FFB020"), (1.0, "#00C896")],
        )
        fig.update_layout(
            paper_bgcolor="#0C1A30", plot_bgcolor="#0C1A30",
            font_color="#E8F0FF", showlegend=False, height=320,
            margin=dict(l=20, r=20, t=50, b=20),
        )
        st.plotly_chart(fig, use_container_width=True)

    with bcol2:
        st.metric("Total Score", f"{sel['total_score']} / 70")
        rec = sel["recommended_type"]
        if rec == "DDMRP":
            st.success(f"Recommended: **🟢 DDMRP**")
        elif rec == "MRP":
            st.info(f"Recommended: **🔵 MRP**")
        else:
            st.warning(f"❓ Incomplete data — fill in Material Master")
        if sel["incomplete_fields"]:
            st.caption("Missing: " + ", ".join(sel["incomplete_fields"]))
        if sel["reasons"]:
            st.caption("Top drivers: " + ", ".join(sel["reasons"]))

        st.markdown(
            f"<div style='font-size:0.78rem;color:#7A92BB;margin-top:0.5rem'>"
            f"LT reduction: <strong style='color:#00C896'>"
            f"{sel['lt_reduction_days']:.1f} d "
            f"({sel['lt_reduction_pct']*100:.0f}%)</strong><br>"
            f"Inventory €: <strong style='color:#00C896'>"
            f"{sel['value_saving']:,.0f}</strong> saved"
            f"</div>",
            unsafe_allow_html=True,
        )

    # Apply controls
    ac1, ac2, ac3 = st.columns(3)
    with ac1:
        if st.button("✅ Apply recommendation", use_container_width=True,
                     type="primary", disabled=(rec == "INCOMPLETE")):
            apply_recommendation(sel["item_id"], rec)
            st.cache_data.clear()
            st.success(f"Set **{sel['part_number']}** to **{rec}**.")
            st.rerun()
    with ac2:
        if st.button("🔵 Force MRP", use_container_width=True):
            apply_recommendation(sel["item_id"], "MRP")
            st.cache_data.clear()
            st.rerun()
    with ac3:
        if st.button("🟢 Force DDMRP", use_container_width=True):
            apply_recommendation(sel["item_id"], "DDMRP")
            st.cache_data.clear()
            st.rerun()


# ── Tab 2: Portfolio summary ─────────────────────────────────────────────

def _render_summary(results: list[dict]):
    n_total      = len(results)
    n_ddmrp      = sum(1 for r in results if r["recommended_type"] == "DDMRP")
    n_mrp        = sum(1 for r in results if r["recommended_type"] == "MRP")
    n_incomplete = sum(1 for r in results if r["recommended_type"] == "INCOMPLETE")

    total_saving = sum(r["value_saving"] for r in results)
    total_lt_days = sum(r["lt_reduction_days"] for r in results
                        if r["recommended_type"] == "DDMRP")

    # KPI strip
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Items analyzed",        f"{n_total}")
    k2.metric("DDMRP candidates",      f"{n_ddmrp}", f"{n_ddmrp/n_total*100:.0f}%" if n_total else "")
    k3.metric("Cumulative LT saved",   f"{total_lt_days:.0f} d")
    k4.metric("Inventory € savings",   f"€ {total_saving:,.0f}")

    if n_incomplete:
        st.warning(
            f"❓ {n_incomplete} item{'s' if n_incomplete != 1 else ''} have incomplete "
            f"positioning data — fill the missing fields in Material Master to score them."
        )

    st.divider()

    # ── Pie: recommendation distribution ──────────────────────────────────
    pcol, bcol = st.columns(2)
    with pcol:
        pie_df = pd.DataFrame({
            "Type":  ["🟢 DDMRP", "🔵 MRP", "❓ Incomplete"],
            "Count": [n_ddmrp, n_mrp, n_incomplete],
        })
        pie_df = pie_df[pie_df["Count"] > 0]
        if not pie_df.empty:
            fig_pie = px.pie(
                pie_df, names="Type", values="Count",
                title="Recommendation distribution",
                color="Type",
                color_discrete_map={
                    "🟢 DDMRP":      "#00C896",
                    "🔵 MRP":        "#1565FF",
                    "❓ Incomplete": "#FFB020",
                },
                hole=0.45,
            )
            fig_pie.update_layout(
                paper_bgcolor="#0C1A30", plot_bgcolor="#0C1A30",
                font_color="#E8F0FF", height=350,
                margin=dict(l=20, r=20, t=50, b=20),
            )
            st.plotly_chart(fig_pie, use_container_width=True)

    with bcol:
        # Bar: top 10 € saving opportunities
        ddmrp_only = [r for r in results
                      if r["recommended_type"] == "DDMRP" and r["value_saving"] > 0]
        ddmrp_only = sorted(ddmrp_only, key=lambda r: -r["value_saving"])[:10]
        if ddmrp_only:
            bar_df = pd.DataFrame({
                "Part":   [r["part_number"] for r in ddmrp_only],
                "€ Save": [r["value_saving"] for r in ddmrp_only],
            })
            fig_bar = px.bar(
                bar_df, x="€ Save", y="Part", orientation="h",
                title="Top 10 € savings — DDMRP candidates",
                color="€ Save",
                color_continuous_scale=[(0, "#1565FF"), (1.0, "#00C896")],
            )
            fig_bar.update_layout(
                paper_bgcolor="#0C1A30", plot_bgcolor="#0C1A30",
                font_color="#E8F0FF", height=350,
                margin=dict(l=20, r=20, t=50, b=20),
                yaxis={"categoryorder": "total ascending"},
                showlegend=False,
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    st.divider()

    # ── Lead-time reduction histogram ─────────────────────────────────────
    ddmrp_ress = [r for r in results
                  if r["recommended_type"] == "DDMRP" and r["lt_reduction_days"] > 0]
    if ddmrp_ress:
        lt_df = pd.DataFrame({
            "Part":          [r["part_number"] for r in ddmrp_ress],
            "LT Saved (d)":  [r["lt_reduction_days"] for r in ddmrp_ress],
            "LT Saved %":    [r["lt_reduction_pct"] * 100 for r in ddmrp_ress],
        })
        fig_hist = px.bar(
            lt_df.sort_values("LT Saved (d)", ascending=True).tail(15),
            x="LT Saved (d)", y="Part", orientation="h",
            title="Top 15 — Lead-time days saved by decoupling",
            hover_data={"LT Saved %": ":.0f"},
            color="LT Saved (d)",
            color_continuous_scale="Teal",
        )
        fig_hist.update_layout(
            paper_bgcolor="#0C1A30", plot_bgcolor="#0C1A30",
            font_color="#E8F0FF", height=400,
            margin=dict(l=20, r=20, t=50, b=20),
            showlegend=False,
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    # ── Detailed table of DDMRP candidates ────────────────────────────────
    st.subheader("DDMRP candidates — detailed savings")
    cand = [r for r in results if r["recommended_type"] == "DDMRP"]
    if not cand:
        st.info("No items currently score above the DDMRP threshold.")
        return
    cand = sorted(cand, key=lambda r: -r["value_saving"])
    df = pd.DataFrame([{
        "Part Number":     r["part_number"],
        "Description":     r["description"],
        "Score":           f"{r['total_score']} / 70",
        "Cum LT (d)":      round(r["cumulative_lt"], 1),
        "DLT (d)":         round(r["decoupled_lt"], 1),
        "LT Saved (d)":    round(r["lt_reduction_days"], 1),
        "LT Saved %":      f"{r['lt_reduction_pct']*100:.0f}%",
        "MRP Inv €":       f"{r['mrp_inv_value']:,.0f}",
        "DDMRP Inv €":     f"{r['ddmrp_inv_value']:,.0f}",
        "€ Saving":        f"{r['value_saving']:,.0f}",
        "Top Drivers":     ", ".join(r["reasons"]),
    } for r in cand])
    st.dataframe(df, use_container_width=True, hide_index=True)
