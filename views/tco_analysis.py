"""
Total Cost of Ownership (TCO) — Streamlit page.

Rank suppliers on more than unit price: quality cost, delivery cost,
service cost, technology cost and risk cost are layered on top of the
raw purchase value. The engine pulls volume from supply_entries × item
unit cost; the user can run a fresh evaluation, override any component
by hand, and save snapshots. The AI agent can also propose snapshots
through the Pending Changes queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import plotly.express as px

from database.auth import get_company_id
from database.db import get_session, Supplier
from modules.tco import (
    QUALITY_FACTOR, DELIVERY_FACTOR, SERVICE_FACTOR, RISK_IMPACT_EUR,
    compute_tco_all, compute_tco, save_tco,
    list_tco_snapshots, delete_tco_snapshot,
    to_dict,
)


def show():
    st.header("💰 Total Cost of Ownership")
    st.caption(
        "Rank suppliers by full cost-per-unit, not just unit price. "
        "Costs are computed from supply entries × item unit cost, surrounded "
        "by quality / delivery / service / technology / risk components."
    )

    company_id = get_company_id()
    if not company_id:
        st.error("No active company.")
        return

    tab_run, tab_save, tab_history = st.tabs([
        "🧮 Run TCO Analysis", "✍️ Override & Save", "📚 Saved Snapshots"
    ])

    with tab_run:
        _render_run_tab(company_id)

    with tab_save:
        _render_override_tab(company_id)

    with tab_history:
        _render_history_tab(company_id)


# ─────────────────────────────────────────────────────────────────────────────
# Run analysis — auto-compute across all suppliers
# ─────────────────────────────────────────────────────────────────────────────

def _render_run_tab(company_id: int):
    st.subheader("Run a portfolio-wide TCO calculation")

    today = datetime.utcnow().date()
    default_start = today - timedelta(days=365)

    c1, c2, c3 = st.columns(3)
    period_start = c1.date_input("Period start", value=default_start,
                                 key="tco_period_start")
    period_end   = c2.date_input("Period end",   value=today,
                                 key="tco_period_end")
    c3.metric("Days in window", (period_end - period_start).days)

    with st.expander("⚙️ Cost factors (advanced)", expanded=False):
        f1, f2, f3, f4 = st.columns(4)
        q_factor = f1.number_input("Quality factor",  0.0, 5.0,
                                   value=float(QUALITY_FACTOR), step=0.05,
                                   help="Multiplier applied to (100% − reliability) × purchase. "
                                        "Higher = more punishment for unreliable suppliers.")
        d_factor = f2.number_input("Delivery factor", 0.0, 5.0,
                                   value=float(DELIVERY_FACTOR), step=0.05,
                                   help="Multiplier on the reliability deficit for "
                                        "expediting / freight upgrades.")
        s_factor = f3.number_input("Service factor",  0.0, 1.0,
                                   value=float(SERVICE_FACTOR), step=0.005,
                                   format="%.3f",
                                   help="Flat % of purchase value attributed to "
                                        "admin / after-sales / support.")
        r_factor = f4.number_input("Risk € per score point", 0.0, 1e6,
                                   value=float(RISK_IMPACT_EUR), step=100.0,
                                   help="Euro value per 1 point of inherent risk score "
                                        "(L × I), summed across open/mitigating risks "
                                        "tagged to the supplier.")

    if st.button("▶️ Run TCO calculation", type="primary",
                 use_container_width=True, key="tco_run_btn"):
        start_dt = datetime.combine(period_start, datetime.min.time())
        end_dt   = datetime.combine(period_end,   datetime.max.time())
        with st.spinner("Computing TCO across all suppliers…"):
            results = compute_tco_all(
                company_id,
                period_start=start_dt, period_end=end_dt,
                quality_factor=q_factor, delivery_factor=d_factor,
                service_factor=s_factor, risk_impact_eur=r_factor,
            )
        st.session_state["tco_last_results"]  = [to_dict(r) for r in results]
        st.session_state["tco_last_period"]   = (period_start.isoformat(),
                                                  period_end.isoformat())

    results = st.session_state.get("tco_last_results", [])
    if not results:
        st.info("Click **Run TCO calculation** to evaluate every supplier in the period.")
        return

    df = pd.DataFrame(results)
    df_display = df.copy()
    cols = ["supplier_code", "supplier_name", "units_delivered", "unit_price_avg",
            "purchase_cost", "quality_cost", "delivery_cost", "service_cost",
            "technology_cost", "risk_cost", "total_cost", "tco_per_unit",
            "reliability_pct", "open_risks"]
    df_display = df_display[cols]

    st.markdown("### Ranking — lowest TCO per unit first")
    st.dataframe(df_display, use_container_width=True, hide_index=True,
                 column_config={
                     "purchase_cost":   st.column_config.NumberColumn("Purchase €",   format="%.2f"),
                     "quality_cost":    st.column_config.NumberColumn("Quality €",    format="%.2f"),
                     "delivery_cost":   st.column_config.NumberColumn("Delivery €",   format="%.2f"),
                     "service_cost":    st.column_config.NumberColumn("Service €",    format="%.2f"),
                     "technology_cost": st.column_config.NumberColumn("Tech €",       format="%.2f"),
                     "risk_cost":       st.column_config.NumberColumn("Risk €",       format="%.2f"),
                     "total_cost":      st.column_config.NumberColumn("Total €",      format="%.2f"),
                     "tco_per_unit":    st.column_config.NumberColumn("TCO / unit €", format="%.4f"),
                     "reliability_pct": st.column_config.NumberColumn("Reliability %",format="%.1f"),
                 })

    # Stacked bar — cost composition per supplier
    rows = []
    for r in results:
        for comp in ("purchase_cost", "quality_cost", "delivery_cost",
                     "service_cost", "technology_cost", "risk_cost"):
            rows.append({"supplier": r["supplier_code"],
                         "component": comp.replace("_cost", "").title(),
                         "amount": r[comp]})
    comp_df = pd.DataFrame(rows)
    if not comp_df.empty:
        st.markdown("### Cost composition by supplier")
        fig = px.bar(comp_df, x="supplier", y="amount", color="component",
                     title="Stacked € by supplier — same period",
                     labels={"amount": "€", "supplier": "Supplier"})
        st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Override & save — per-supplier manual entry
# ─────────────────────────────────────────────────────────────────────────────

def _render_override_tab(company_id: int):
    st.subheader("Override components and save a snapshot")

    session = get_session()
    try:
        suppliers = (session.query(Supplier)
                     .filter(Supplier.company_id == company_id)
                     .order_by(Supplier.code).all())
    finally:
        session.close()

    if not suppliers:
        st.warning("No suppliers in the company.")
        return

    options = {f"{s.code} — {s.name}": s.id for s in suppliers}
    pick = st.selectbox("Supplier", list(options.keys()), key="tco_override_pick")
    sup_id = options[pick]

    today = datetime.utcnow().date()
    c1, c2 = st.columns(2)
    period_start = c1.date_input("Period start",
                                 value=today - timedelta(days=365),
                                 key="tco_override_start")
    period_end   = c2.date_input("Period end", value=today,
                                 key="tco_override_end")

    if st.button("Compute baseline", key="tco_override_compute"):
        try:
            result = compute_tco(
                supplier_id=sup_id, company_id=company_id,
                period_start=datetime.combine(period_start, datetime.min.time()),
                period_end=datetime.combine(period_end, datetime.max.time()),
            )
            st.session_state["tco_override_baseline"] = to_dict(result)
        except Exception as exc:
            st.error(f"Compute failed: {exc}")

    baseline = st.session_state.get("tco_override_baseline")
    if not baseline or baseline["supplier_id"] != sup_id:
        st.info("Click **Compute baseline** to load auto-computed values for this supplier.")
        return

    st.markdown(f"**Baseline** — {baseline['supplier_name']}  ·  "
                f"{baseline['units_delivered']:.0f} units @ avg "
                f"€{baseline['unit_price_avg']:.4f}")

    e1, e2 = st.columns(2)
    purchase   = e1.number_input("Purchase €",   0.0, value=float(baseline["purchase_cost"]),
                                  step=100.0, key="tco_ov_purchase")
    quality    = e2.number_input("Quality €",    0.0, value=float(baseline["quality_cost"]),
                                  step=100.0, key="tco_ov_quality")
    e3, e4 = st.columns(2)
    delivery   = e3.number_input("Delivery €",   0.0, value=float(baseline["delivery_cost"]),
                                  step=100.0, key="tco_ov_delivery")
    service    = e4.number_input("Service €",    0.0, value=float(baseline["service_cost"]),
                                  step=100.0, key="tco_ov_service")
    e5, e6 = st.columns(2)
    technology = e5.number_input("Technology €", 0.0, value=float(baseline["technology_cost"]),
                                  step=100.0, key="tco_ov_tech")
    risk_cost  = e6.number_input("Risk €",       0.0, value=float(baseline["risk_cost"]),
                                  step=100.0, key="tco_ov_risk")
    notes = st.text_area("Notes", value="", key="tco_ov_notes",
                         placeholder="Why are you overriding the auto-computed values?")

    if st.button("💾 Save snapshot", type="primary", key="tco_ov_save"):
        from dataclasses import dataclass
        # Rebuild TCOResult from the baseline dict
        from modules.tco import TCOResult
        r = TCOResult(**baseline)
        r.notes = notes.strip()
        overrides = {
            "purchase_cost":   purchase,
            "quality_cost":    quality,
            "delivery_cost":   delivery,
            "service_cost":    service,
            "technology_cost": technology,
            "risk_cost":       risk_cost,
        }
        # Detect if any value actually differs from baseline
        edited = any(abs(overrides[k] - baseline[k]) > 1e-6 for k in overrides)
        snap_id = save_tco(company_id=company_id, result=r,
                           auto_computed=not edited,
                           overrides=overrides if edited else None)
        st.success(f"Saved snapshot #{snap_id}.")


# ─────────────────────────────────────────────────────────────────────────────
# History — list saved snapshots
# ─────────────────────────────────────────────────────────────────────────────

def _render_history_tab(company_id: int):
    st.subheader("Saved TCO snapshots")

    rows = list_tco_snapshots(company_id=company_id, limit=200)
    if not rows:
        st.info("No saved snapshots yet. Use **Override & Save** to persist a TCO row.")
        return

    df = pd.DataFrame(rows)
    df_display = df[[
        "id", "supplier_code", "supplier_name", "period_start", "period_end",
        "units_delivered", "purchase_cost", "quality_cost", "delivery_cost",
        "service_cost", "technology_cost", "risk_cost", "total_cost",
        "tco_per_unit", "auto_computed", "created_at",
    ]]
    st.dataframe(df_display, use_container_width=True, hide_index=True,
                 column_config={
                     "purchase_cost":   st.column_config.NumberColumn("Purchase €",   format="%.2f"),
                     "quality_cost":    st.column_config.NumberColumn("Quality €",    format="%.2f"),
                     "delivery_cost":   st.column_config.NumberColumn("Delivery €",   format="%.2f"),
                     "service_cost":    st.column_config.NumberColumn("Service €",    format="%.2f"),
                     "technology_cost": st.column_config.NumberColumn("Tech €",       format="%.2f"),
                     "risk_cost":       st.column_config.NumberColumn("Risk €",       format="%.2f"),
                     "total_cost":      st.column_config.NumberColumn("Total €",      format="%.2f"),
                     "tco_per_unit":    st.column_config.NumberColumn("TCO / unit €", format="%.4f"),
                 })

    with st.expander("🗑️ Delete a snapshot"):
        ids = [r["id"] for r in rows]
        chosen = st.selectbox("Snapshot id", ids, key="tco_history_del_pick")
        if st.button("Delete selected", key="tco_history_del_btn"):
            ok = delete_tco_snapshot(int(chosen), company_id)
            if ok:
                st.success(f"Snapshot #{chosen} deleted.")
                st.rerun()
            else:
                st.error("Could not delete (not found or cross-company).")
