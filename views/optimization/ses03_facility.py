"""
Session 3 — Facility Location view.

Two tabs:
  • Configure & Run  — horizon, UFLP/CFLP mode, sourcing, facility-count
                       limits, preview of candidate facilities & customers
  • Results          — KPIs, open/closed facilities, assignment Sankey,
                       cost breakdown, utilization, costly customers
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from database.auth import get_company_id, get_current_user
from database.db import Base, engine, Location, Lane, LocationDemand
from modules.optimization.data_adapters import load_facility_inputs
from modules.optimization.ses03_facility import solve as solve_facility
from modules.optimization.solver_base import record_run
from views.optimization._shared import (
    render_run_history,
    load_run_payloads,
    status_badge,
)


def show() -> None:
    # Defensive: ensure network tables exist (no-op if already created).
    try:
        Base.metadata.create_all(engine, tables=[
            Location.__table__, Lane.__table__, LocationDemand.__table__,
        ])
    except Exception:
        pass

    company_id = get_company_id()
    user       = get_current_user()
    user_id    = user.get("id") if user else None

    top = st.columns([6, 1])
    with top[0]:
        st.title("🏭 Session 3 — Facility Location")
        st.caption("Ch7 UFLP / CFLP · solver: PuLP / CBC · snapshots expire in 10 days")
    with top[1]:
        if st.button("← Catalogue", use_container_width=True):
            st.session_state.optimization_session = None
            st.rerun()

    st.divider()

    tab_run, tab_results = st.tabs(["🛠️ Configure & Run", "📊 Results"])

    # ── Tab: Configure & Run ─────────────────────────────────────────────────
    with tab_run:
        st.markdown("##### Demand horizon")
        c1, c2 = st.columns(2)
        with c1:
            horizon_days = st.number_input(
                "Demand horizon (days)", min_value=7, max_value=365,
                value=30, step=1,
                help="Customer demand = per-location demand × horizon/30, or "
                     "Σ(item ADU) × horizon if no per-location demand is defined.",
            )
        with c2:
            scenario_name = st.text_input(
                "Scenario name (optional)",
                value=f"facility · {horizon_days}d",
            )

        try:
            inputs = load_facility_inputs(company_id, horizon_days=int(horizon_days))
        except Exception as exc:
            st.error(f"Failed to load inputs: {exc}")
            return

        n_fac = len(inputs["facilities"])
        cust_with_demand = [c for c in inputs["customers"] if c["demand"] > 0]
        n_cust = len(cust_with_demand)
        total_dem = sum(c["demand"] for c in inputs["customers"])
        total_fixed = sum(f["fixed_open_cost"] for f in inputs["facilities"])

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Candidate facilities", n_fac)
        k2.metric("Customers w/ demand", n_cust)
        k3.metric("Total demand", f"{total_dem:,.0f}")
        k4.metric("Σ fixed open cost", f"€ {total_fixed:,.0f}")

        if n_fac == 0 or n_cust == 0:
            st.warning(
                "Need at least one plant/dc Location (candidate facility) and "
                "one customer Location with demand. Add them in "
                "Master Data → Network Master."
            )

        st.markdown("##### Model options")
        o1, o2 = st.columns(2)
        with o1:
            respect_capacity = st.checkbox(
                "Respect facility capacity (CFLP)", value=True,
                help="On → each open facility limited to throughput_capacity × "
                     "horizon (CFLP). Off → uncapacitated (UFLP).",
            )
        with o2:
            single_source = st.checkbox(
                "Single-source each customer", value=True,
                help="On → each customer served by exactly one facility. "
                     "Off → demand may be split across facilities.",
            )

        st.markdown("##### Facility-count limits (optional)")
        l1, l2, l3 = st.columns(3)
        with l1:
            use_force_p = st.checkbox("Force exact count", value=False)
            force_p = st.number_input(
                "Open exactly", min_value=1, max_value=max(1, n_fac),
                value=min(1, n_fac) or 1, step=1, disabled=not use_force_p,
            ) if n_fac else None
        with l2:
            min_open = st.number_input(
                "Min open", min_value=0, max_value=max(0, n_fac),
                value=0, step=1, disabled=use_force_p,
            )
        with l3:
            max_open = st.number_input(
                "Max open (0 = no limit)", min_value=0, max_value=max(0, n_fac),
                value=0, step=1, disabled=use_force_p,
            )

        with st.expander("Candidate facilities"):
            if inputs["facilities"]:
                df = pd.DataFrame(inputs["facilities"])
                st.dataframe(
                    df, use_container_width=True, height=240,
                    column_config={
                        "fixed_open_cost": st.column_config.NumberColumn("Fixed € / period", format="€ %.0f"),
                        "capacity":        st.column_config.NumberColumn("Capacity (period)", format="%.0f"),
                    },
                )
            else:
                st.info("No candidate facilities.")

        with st.expander("Customers & demand"):
            if cust_with_demand:
                st.dataframe(
                    pd.DataFrame(cust_with_demand),
                    use_container_width=True, height=240,
                    column_config={
                        "demand": st.column_config.NumberColumn("Demand (period)", format="%.0f"),
                    },
                )
            else:
                st.info("No customer demand.")

        st.divider()
        if st.button("🚀 Solve facility location", type="primary",
                     use_container_width=True,
                     disabled=(n_fac == 0 or n_cust == 0)):
            params = {
                **inputs,
                "respect_capacity": respect_capacity,
                "single_source":    single_source,
                "force_p":          int(force_p) if use_force_p and force_p else None,
                "min_open":         int(min_open) if (not use_force_p and min_open) else None,
                "max_open":         int(max_open) if (not use_force_p and max_open) else None,
            }
            try:
                with st.spinner("Solving facility-location MILP…"):
                    result = solve_facility(params)
                run_id = record_run(
                    company_id=company_id,
                    user_id=user_id,
                    session_key="ses03_facility",
                    scenario_name=scenario_name,
                    params={
                        "horizon_days":     int(horizon_days),
                        "n_facilities":     n_fac,
                        "n_customers":      n_cust,
                        "respect_capacity": respect_capacity,
                        "single_source":    single_source,
                        "force_p":          params["force_p"],
                        "min_open":         params["min_open"],
                        "max_open":         params["max_open"],
                    },
                    result=result,
                )
                if result.status == "solved":
                    st.success(
                        f"✓ Solved in {result.runtime_ms} ms · "
                        f"objective = € {result.objective_value:,.2f} · run #{run_id}"
                    )
                    st.session_state["ses03_last_run_id"] = run_id
                else:
                    st.error(f"Status: {result.status}. {result.error_message or ''}")
            except Exception as exc:
                st.error(f"Solver error: {exc}")

        st.divider()
        render_run_history(
            company_id, "ses03_facility",
            on_select=lambda rid: st.session_state.update({"ses03_last_run_id": rid}),
        )

    # ── Tab: Results ─────────────────────────────────────────────────────────
    with tab_results:
        run_id = st.session_state.get("ses03_last_run_id")
        if not run_id:
            st.info("Run a scenario from the **Configure & Run** tab to see results here.")
            return
        payloads = load_run_payloads(int(run_id), company_id)
        if not payloads:
            st.error("Run not found (it may have expired).")
            return
        _render_results(payloads["_run"], payloads)


def _render_results(run, payloads: dict) -> None:
    st.markdown(
        f"### Run #{run.id} — {run.scenario_name or '—'}  "
        f"{status_badge(run.status)}",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Created {run.created_at.strftime('%Y-%m-%d %H:%M')} · "
        f"solver {run.solver_used} · {run.runtime_ms or 0} ms · "
        f"expires {run.expires_at.strftime('%Y-%m-%d')}"
    )

    if run.status != "solved":
        st.error(f"Status: {run.status}. {run.error_message or ''}")
        return

    summary    = payloads.get("summary", {})
    chart_data = payloads.get("chart_data", {})
    assignments = payloads.get("allocation", {}).get("assignments", [])
    sens       = payloads.get("sensitivity", {})

    # KPI row
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total cost", f"€ {summary.get('total_cost', 0):,.0f}")
    k2.metric("Facilities open",
              f"{summary.get('facilities_open', 0)} / {summary.get('facilities_total', 0)}")
    k3.metric("Fixed cost", f"€ {summary.get('fixed_cost', 0):,.0f}")
    k4.metric("Transport cost", f"€ {summary.get('transport_cost', 0):,.0f}")
    st.caption(
        f"Mode: **{summary.get('mode', '—')}** · "
        f"{'single-source' if summary.get('single_source') else 'demand-splitting'} · "
        f"{summary.get('customers_served', 0)} customers served · "
        f"total demand {summary.get('total_demand', 0):,.0f}"
    )

    unreachable = chart_data.get("unreachable_customers", [])
    if unreachable:
        st.warning(
            f"⚠️ {len(unreachable)} customer(s) had no lane path from any facility "
            f"and were excluded: {', '.join(unreachable[:10])}"
            + (" …" if len(unreachable) > 10 else "")
        )

    st.divider()

    # ── Open / closed facilities ─────────────────────────────────────────────
    facility_rows = chart_data.get("facility_rows", [])
    if facility_rows:
        st.markdown("**Facilities — open vs. closed**")
        df = pd.DataFrame(facility_rows)
        df["status"] = df["is_open"].map(lambda b: "🟢 OPEN" if b else "⚪ closed")
        view_cols = ["status", "code", "name", "node_type", "fixed_cost",
                     "demand_served", "capacity", "util_pct", "customers"]
        view_cols = [c for c in view_cols if c in df.columns]
        st.dataframe(
            df[view_cols], use_container_width=True, height=300,
            column_config={
                "fixed_cost":    st.column_config.NumberColumn("Fixed €", format="€ %.0f"),
                "demand_served": st.column_config.NumberColumn("Demand served", format="%.0f"),
                "capacity":      st.column_config.NumberColumn("Capacity", format="%.0f"),
                "util_pct":      st.column_config.NumberColumn("Util %", format="%.1f%%"),
                "customers":     st.column_config.NumberColumn("# Customers", format="%d"),
            },
        )

    # ── Sankey facility → customer ───────────────────────────────────────────
    sk_nodes = chart_data.get("sankey_nodes", [])
    sk_links = chart_data.get("sankey_links", [])
    if sk_nodes and sk_links:
        st.markdown("**Demand assignment (Sankey)**")
        fig = go.Figure(go.Sankey(
            node=dict(
                pad=18, thickness=18, label=sk_nodes,
                line=dict(color="#1f2937", width=0.5),
            ),
            link=dict(
                source=[l["source"] for l in sk_links],
                target=[l["target"] for l in sk_links],
                value=[l["value"]  for l in sk_links],
                label=[l["label"]  for l in sk_links],
            ),
        ))
        fig.update_layout(height=420, margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

    # ── Cost breakdown per open facility ─────────────────────────────────────
    cost_by_fac = chart_data.get("cost_by_fac", [])
    if cost_by_fac:
        st.markdown("**Cost breakdown per open facility**")
        df = pd.DataFrame(cost_by_fac)
        fig = go.Figure()
        fig.add_bar(name="Fixed", x=df["code"], y=df["fixed_cost"], marker_color="#6366F1")
        fig.add_bar(name="Transport", x=df["code"], y=df["transport_cost"], marker_color="#22C55E")
        fig.update_layout(barmode="stack", height=320,
                          margin=dict(t=10, b=10, l=10, r=10),
                          yaxis_title="€")
        st.plotly_chart(fig, use_container_width=True)

    # ── Assignment detail ────────────────────────────────────────────────────
    if assignments:
        with st.expander("Assignment detail (facility → customer)"):
            df = pd.DataFrame(assignments)
            view_cols = ["facility_code", "customer_code", "share_pct",
                         "demand_served", "unit_cost", "line_cost"]
            view_cols = [c for c in view_cols if c in df.columns]
            st.dataframe(
                df[view_cols], use_container_width=True, height=320,
                column_config={
                    "share_pct":     st.column_config.NumberColumn("Share %", format="%.1f%%"),
                    "demand_served": st.column_config.NumberColumn("Demand", format="%.0f"),
                    "unit_cost":     st.column_config.NumberColumn("€/u", format="€ %.4f"),
                    "line_cost":     st.column_config.NumberColumn("Cost €", format="€ %.0f"),
                },
            )

    # ── Sensitivity ──────────────────────────────────────────────────────────
    if sens.get("bottlenecks"):
        st.warning(
            f"⚠️ {len(sens['bottlenecks'])} open facility(ies) running at ≥90% "
            "capacity — consider raising capacity or opening another node."
        )
        st.dataframe(pd.DataFrame(sens["bottlenecks"]),
                     use_container_width=True, height=160)

    if sens.get("costly_customers"):
        with st.expander("Most expensive customers to serve (transport €)"):
            st.dataframe(pd.DataFrame(sens["costly_customers"]),
                         use_container_width=True, height=240)
