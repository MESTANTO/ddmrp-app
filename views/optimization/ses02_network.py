"""
Session 2 — Multi-Echelon Network Flow view.

Two tabs:
  • Configure & Run  — horizon, item filter, preview of inputs → solve
  • Results          — KPIs, Sankey of optimal flows, lane utilization,
                       per-node load, per-item cost
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from database.auth import get_company_id, get_current_user
from database.db import Base, engine, Location, Lane, LocationDemand
from modules.optimization.data_adapters import load_network_inputs
from modules.optimization.ses02_network import solve as solve_network
from modules.optimization.solver_base import record_run
from views.optimization._shared import (
    render_run_history,
    load_run_payloads,
    status_badge,
)


def show() -> None:
    # Defensive: ensure new network tables exist (no-op if already created).
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
        st.title("🌐 Session 2 — Multi-Echelon Network Flow")
        st.caption("Ch4 Network LP · solver: NetworkX · snapshots expire in 10 days")
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
                "Demand horizon (days)", min_value=7, max_value=365, value=30, step=1,
                help="Per-item demand = ADU × horizon, or LocationDemand × horizon/30 if defined per customer.",
            )
        with c2:
            scenario_name = st.text_input(
                "Scenario name (optional)",
                value=f"network · {horizon_days}d",
            )

        # Preview the data the solver will see
        st.markdown("##### 🔎 Preview inputs (loaded from Network Master)")
        try:
            inputs = load_network_inputs(company_id, horizon_days=int(horizon_days))
        except Exception as exc:
            st.error(f"Failed to load inputs: {exc}")
            return

        n_loc   = len(inputs["locations"])
        n_lanes = len(inputs["lanes"])
        n_items = sum(1 for it in inputs["items"]
                      if sum(d["demand"] for d in inputs["per_item_demand"].get(it["id"], [])) > 0)
        total_dem = sum(
            sum(d["demand"] for d in rows)
            for rows in inputs["per_item_demand"].values()
        )
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Locations", n_loc)
        k2.metric("Lanes", n_lanes)
        k3.metric("Items with demand", n_items)
        k4.metric("Total demand", f"{total_dem:,.0f}")

        if n_loc == 0 or n_lanes == 0:
            st.warning(
                "Network Master is empty. Add Locations and Lanes "
                "(Master Data → Network Master) before solving."
            )

        with st.expander("Locations"):
            if inputs["locations"]:
                st.dataframe(
                    pd.DataFrame(inputs["locations"]),
                    use_container_width=True, height=240,
                )
            else:
                st.info("No locations.")

        with st.expander("Lanes"):
            if inputs["lanes"]:
                loc_lookup = {l["id"]: l for l in inputs["locations"]}
                lane_view = []
                for ln in inputs["lanes"]:
                    o = loc_lookup.get(ln["origin_id"], {})
                    d = loc_lookup.get(ln["dest_id"], {})
                    lane_view.append({
                        "id": ln["id"],
                        "origin": f"{o.get('code', '?')} [{o.get('node_type', '?')}]",
                        "dest":   f"{d.get('code', '?')} [{d.get('node_type', '?')}]",
                        "mode":   ln["mode"],
                        "cost_per_unit": ln["cost_per_unit"],
                        "lead_time": ln["lead_time_days"],
                        "capacity": ln["capacity_units"] if ln["capacity_units"] else "∞",
                    })
                st.dataframe(pd.DataFrame(lane_view),
                             use_container_width=True, height=240)
            else:
                st.info("No lanes.")

        st.divider()
        if st.button("🚀 Solve network flow", type="primary",
                     use_container_width=True,
                     disabled=(n_loc == 0 or n_lanes == 0)):
            try:
                params = {**inputs}
                with st.spinner("Solving min-cost flow per item…"):
                    result = solve_network(params)
                run_id = record_run(
                    company_id=company_id,
                    user_id=user_id,
                    session_key="ses02_network",
                    scenario_name=scenario_name,
                    params={
                        "horizon_days": int(horizon_days),
                        "n_locations": n_loc,
                        "n_lanes":     n_lanes,
                        "n_items_with_demand": n_items,
                    },
                    result=result,
                )
                if result.status == "solved":
                    st.success(
                        f"✓ Solved in {result.runtime_ms} ms · "
                        f"objective = € {result.objective_value:,.2f} · run #{run_id}"
                    )
                    st.session_state["ses02_last_run_id"] = run_id
                else:
                    st.error(
                        f"Status: {result.status}. {result.error_message or ''}"
                    )
            except Exception as exc:
                st.error(f"Solver error: {exc}")

        st.divider()
        render_run_history(
            company_id, "ses02_network",
            on_select=lambda rid: st.session_state.update({"ses02_last_run_id": rid}),
        )

    # ── Tab: Results ─────────────────────────────────────────────────────────
    with tab_results:
        run_id = st.session_state.get("ses02_last_run_id")
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
    allocation = payloads.get("allocation", {}).get("per_item", [])
    sens       = payloads.get("sensitivity", {})

    # KPI row
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total transport cost", f"€ {summary.get('total_cost', 0):,.0f}")
    k2.metric("Items solved", f"{summary.get('items_solved', 0)}")
    k3.metric("Lanes used", f"{summary.get('lanes_active', 0)}")
    k4.metric("Bottlenecks (≥90% util)", f"{len(sens.get('bottlenecks', []))}")

    st.divider()

    # ── Sankey ───────────────────────────────────────────────────────────────
    sk_nodes = chart_data.get("sankey_nodes", [])
    sk_links = chart_data.get("sankey_links", [])
    if sk_nodes and sk_links:
        st.markdown("**Optimal flow (Sankey)**")
        fig = go.Figure(go.Sankey(
            node=dict(
                pad=18, thickness=18,
                label=sk_nodes,
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

    # ── Lane utilization table ──────────────────────────────────────────────
    lane_rows = chart_data.get("lane_rows", [])
    if lane_rows:
        st.markdown("**Lane utilization** (sorted by units shipped)")
        df = pd.DataFrame(lane_rows)
        view_cols = ["origin_code", "dest_code", "mode", "units",
                     "capacity", "util_pct", "cost_per_unit", "value"]
        view_cols = [c for c in view_cols if c in df.columns]
        st.dataframe(
            df[view_cols],
            use_container_width=True, height=320,
            column_config={
                "units":         st.column_config.NumberColumn("Units", format="%.0f"),
                "capacity":      st.column_config.NumberColumn("Capacity", format="%.0f"),
                "util_pct":      st.column_config.NumberColumn("Util %", format="%.1f%%"),
                "cost_per_unit": st.column_config.NumberColumn("€/u", format="€ %.4f"),
                "value":         st.column_config.NumberColumn("Lane cost €", format="€ %.0f"),
            },
        )

    # ── Node throughput ──────────────────────────────────────────────────────
    node_rows = chart_data.get("node_rows", [])
    if node_rows:
        with st.expander("Per-node throughput"):
            st.dataframe(pd.DataFrame(node_rows), use_container_width=True, height=260)

    # ── Per-item cost ────────────────────────────────────────────────────────
    if allocation:
        with st.expander("Per-item cost breakdown"):
            st.dataframe(pd.DataFrame(allocation), use_container_width=True, height=260)

    # ── Bottlenecks / underused ──────────────────────────────────────────────
    if sens.get("bottlenecks"):
        st.warning(
            f"⚠️ {len(sens['bottlenecks'])} lane(s) running at ≥90% capacity — "
            "consider adding capacity or alternative routing."
        )
        st.dataframe(pd.DataFrame(sens["bottlenecks"]),
                     use_container_width=True, height=200)
