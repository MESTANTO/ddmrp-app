"""
Session 1 — Sourcing Allocation view.

Two tabs:
  • Configure & Run  — pick horizon, weights, top-N, capacities → solve
  • Results          — KPIs, allocation table, supplier-share pie, sensitivity

Inputs come from `data_adapters.load_sourcing_inputs()`; the solve hits
`modules.optimization.ses01_sourcing.solve()`; output is persisted via
`modules.optimization.solver_base.record_run()`.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from database.auth import get_company_id, get_current_user
from modules.optimization.data_adapters import load_sourcing_inputs
from modules.optimization.ses01_sourcing import solve as solve_sourcing
from modules.optimization.solver_base import record_run
from views.optimization._shared import (
    render_run_history,
    load_run_payloads,
    status_badge,
)


def show() -> None:
    company_id = get_company_id()
    user       = get_current_user()
    user_id    = user.get("id") if user else None

    # ── Header ───────────────────────────────────────────────────────────────
    top = st.columns([6, 1])
    with top[0]:
        st.title("📦 Session 1 — Sourcing Allocation")
        st.caption("Ch2 (LP) + Ch3 (IP) · solver: PuLP/CBC · snapshots expire in 10 days")
    with top[1]:
        if st.button("← Catalogue", use_container_width=True):
            st.session_state.optimization_session = None
            st.rerun()

    st.divider()

    tab_run, tab_results = st.tabs(["🛠️ Configure & Run", "📊 Results"])

    # ── Tab: Configure & Run ─────────────────────────────────────────────────
    with tab_run:
        st.markdown("##### 1. Demand horizon & scope")
        c1, c2, c3 = st.columns(3)
        with c1:
            horizon_days = st.number_input(
                "Demand horizon (days)", min_value=7, max_value=365, value=30, step=1,
                help="Demand per item = ADU × horizon.",
            )
        with c2:
            top_n = st.number_input(
                "Top-N items by € spend", min_value=5, max_value=200, value=25, step=5,
                help="Keeps the MILP tractable. Items ranked by ADU × unit_cost × horizon.",
            )
        with c3:
            max_per_item = st.number_input(
                "Max suppliers per item", min_value=1, max_value=5, value=2, step=1,
                help="Diversification cap — prevents single-source concentration.",
            )

        st.markdown("##### 2. Cost-trade-off weights")
        c4, c5, c6 = st.columns(3)
        with c4:
            alpha = st.slider(
                "Reliability penalty (α)", 0.0, 0.50, 0.10, step=0.01,
                help="Effective cost = unit_cost × (1 + α·(100-rel%)/100 + …). "
                     "Higher α favours reliable suppliers.",
            )
        with c5:
            beta = st.slider(
                "Lead-time penalty (β)", 0.0, 0.30, 0.05, step=0.01,
                help="Effective cost = unit_cost × (… + β·lead_days/30). "
                     "Higher β favours short-lead-time suppliers.",
            )
        with c6:
            fixed_cost = st.number_input(
                "Fixed cost per supplier link (€)", min_value=0.0,
                value=0.0, step=50.0,
                help="Penalty per item-supplier relationship used. Encourages consolidation.",
            )

        st.markdown("##### 3. Preferred-supplier bias")
        c7, c8 = st.columns([2, 3])
        with c7:
            prefer_preferred = st.checkbox(
                "Favour preferred suppliers",
                value=False,
                help="Gives links flagged 'Preferred' in the Supplier-Part Matrix "
                     "a small cost advantage so they win on near-ties. Reported "
                     "costs stay at the true effective cost.",
            )
        with c8:
            preferred_discount = st.slider(
                "Preferred advantage", 0.0, 0.20, 0.03, step=0.01,
                format="%.0f%%",
                disabled=not prefer_preferred,
                help="Objective-only discount applied to preferred links "
                     "(e.g. 3% means a preferred supplier wins unless another "
                     "is more than 3% cheaper).",
            )

        scenario_name = st.text_input(
            "Scenario name (optional)",
            placeholder="e.g. baseline / α=0.20 / consolidate",
            value=f"{horizon_days}d · α={alpha:g} · β={beta:g}",
        )

        # Preview the data the solver will see
        with st.expander("🔎 Preview inputs (data pulled from master data)"):
            try:
                inputs = load_sourcing_inputs(
                    company_id, horizon_days=int(horizon_days), top_n_items=int(top_n)
                )
            except Exception as exc:
                st.error(f"Failed to load inputs: {exc}")
                inputs = None

            if inputs is not None:
                n_items = len(inputs["items"])
                n_sups  = len(inputs["suppliers"])
                total_demand = sum(i["demand"] for i in inputs["items"])
                total_spend  = sum(i["demand"] * i["unit_cost"] for i in inputs["items"])
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Items", f"{n_items}")
                k2.metric("Active suppliers", f"{n_sups}")
                k3.metric("Total demand", f"{total_demand:,.0f}")
                k4.metric("Total spend", f"€ {total_spend:,.0f}")

                # Sourcing mode — restricted to the Supplier-Part Matrix links
                # when any exist, otherwise the legacy Cartesian product.
                candidates = inputs.get("candidates") or []
                kept_ids   = {it["id"] for it in inputs["items"]}
                if inputs.get("has_links"):
                    items_with_link = {c["item_id"] for c in candidates}
                    missing = len(kept_ids - items_with_link)
                    st.info(
                        f"🔗 **Matrix sourcing** — restricted to "
                        f"{len(candidates)} supplier-part link(s) from the "
                        f"Supplier-Part Matrix."
                        + (f" ⚠️ {missing} selected item(s) have no link and "
                           "will be reported as unsourced." if missing else "")
                    )
                else:
                    st.caption(
                        "No Supplier-Part Matrix links defined — every item may "
                        "be sourced from every active supplier (Cartesian). "
                        "Define links in Master Data → Supplier-Part Matrix to constrain this."
                    )

                if inputs["items"]:
                    st.markdown("**Items the solver will allocate**")
                    st.dataframe(
                        pd.DataFrame(inputs["items"])[
                            ["part_number", "description", "demand", "unit_cost"]
                        ],
                        use_container_width=True, height=240,
                    )
                if inputs["suppliers"]:
                    st.markdown("**Active suppliers (candidates)**")
                    st.dataframe(
                        pd.DataFrame(inputs["suppliers"])[
                            ["code", "name", "lead_time_days", "reliability_pct", "status"]
                        ],
                        use_container_width=True, height=200,
                    )

        # ── Solve button ─────────────────────────────────────────────────────
        st.divider()
        if st.button("🚀 Solve sourcing MILP", type="primary", use_container_width=True):
            try:
                inputs = load_sourcing_inputs(
                    company_id, horizon_days=int(horizon_days), top_n_items=int(top_n)
                )
                if not inputs["items"] or not inputs["suppliers"]:
                    st.error(
                        "Cannot solve: need at least 1 item with non-zero ADU/unit_cost "
                        "and 1 active supplier. Check Material Master and Supplier Master."
                    )
                else:
                    params = {
                        **inputs,
                        "alpha": float(alpha),
                        "beta":  float(beta),
                        "fixed_cost_per_link": float(fixed_cost),
                        "max_suppliers_per_item": int(max_per_item),
                        "prefer_preferred": bool(prefer_preferred),
                        "preferred_discount": float(preferred_discount),
                    }
                    with st.spinner("Solving MILP with CBC…"):
                        result = solve_sourcing(params)
                    run_id = record_run(
                        company_id=company_id,
                        user_id=user_id,
                        session_key="ses01_sourcing",
                        scenario_name=scenario_name,
                        params={
                            "horizon_days": int(horizon_days),
                            "top_n_items":  int(top_n),
                            "alpha": float(alpha),
                            "beta":  float(beta),
                            "fixed_cost_per_link": float(fixed_cost),
                            "max_suppliers_per_item": int(max_per_item),
                            "n_items":     len(inputs["items"]),
                            "n_suppliers": len(inputs["suppliers"]),
                            "sourcing_mode": "matrix" if inputs.get("has_links") else "cartesian",
                            "n_candidate_links": len(inputs.get("candidates") or []),
                            "prefer_preferred": bool(prefer_preferred),
                            "preferred_discount": float(preferred_discount),
                        },
                        result=result,
                    )
                    if result.status == "solved":
                        st.success(
                            f"✓ Solved in {result.runtime_ms} ms · "
                            f"objective = € {result.objective_value:,.2f} · run #{run_id}"
                        )
                        st.session_state["ses01_last_run_id"] = run_id
                    else:
                        st.error(
                            f"Solve status: {result.status}. "
                            f"{result.error_message or ''}"
                        )
            except Exception as exc:
                st.error(f"Solver error: {exc}")

        st.divider()
        render_run_history(
            company_id, "ses01_sourcing",
            on_select=lambda rid: st.session_state.update({"ses01_last_run_id": rid}),
        )

    # ── Tab: Results ─────────────────────────────────────────────────────────
    with tab_results:
        run_id = st.session_state.get("ses01_last_run_id")
        if not run_id:
            st.info("Run a scenario from the **Configure & Run** tab to see results here.")
            return

        payloads = load_run_payloads(int(run_id), company_id)
        if not payloads:
            st.error("Run not found (it may have expired).")
            return

        run = payloads["_run"]
        _render_results(run, payloads)


def _render_results(run, payloads: dict) -> None:
    """Render KPIs + allocation table + supplier-share pie + sensitivity."""
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
    kpi        = payloads.get("kpi", {})
    allocation = payloads.get("allocation", {}).get("rows", [])
    sens       = payloads.get("sensitivity", {})

    # Sourcing mode + unsourced items (matrix mode only)
    if summary.get("sourcing_mode") == "matrix":
        st.caption("🔗 Sourcing restricted to Supplier-Part Matrix links.")
    unsourced = sens.get("unsourced", [])
    if unsourced:
        st.warning(
            f"⚠️ {len(unsourced)} item(s) had no supplier link and could not be "
            "sourced. Add links in Master Data → Supplier-Part Matrix."
        )
        st.dataframe(pd.DataFrame(unsourced), use_container_width=True, height=160)

    # KPIs
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total cost", f"€ {summary.get('total_cost', 0):,.0f}")
    k2.metric("Suppliers used", f"{summary.get('suppliers_used', 0)}")
    k3.metric("Single-source %", f"{kpi.get('single_source_pct', 0):.0f}%")
    k4.metric("HH index", f"{kpi.get('hh_index', 0):,.0f}",
              help="Herfindahl–Hirschman index on supplier value share. "
                   "0 = perfectly diversified; 10 000 = monopoly. > 2 500 = concentrated.")

    st.divider()

    # Supplier share pie + items-per-supplier bar
    if sens.get("per_supplier_load"):
        c1, c2 = st.columns(2)
        per_sup_df = pd.DataFrame(sens["per_supplier_load"])
        with c1:
            st.markdown("**Supplier value share**")
            fig = px.pie(
                per_sup_df, values="value", names="supplier_code",
                hover_data=["supplier_name", "items_count"],
                hole=0.45,
            )
            fig.update_layout(height=360, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

        with c2:
            st.markdown("**Items per supplier**")
            fig2 = px.bar(
                per_sup_df.sort_values("items_count", ascending=True),
                x="items_count", y="supplier_code",
                hover_data=["supplier_name", "value"],
                orientation="h",
            )
            fig2.update_layout(height=360, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig2, use_container_width=True)

    # Allocation table
    if allocation:
        st.markdown("**Optimal allocation (item × supplier)**")
        alloc_df = pd.DataFrame(allocation)
        if "is_preferred" in alloc_df.columns:
            alloc_df["preferred"] = alloc_df["is_preferred"].map(
                lambda v: "⭐" if v else "")
        # nicer column ordering
        cols = ["part_number", "supplier_code", "supplier_name", "preferred",
                "units", "share_pct", "value", "unit_cost"]
        cols = [c for c in cols if c in alloc_df.columns]
        st.dataframe(
            alloc_df[cols],
            use_container_width=True, height=420,
            column_config={
                "preferred": st.column_config.TextColumn("Pref.", help="⭐ = preferred supplier link"),
                "units":     st.column_config.NumberColumn("Units", format="%.0f"),
                "share_pct": st.column_config.NumberColumn("Share %", format="%.1f%%"),
                "value":     st.column_config.NumberColumn("Value €", format="€ %.2f"),
                "unit_cost": st.column_config.NumberColumn("Eff. unit €", format="€ %.3f"),
            },
        )

    # Per-item diversification
    if sens.get("per_item_supplier_count"):
        with st.expander("Per-item supplier diversification"):
            it_df = pd.DataFrame(sens["per_item_supplier_count"])
            st.dataframe(it_df, use_container_width=True, height=300)
