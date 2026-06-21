"""
DDMRP vs MRP — Methodology Simulation (standalone page).

Replays the company's parts (optionally only the DDMRP-eligible ones) through a
daily Monte-Carlo loop under several replenishment policies, then compares the
inventory rotation index ("indice di rotazione di stock"), average stock value
and service level so the user can choose the methodology that maximises service
while minimising stock.

Two tabs:
  • Configure & Run — horizon, replications, demand mode/profile, policies,
                      service target, eligible-only filter, item preview
  • Results         — KPI strip, per-policy comparison, per-item table,
                      per-item recommendation (DDMRP vs best MRP)
"""

from __future__ import annotations

import io

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from database.auth import get_company_id, get_current_user
from modules.optimization.data_adapters import load_simulation_inputs
from modules.optimization.ses_simulation import solve as solve_sim, POLICY_LABELS
from modules.optimization.solver_base import record_run
from modules.positioning_engine import assign_methodologies

# Methodologies the user can accept/assign to a part (AS-IS is the baseline).
_ASSIGN_LABELS = {k: v for k, v in POLICY_LABELS.items() if k != "as_is"}
_KEEP_CURRENT = "— keep current (AS-IS) —"
from views.optimization._shared import (
    render_run_history,
    load_run_payloads,
    status_badge,
)

_SESSION_KEY = "sim_ddmrp_mrp"

# Stable colour per policy for the deep-analysis overlay.
_TRACE_COLORS = {
    "as_is":       "#94A3B8",
    "ddmrp":       "#6366F1",
    "rop_q":       "#0EA5E9",
    "rop_ss":      "#14B8A6",
    "kanban":      "#F59E0B",
    "periodic_rs": "#A855F7",
}


@st.cache_data(show_spinner=False)
def _items_map(company_id: int) -> dict:
    """Part-number → item dict for the company (cached). Used to re-simulate a
    single part's daily trace for the deep-analysis chart."""
    inputs = load_simulation_inputs(
        company_id, only_ddmrp_eligible=False, top_n_items=1000)
    return {it["part_number"]: it for it in inputs["items"]}


def _trace_chart(traces: dict, title: str):
    """Build a 2-row figure for one part:
      • row 1 — on-hand stock line(s) per policy, demand bars, planned-supply
                (receipts) markers
      • row 2 — inventory value (€) line(s) per policy
    `traces` maps a display label → the trace dict returned by simulate_trace,
    each carrying a `policy` key for colouring."""
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
        row_heights=[0.62, 0.38],
        subplot_titles=("On-hand stock · demand · planned supply",
                        "Inventory value (€)"),
    )

    first = next(iter(traces.values()))
    days = first.get("day", [])
    warmup = first.get("warmup_days", 0)

    # shared demand bars (identical across policies for the same seed)
    fig.add_bar(
        x=days, y=first.get("demand", []), name="Demand",
        marker_color="rgba(148,163,184,0.35)",
        hovertemplate="day %{x}<br>demand %{y:.1f}<extra></extra>",
        row=1, col=1,
    )

    for label, tr in traces.items():
        pol = tr.get("policy", "")
        color = _TRACE_COLORS.get(pol, "#1f2937")
        # on-hand line
        fig.add_scatter(
            x=tr.get("day", []), y=tr.get("on_hand", []), mode="lines",
            name=f"{label} — on-hand", line=dict(color=color, width=2),
            hovertemplate="day %{x}<br>on-hand %{y:.1f}<extra></extra>",
            row=1, col=1,
        )
        # planned supply (receipts landing) markers
        rx = [d for d, v in zip(tr.get("day", []), tr.get("receipts", [])) if v > 0]
        ry = [v for v in tr.get("receipts", []) if v > 0]
        if rx:
            fig.add_scatter(
                x=rx, y=ry, mode="markers", name=f"{label} — supply",
                marker=dict(color=color, size=8, symbol="triangle-up",
                            line=dict(color="white", width=1)),
                hovertemplate="day %{x}<br>supply +%{y:.1f}<extra></extra>",
                row=1, col=1,
            )
        # inventory value line
        fig.add_scatter(
            x=tr.get("day", []), y=tr.get("inventory_value", []), mode="lines",
            name=f"{label} — € value", line=dict(color=color, width=2, dash="dot"),
            hovertemplate="day %{x}<br>€ %{y:,.0f}<extra></extra>",
            showlegend=False, row=2, col=1,
        )

    # warmup boundary (metrics start after this)
    if warmup and days:
        for r in (1, 2):
            fig.add_vline(x=warmup, line_dash="dash", line_color="#CBD5E1",
                          row=r, col=1)
        fig.add_annotation(x=warmup, y=1.0, yref="paper", showarrow=False,
                           text="warm-up ▸", font=dict(size=10, color="#94A3B8"),
                           xanchor="left")

    fig.update_layout(
        title=title, height=560, barmode="overlay", plot_bgcolor="white",
        margin=dict(t=70, b=40, l=50, r=20), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.04, x=0),
    )
    fig.update_yaxes(title_text="units", row=1, col=1)
    fig.update_yaxes(title_text="€", row=2, col=1)
    fig.update_xaxes(title_text="day", row=2, col=1)
    return fig


def show() -> None:
    company_id = get_company_id()
    user       = get_current_user()
    user_id    = user.get("id") if user else None

    st.title("⚖️ DDMRP vs MRP — Methodology Simulation")
    st.caption(
        "Simulate each part over repeated DDMRP cycles against classic MRP "
        "policies (Reorder Point, ROP+SS, Kanban, Periodic Review) and compare "
        "stock rotation, average stock value and service level. "
        "Solver: Monte-Carlo · snapshots expire in 10 days."
    )
    st.divider()

    tab_run, tab_results, tab_tobe = st.tabs(
        ["🛠️ Configure & Run", "📊 Results", "🔮 TO-BE Scenario"])

    with tab_run:
        _render_configure(company_id, user_id)

    with tab_results:
        run_id = st.session_state.get(f"{_SESSION_KEY}_last_run_id")
        if not run_id:
            st.info("Run a simulation from the **Configure & Run** tab to see results here.")
        else:
            payloads = load_run_payloads(int(run_id), company_id)
            if not payloads:
                st.error("Run not found (it may have expired).")
            else:
                _render_results(payloads["_run"], payloads)

    with tab_tobe:
        _render_to_be(company_id, user_id)


# ── Configure & Run ──────────────────────────────────────────────────────────

def _render_configure(company_id: int, user_id) -> None:
    st.markdown("##### Scope")
    c1, c2 = st.columns(2)
    with c1:
        only_eligible = st.checkbox(
            "Only DDMRP-eligible parts", value=False,
            help="Use the Strategic Positioning recommendation (score-based) to "
                 "limit the simulation to parts flagged for DDMRP.",
        )
    with c2:
        top_n = st.number_input(
            "Max parts (by € volume)", min_value=1, max_value=500,
            value=50, step=5,
            help="Caps the run for responsiveness — parts ranked by ADU × unit cost.",
        )

    try:
        inputs = load_simulation_inputs(
            company_id,
            only_ddmrp_eligible=only_eligible,
            top_n_items=int(top_n),
        )
    except Exception as exc:
        st.error(f"Failed to load inputs: {exc}")
        return

    items = inputs["items"]
    n_items = len(items)
    n_eligible = sum(1 for it in items if it["is_ddmrp_eligible"])

    k1, k2, k3 = st.columns(3)
    k1.metric("Parts in scope", n_items)
    k2.metric("DDMRP-eligible", n_eligible)
    k3.metric("Σ unit-cost × ADU/day",
              f"€ {sum(it['adu'] * it['unit_cost'] for it in items):,.0f}")

    if n_items == 0:
        st.warning(
            "No parts with demand in scope. Set **ADU** (or demand history) and "
            "**Unit Cost** in Material Master, and/or relax the eligible-only filter."
        )

    st.markdown("##### Simulation horizon")
    h1, h2, h3 = st.columns(3)
    with h1:
        horizon = st.number_input(
            "Horizon (days per cycle loop)", min_value=30, max_value=730,
            value=180, step=30,
            help="Length of each simulated DDMRP/MRP loop.",
        )
    with h2:
        n_reps = st.number_input(
            "Replications", min_value=1, max_value=200, value=30, step=5,
            help="Monte-Carlo replications averaged per part × policy.",
        )
    with h3:
        seed = st.number_input("Random seed", min_value=0, value=42, step=1)

    st.markdown("##### Demand")
    d1, d2 = st.columns(2)
    with d1:
        demand_mode = st.radio(
            "Demand source", ["bootstrap", "synthetic"], horizontal=True,
            help="Bootstrap = resampled from each part's actual imported history "
                 "(recommended for a client AS-IS assessment; falls back to "
                 "synthetic for parts without history). Synthetic = generated "
                 "from ADU + variability per the profile below.",
        )
    with d2:
        demand_profile = st.selectbox(
            "Synthetic profile",
            ["stable", "seasonal", "lumpy", "trending"],
            disabled=(demand_mode == "bootstrap"),
            help="Shape of the synthetic demand signal per part.",
        )

    st.markdown("##### Policies & target")
    p1, p2 = st.columns([3, 1])
    with p1:
        mrp_choices = {k: v for k, v in POLICY_LABELS.items() if k != "ddmrp"}
        selected_mrp = st.multiselect(
            "MRP policies to compare against DDMRP",
            options=list(mrp_choices.keys()),
            default=list(mrp_choices.keys()),
            format_func=lambda k: mrp_choices[k],
        )
    with p2:
        service_target = st.number_input(
            "Target fill rate (%)", min_value=50.0, max_value=99.9,
            value=95.0, step=0.5,
        ) / 100.0

    scenario_name = st.text_input(
        "Scenario name (optional)",
        value=f"sim · {int(horizon)}d × {int(n_reps)} · {demand_mode}",
    )

    with st.expander("Parts in scope (master-data preview)"):
        if items:
            df = pd.DataFrame([{
                "Part": it["part_number"],
                "Description": it["description"],
                "Type": it["item_type"],
                "ADU": it["adu"],
                "DLT": it["dlt"],
                "Unit € ": it["unit_cost"],
                "MOQ": it["min_order_qty"],
                "OC": it["order_cycle"],
                "CV(hist)": it["hist_cv"],
                "Eligible": "✅" if it["is_ddmrp_eligible"] else "—",
                "Score": it["positioning_score"],
            } for it in items])
            st.dataframe(df, use_container_width=True, height=260, hide_index=True)
        else:
            st.info("No parts to preview.")

    st.divider()
    policies = ["ddmrp", *selected_mrp]
    if st.button("🚀 Run simulation", type="primary",
                 use_container_width=True, disabled=(n_items == 0)):
        params = {
            "items":          items,
            "horizon_days":   int(horizon),
            "n_replications": int(n_reps),
            "demand_mode":    demand_mode,
            "demand_profile": demand_profile,
            "policies":       policies,
            "service_target": float(service_target),
            "seed":           int(seed),
        }
        try:
            with st.spinner(f"Simulating {n_items} part(s) × {len(policies)} "
                            f"policies × {int(n_reps)} reps…"):
                result = solve_sim(params)
            run_id = record_run(
                company_id=company_id,
                user_id=user_id,
                session_key=_SESSION_KEY,
                scenario_name=scenario_name,
                params={
                    "n_items":        n_items,
                    "horizon_days":   int(horizon),
                    "n_replications": int(n_reps),
                    "demand_mode":    demand_mode,
                    "demand_profile": demand_profile,
                    "policies":       policies,
                    "service_target": float(service_target),
                    "only_ddmrp_eligible": only_eligible,
                    "seed":           int(seed),
                },
                result=result,
            )
            if result.status == "solved":
                st.success(
                    f"✓ Simulated in {result.runtime_ms} ms · "
                    f"DDMRP Σ stock value = € {result.objective_value:,.0f} · run #{run_id}"
                )
                st.session_state[f"{_SESSION_KEY}_last_run_id"] = run_id
            else:
                st.error(f"Status: {result.status}. {result.error_message or ''}")
        except Exception as exc:
            st.error(f"Simulation error: {exc}")

    st.divider()
    render_run_history(
        company_id, _SESSION_KEY,
        on_select=lambda rid: st.session_state.update(
            {f"{_SESSION_KEY}_last_run_id": rid}),
    )


# ── Results ──────────────────────────────────────────────────────────────────

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

    summary = payloads.get("summary", {})
    rows    = payloads.get("by_item_policy", {}).get("rows", [])
    recs    = payloads.get("recommendation", {}).get("rows", [])
    psum    = summary.get("policy_summary", [])

    target = summary.get("service_target", 0.95)

    # KPI strip
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Parts simulated", summary.get("n_items", 0))
    k2.metric("Policies", summary.get("n_policies", 0))
    k3.metric("DDMRP Σ stock value",
              f"€ {summary.get('ddmrp_total_stock_value', 0):,.0f}")
    k4.metric("DDMRP recommended",
              f"{summary.get('n_ddmrp_recommended', 0)} / {summary.get('n_items', 0)}")
    st.caption(
        f"Horizon **{summary.get('horizon_days', 0)}d** × "
        f"{summary.get('n_replications', 0)} reps · demand "
        f"**{summary.get('demand_mode', '—')}**"
        + (f" / {summary.get('demand_profile')}" if summary.get('demand_mode') == 'synthetic' else "")
        + f" · target fill **{target * 100:.0f}%**"
    )

    st.divider()

    # ── Executive summary (AS-IS vs recommended) ─────────────────────────────
    if summary.get("total_asis_stock_value") is not None:
        st.markdown("#### Executive summary — AS-IS (current SAP) vs recommended")
        asis_stock = summary.get("total_asis_stock_value", 0.0)
        reco_stock = summary.get("total_reco_stock_value", 0.0)
        wc_freed = summary.get("working_capital_freed", 0.0)
        wc_pct = summary.get("working_capital_freed_pct", 0.0)
        asis_fill = summary.get("avg_asis_fill")
        reco_fill = summary.get("avg_reco_fill")
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Σ inventory — AS-IS", f"€ {asis_stock:,.0f}")
        e2.metric("Σ inventory — recommended", f"€ {reco_stock:,.0f}",
                  delta=f"-€ {wc_freed:,.0f}" if wc_freed >= 0 else f"+€ {-wc_freed:,.0f}",
                  delta_color="normal")
        e3.metric("Working capital freed", f"€ {wc_freed:,.0f}",
                  delta=f"{wc_pct:.1f}%")
        e4.metric(
            "Avg fill — AS-IS → reco",
            f"{(reco_fill or 0) * 100:.1f}%",
            delta=(f"+{(reco_fill - asis_fill) * 100:.1f} pts"
                   if (asis_fill is not None and reco_fill is not None) else None),
        )
        st.caption(
            f"AS-IS avg fill **{(asis_fill or 0) * 100:.1f}%** · "
            f"parts unmanaged today (MRP Type ND): **{summary.get('n_unmanaged', 0)}** · "
            f"parts where no policy meets target: **{summary.get('n_no_policy_meets', 0)}** · "
            f"parts already optimal (keep current): **{summary.get('n_keep_current', 0)}**"
        )
        st.divider()

    # ── Per-policy comparison ────────────────────────────────────────────────
    if psum:
        st.markdown("#### Policy comparison (all parts aggregated)")
        df = pd.DataFrame(psum)
        cc1, cc2 = st.columns(2)
        with cc1:
            fig = go.Figure()
            fig.add_bar(
                x=df["policy_label"], y=df["total_stock_value"],
                marker_color=["#6366F1" if p == "ddmrp" else "#94A3B8"
                              for p in df["policy"]],
                hovertemplate="<b>%{x}</b><br>Σ stock € %{y:,.0f}<extra></extra>",
            )
            fig.update_layout(
                title="Total average stock value (€)", height=340,
                margin=dict(t=40, b=80, l=40, r=20), yaxis_title="€",
                plot_bgcolor="white",
            )
            st.plotly_chart(fig, use_container_width=True)
        with cc2:
            fig = go.Figure()
            fig.add_bar(
                x=df["policy_label"], y=df["avg_fill_rate"] * 100.0,
                marker_color=["#22C55E" if f >= target else "#EF4444"
                              for f in df["avg_fill_rate"]],
                hovertemplate="<b>%{x}</b><br>fill %{y:.1f}%<extra></extra>",
            )
            fig.add_hline(y=target * 100.0, line_dash="dash",
                          line_color="#1f2937",
                          annotation_text=f"target {target*100:.0f}%")
            fig.update_layout(
                title="Average unit fill rate (%)", height=340,
                margin=dict(t=40, b=80, l=40, r=20), yaxis_title="%",
                plot_bgcolor="white",
            )
            st.plotly_chart(fig, use_container_width=True)

        df["Avg fill %"] = df["avg_fill_rate"] * 100.0
        df["Avg CSL %"] = df["avg_csl"] * 100.0
        show = df.rename(columns={
            "policy_label": "Policy",
            "total_stock_value": "Σ stock €",
            "avg_turnover": "Avg turnover",
            "total_holding_cost": "Σ holding €/yr",
            "n_items": "Parts",
        })[["Policy", "Avg fill %", "Avg CSL %", "Σ stock €",
            "Avg turnover", "Σ holding €/yr", "Parts"]]
        st.dataframe(
            show, use_container_width=True, hide_index=True,
            column_config={
                "Avg fill %": st.column_config.NumberColumn(format="%.1f%%"),
                "Avg CSL %":  st.column_config.NumberColumn(format="%.1f%%"),
                "Σ stock €": st.column_config.NumberColumn(format="€ %.0f"),
                "Avg turnover": st.column_config.NumberColumn("Avg turnover ×", format="%.2f"),
                "Σ holding €/yr": st.column_config.NumberColumn(format="€ %.0f"),
            },
        )

    st.divider()

    # ── Part filter + Excel export ───────────────────────────────────────────
    all_parts = sorted({r.get("part_number") for r in recs}
                       | {r.get("part_number") for r in rows})
    fc1, fc2 = st.columns([3, 1])
    with fc1:
        selected_parts = st.multiselect(
            "Filter by part", options=all_parts, default=[],
            help="Leave empty to show all parts. Filters the tables below and "
                 "the per-part sheets of the Excel export.",
        )
    part_set = set(selected_parts) if selected_parts else None
    f_recs = [r for r in recs if (part_set is None or r.get("part_number") in part_set)]
    f_rows = [r for r in rows if (part_set is None or r.get("part_number") in part_set)]

    with fc2:
        st.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
        st.download_button(
            "⬇️ Export to Excel",
            data=_build_excel(run, summary, psum, f_recs, f_rows),
            file_name=f"ddmrp_vs_mrp_sim_run{run.id}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    if part_set is not None:
        st.caption(f"Filtered to **{len(part_set)}** part(s).")

    # ── SAP prescription per part (AS-IS → recommended) ──────────────────────
    if f_recs and "as_is_fill" in f_recs[0]:
        st.markdown("#### SAP prescription per part — AS-IS → recommended")
        st.caption(
            "For each part: today's SAP setup (AS-IS) vs the recommended "
            "methodology and the exact SAP target to configure. "
            "Δ fill > 0 = better service · Δ stock € > 0 = inventory freed · "
            "🔒 = already optimal (keep current)."
        )
        pdf = pd.DataFrame(f_recs)
        pdf["Keep"] = pdf["keep_current"].map(lambda b: "🔒" if b else "")
        pdf["AS-IS fill %"] = pdf["as_is_fill"].map(
            lambda v: v * 100.0 if v is not None else None)
        pdf["Reco fill %"] = pdf["best_fill"] * 100.0
        pdf["Δ fill pts"] = pdf["delta_fill"].map(
            lambda v: v * 100.0 if v is not None else None)
        pview = pdf.rename(columns={
            "part_number": "Part",
            "as_is_mrp_type": "AS-IS MRP",
            "as_is_stock_value": "AS-IS stock €",
            "best_policy_label": "Recommended",
            "target_mrp_type": "Target MRP Type",
            "target_safety_stock": "Target SS",
            "target_reorder_point": "Target ROP",
            "target_lot": "Target Lot",
            "best_stock_value": "Reco stock €",
            "delta_stock_value": "Δ stock €",
        })
        pcols = ["Part", "Keep", "AS-IS MRP", "AS-IS fill %", "AS-IS stock €",
                 "Recommended", "Target MRP Type", "Target SS", "Target ROP",
                 "Target Lot", "Reco fill %", "Reco stock €",
                 "Δ fill pts", "Δ stock €"]
        pcols = [c for c in pcols if c in pview.columns]
        st.dataframe(
            pview[pcols], use_container_width=True, hide_index=True, height=360,
            column_config={
                "AS-IS fill %": st.column_config.NumberColumn(format="%.1f%%"),
                "Reco fill %":  st.column_config.NumberColumn(format="%.1f%%"),
                "AS-IS stock €": st.column_config.NumberColumn(format="€ %.0f"),
                "Reco stock €":  st.column_config.NumberColumn(format="€ %.0f"),
                "Target SS":  st.column_config.NumberColumn(format="%.0f"),
                "Target ROP": st.column_config.NumberColumn(format="%.0f"),
                "Target Lot": st.column_config.NumberColumn(format="%.0f"),
                "Δ fill pts": st.column_config.NumberColumn(format="%+.1f"),
                "Δ stock €":  st.column_config.NumberColumn(format="€ %+.0f"),
            },
        )

    # ── Accept recommendations → assign methodology in master data ───────────
    if f_recs and "as_is_fill" in f_recs[0]:
        _render_assignment_editor(f_recs)

    # ── Per-item recommendation (DDMRP vs best MRP) ──────────────────────────
    if f_recs:
        st.markdown("#### Recommendation per part — DDMRP vs MRP")
        st.caption(
            "Best policy = lowest average stock value among policies meeting the "
            "target fill rate. Δ vs MRP < 0 means DDMRP holds **less** stock than "
            "the cheapest MRP policy."
        )
        rdf = pd.DataFrame(f_recs)
        rdf["Eligible"] = rdf["is_ddmrp_eligible"].map(lambda b: "✅" if b else "—")
        rdf["Best"] = rdf["best_policy_label"]
        rdf["DDMRP wins"] = rdf["ddmrp_wins"].map(lambda b: "🏆" if b else "")
        rdf["Best fill %"] = rdf["best_fill"] * 100.0
        rdf["DDMRP fill %"] = rdf["ddmrp_fill"] * 100.0
        view = rdf.rename(columns={
            "part_number": "Part",
            "best_stock_value": "Best stock €",
            "best_turnover": "Best turnover",
            "ddmrp_stock_value": "DDMRP stock €",
            "ddmrp_turnover": "DDMRP turnover",
            "ddmrp_stock_delta_vs_mrp": "Δ stock vs MRP €",
        })
        cols = ["Part", "Eligible", "Best", "DDMRP wins",
                "Best fill %", "Best stock €", "DDMRP fill %", "DDMRP stock €",
                "DDMRP turnover", "Δ stock vs MRP €"]
        cols = [c for c in cols if c in view.columns]
        st.dataframe(
            view[cols], use_container_width=True, hide_index=True, height=360,
            column_config={
                "Best fill %":  st.column_config.NumberColumn(format="%.1f%%"),
                "DDMRP fill %": st.column_config.NumberColumn(format="%.1f%%"),
                "Best stock €":  st.column_config.NumberColumn(format="€ %.0f"),
                "DDMRP stock €": st.column_config.NumberColumn(format="€ %.0f"),
                "DDMRP turnover": st.column_config.NumberColumn("DDMRP turnover ×", format="%.2f"),
                "Δ stock vs MRP €": st.column_config.NumberColumn(format="€ %.0f"),
            },
        )

    # ── Deep analysis — per-part daily trace ─────────────────────────────────
    if all_parts:
        avail = [p.get("policy") for p in psum] or ["as_is", "ddmrp"]
        # best policy per part for a sensible default overlay
        best_by_part = {r.get("part_number"): r.get("best_policy") for r in recs}
        _render_trace_block(all_parts, summary, avail, best_by_part, "cmp")

    # ── Full detail ──────────────────────────────────────────────────────────
    if f_rows:
        with st.expander("Full detail — every part × policy", expanded=bool(part_set)):
            ddf = pd.DataFrame(f_rows)
            ddf["Policy"] = ddf["policy"].map(lambda p: POLICY_LABELS.get(p, p))
            ddf["Fill %"] = ddf["unit_fill_rate"] * 100.0
            ddf["CSL %"] = ddf["cycle_service_level"] * 100.0
            view = ddf.rename(columns={
                "part_number": "Part",
                "avg_on_hand_units": "Avg OH units",
                "avg_on_hand_value": "Avg OH €",
                "turnover_index": "Turnover",
                "n_orders": "# Orders",
                "annual_holding_cost": "Holding €/yr",
            })
            cols = ["Part", "Policy", "Fill %", "CSL %", "Avg OH units", "Avg OH €",
                    "Turnover", "# Orders", "Holding €/yr"]
            cols = [c for c in cols if c in view.columns]
            st.dataframe(
                view[cols], use_container_width=True, hide_index=True, height=400,
                column_config={
                    "Fill %": st.column_config.NumberColumn(format="%.1f%%"),
                    "CSL %":  st.column_config.NumberColumn(format="%.1f%%"),
                    "Avg OH units": st.column_config.NumberColumn(format="%.1f"),
                    "Avg OH €": st.column_config.NumberColumn(format="€ %.0f"),
                    "Turnover": st.column_config.NumberColumn("Turnover ×", format="%.2f"),
                    "Holding €/yr": st.column_config.NumberColumn(format="€ %.0f"),
                },
            )


def _render_trace_block(all_parts: list, summary: dict, avail_policies: list,
                        best_by_part: dict, key_prefix: str) -> None:
    """Deep-analysis expander: stock / demand / planned-supply / inventory-value
    trend for ONE selected part, overlaying one or more policies."""
    from modules.optimization.ses_simulation import simulate_trace

    with st.expander("🔬 Deep analysis — per-part daily trace", expanded=False):
        c1, c2 = st.columns([2, 3])
        with c1:
            part = st.selectbox("Part", options=all_parts,
                                key=f"{key_prefix}_trace_part")
        # default overlay: AS-IS + this part's best policy (fallback ddmrp)
        best = best_by_part.get(part) or ("ddmrp" if "ddmrp" in avail_policies
                                          else avail_policies[0])
        default = [p for p in ("as_is", best) if p in avail_policies] or avail_policies[:1]
        with c2:
            policies = st.multiselect(
                "Policies to overlay", options=avail_policies, default=default,
                format_func=lambda k: POLICY_LABELS.get(k, k),
                key=f"{key_prefix}_trace_pols",
            )
        if not part or not policies:
            st.info("Select a part and at least one policy to plot its trace.")
            return

        company_id = get_company_id()
        item = _items_map(company_id).get(part)
        if item is None:
            st.warning("Part not found in current master data — re-run the simulation.")
            return

        horizon = int(summary.get("horizon_days", 180))
        demand_mode = summary.get("demand_mode", "synthetic")
        demand_profile = summary.get("demand_profile", "stable")
        target = float(summary.get("service_target", 0.95))
        seed = int(summary.get("seed", 42))

        traces = {}
        for pol in policies:
            tr = simulate_trace(
                item, pol, horizon=horizon, demand_mode=demand_mode,
                demand_profile=demand_profile, service_target=target, seed=seed)
            if tr:
                tr["policy"] = pol
                traces[POLICY_LABELS.get(pol, pol)] = tr
        if not traces:
            st.warning("No trace could be generated for this part.")
            return

        st.plotly_chart(
            _trace_chart(traces, f"Part {part} — daily trace ({horizon}d, 1 rep)"),
            use_container_width=True,
        )
        st.caption(
            "Single representative replication (seed-locked). Bars = daily "
            "demand · solid line = on-hand stock · triangles = planned supply "
            "landing · dotted line (lower panel) = inventory value (€). "
            "Demand is identical across policies for a clean comparison."
        )


def _render_assignment_editor(recs: list) -> None:
    """Editable table to accept a recommendation and persist the chosen
    methodology to the item master (drives the TO-BE scenario)."""
    st.markdown("#### Accept → assign methodology in master data")
    st.caption(
        "Tick **Accept** to flag a part with a methodology in the master data "
        "(DDMRP recommendations also set MRP Type override = DDMRP). The chosen "
        "policy defaults to the recommendation but you can override it. Saved "
        "assignments drive the **TO-BE Scenario** tab."
    )
    label_to_key = {v: k for k, v in _ASSIGN_LABELS.items()}
    id_by_part = {r["part_number"]: r["item_id"] for r in recs}

    edf = pd.DataFrame([{
        "Part":        r["part_number"],
        "Recommended": r["best_policy_label"],
        "Accept":      not bool(r.get("keep_current")),
        "Assign":      r["best_policy_label"],
    } for r in recs])

    edited = st.data_editor(
        edf, use_container_width=True, hide_index=True, height=320,
        key="assign_editor",
        column_config={
            "Part":        st.column_config.TextColumn(disabled=True),
            "Recommended": st.column_config.TextColumn(disabled=True),
            "Accept":      st.column_config.CheckboxColumn(help="Persist this assignment"),
            "Assign":      st.column_config.SelectboxColumn(
                options=[_KEEP_CURRENT, *_ASSIGN_LABELS.values()],
                help="Methodology to set in the master data",
            ),
        },
    )

    c1, c2 = st.columns([1, 3])
    with c1:
        save = st.button("💾 Save assignments", type="primary",
                         use_container_width=True)
    if save:
        mapping = {}
        for _, row in edited.iterrows():
            iid = id_by_part.get(row["Part"])
            if iid is None or not row["Accept"]:
                continue
            mapping[iid] = label_to_key.get(row["Assign"], "")
        n = assign_methodologies(mapping)
        if n:
            st.success(f"✓ Saved {n} assignment(s) to master data. "
                       f"Open the **TO-BE Scenario** tab to simulate the mix.")
        else:
            st.info("No parts ticked for Accept — nothing saved.")
    with c2:
        n_ticked = int(edited["Accept"].sum()) if len(edited) else 0
        st.caption(f"**{n_ticked}** part(s) ticked to accept.")


def _render_to_be(company_id: int, user_id) -> None:
    """TO-BE (future-state) tab: simulate the portfolio under the accepted
    methodology mix and compare to AS-IS."""
    st.markdown("### 🔮 TO-BE scenario — portfolio under the accepted mix")
    st.caption(
        "Each part is simulated under the methodology you accepted "
        "(Results tab → *Accept → assign*). Parts with no assignment stay on "
        "their current AS-IS policy. The result shows the client the final "
        "future state: service and inventory across the whole portfolio."
    )

    c1, c2 = st.columns(2)
    with c1:
        top_n = st.number_input(
            "Max parts (by € volume)", min_value=1, max_value=500, value=100,
            step=10, key="tobe_top_n",
        )
    with c2:
        only_eligible = st.checkbox("Only DDMRP-eligible parts", value=False,
                                    key="tobe_only_elig")

    try:
        inputs = load_simulation_inputs(
            company_id, only_ddmrp_eligible=only_eligible, top_n_items=int(top_n))
    except Exception as exc:
        st.error(f"Failed to load inputs: {exc}")
        return

    items = inputs["items"]
    n_items = len(items)
    n_assigned = sum(1 for it in items if (it.get("assigned_methodology") or ""))
    k1, k2, k3 = st.columns(3)
    k1.metric("Parts in scope", n_items)
    k2.metric("With accepted methodology", n_assigned)
    k3.metric("Will stay AS-IS", n_items - n_assigned)

    if n_assigned == 0:
        st.warning(
            "No parts have an accepted methodology yet. Go to the **Results** "
            "tab, tick **Accept** on the recommendations and save — then run here."
        )

    h1, h2, h3, h4 = st.columns(4)
    with h1:
        horizon = st.number_input("Horizon (days)", 30, 730, 180, 30, key="tobe_h")
    with h2:
        n_reps = st.number_input("Replications", 1, 200, 30, 5, key="tobe_r")
    with h3:
        demand_mode = st.radio("Demand", ["bootstrap", "synthetic"],
                               horizontal=True, key="tobe_dm")
    with h4:
        service_target = st.number_input("Target fill %", 50.0, 99.9, 95.0, 0.5,
                                         key="tobe_st") / 100.0

    if st.button("🚀 Run TO-BE simulation", type="primary",
                 use_container_width=True, disabled=(n_items == 0)):
        params = {
            "items": items, "mode": "to_be",
            "horizon_days": int(horizon), "n_replications": int(n_reps),
            "demand_mode": demand_mode, "demand_profile": "stable",
            "service_target": float(service_target), "seed": 42,
        }
        try:
            with st.spinner(f"Simulating TO-BE for {n_items} part(s)…"):
                result = solve_sim(params)
            run_id = record_run(
                company_id=company_id, user_id=user_id,
                session_key=f"{_SESSION_KEY}_tobe",
                scenario_name=f"TO-BE · {n_assigned} switched · {int(horizon)}d",
                params={"n_items": n_items, "n_switched": n_assigned,
                        "horizon_days": int(horizon), "demand_mode": demand_mode,
                        "service_target": float(service_target)},
                result=result,
            )
            if result.status == "solved":
                st.session_state[f"{_SESSION_KEY}_tobe_last_run_id"] = run_id
                st.success(f"✓ TO-BE simulated in {result.runtime_ms} ms · run #{run_id}")
            else:
                st.error(f"Status: {result.status}. {result.error_message or ''}")
        except Exception as exc:
            st.error(f"TO-BE simulation error: {exc}")

    run_id = st.session_state.get(f"{_SESSION_KEY}_tobe_last_run_id")
    if run_id:
        payloads = load_run_payloads(int(run_id), company_id)
        if payloads:
            _render_to_be_results(payloads["_run"], payloads)


def _render_to_be_results(run, payloads: dict) -> None:
    if run.status != "solved":
        st.error(f"Status: {run.status}. {run.error_message or ''}")
        return
    s = payloads.get("to_be_summary", {})
    detail = payloads.get("to_be_detail", {}).get("rows", [])
    if not s:
        return

    st.divider()
    st.markdown(f"#### Result — run #{run.id}")
    asis = s.get("total_asis_stock_value", 0.0)
    tobe = s.get("total_tobe_stock_value", 0.0)
    freed = s.get("working_capital_freed", 0.0)
    pct = s.get("working_capital_freed_pct", 0.0)
    af = s.get("avg_asis_fill")
    tf = s.get("avg_tobe_fill")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Σ inventory — AS-IS", f"€ {asis:,.0f}")
    k2.metric("Σ inventory — TO-BE", f"€ {tobe:,.0f}",
              delta=f"-€ {freed:,.0f}" if freed >= 0 else f"+€ {-freed:,.0f}")
    k3.metric("Working capital freed", f"€ {freed:,.0f}", delta=f"{pct:.1f}%")
    k4.metric("Avg fill — AS-IS → TO-BE", f"{(tf or 0) * 100:.1f}%",
              delta=(f"+{(tf - af) * 100:.1f} pts"
                     if (af is not None and tf is not None) else None))
    st.caption(
        f"Parts switched: **{s.get('n_switched', 0)}** · staying AS-IS: "
        f"**{s.get('n_unchanged', 0)}** · AS-IS avg fill **{(af or 0) * 100:.1f}%**"
    )

    mix = s.get("methodology_mix", [])
    if mix:
        mdf = pd.DataFrame(mix)
        fig = go.Figure()
        fig.add_bar(
            x=mdf["policy_label"], y=mdf["n_items"],
            marker_color=["#6366F1" if p == "ddmrp" else
                          "#94A3B8" if p == "as_is" else "#22C55E"
                          for p in mdf["policy"]],
            hovertemplate="<b>%{x}</b><br>%{y} parts<extra></extra>",
        )
        fig.update_layout(title="TO-BE methodology mix (parts per policy)",
                          height=320, margin=dict(t=40, b=80, l=40, r=20),
                          yaxis_title="parts", plot_bgcolor="white")
        st.plotly_chart(fig, use_container_width=True)

    if detail:
        st.markdown("##### Per-part AS-IS → TO-BE")
        ddf = pd.DataFrame(detail)
        ddf["Switch"] = ddf["switched"].map(lambda b: "→" if b else "")
        ddf["AS-IS fill %"] = ddf["as_is_fill"] * 100.0
        ddf["TO-BE fill %"] = ddf["to_be_fill"] * 100.0
        ddf["Δ fill pts"] = ddf["delta_fill"] * 100.0
        view = ddf.rename(columns={
            "part_number": "Part", "as_is_mrp_type": "AS-IS MRP",
            "to_be_policy_label": "TO-BE methodology",
            "as_is_stock_value": "AS-IS stock €", "to_be_stock_value": "TO-BE stock €",
            "delta_stock_value": "Δ stock €",
        })
        cols = ["Part", "AS-IS MRP", "Switch", "TO-BE methodology",
                "AS-IS fill %", "TO-BE fill %", "AS-IS stock €", "TO-BE stock €",
                "Δ fill pts", "Δ stock €"]
        cols = [c for c in cols if c in view.columns]
        st.dataframe(
            view[cols], use_container_width=True, hide_index=True, height=400,
            column_config={
                "AS-IS fill %": st.column_config.NumberColumn(format="%.1f%%"),
                "TO-BE fill %": st.column_config.NumberColumn(format="%.1f%%"),
                "AS-IS stock €": st.column_config.NumberColumn(format="€ %.0f"),
                "TO-BE stock €": st.column_config.NumberColumn(format="€ %.0f"),
                "Δ fill pts": st.column_config.NumberColumn(format="%+.1f"),
                "Δ stock €": st.column_config.NumberColumn(format="€ %+.0f"),
            },
        )
        st.download_button(
            "⬇️ Export TO-BE to Excel",
            data=_build_tobe_excel(run, s, detail),
            file_name=f"ddmrp_to_be_run{run.id}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        # ── Deep analysis — AS-IS vs assigned TO-BE policy for one part ───────
        _render_tobe_trace_block(detail, s)


def _render_tobe_trace_block(detail: list, s: dict) -> None:
    """Deep-analysis expander for the TO-BE tab: overlay AS-IS vs the part's
    assigned TO-BE policy for one selected part."""
    from modules.optimization.ses_simulation import simulate_trace

    parts = [r.get("part_number") for r in detail]
    pol_by_part = {r.get("part_number"): (r.get("to_be_policy") or "as_is")
                   for r in detail}
    with st.expander("🔬 Deep analysis — per-part daily trace", expanded=False):
        part = st.selectbox("Part", options=parts, key="tobe_trace_part")
        if not part:
            return
        to_be_pol = pol_by_part.get(part, "as_is")
        policies = ["as_is"] if to_be_pol == "as_is" else ["as_is", to_be_pol]

        company_id = get_company_id()
        item = _items_map(company_id).get(part)
        if item is None:
            st.warning("Part not found in current master data — re-run the simulation.")
            return

        horizon = int(s.get("horizon_days", 180))
        demand_mode = s.get("demand_mode", "synthetic")
        demand_profile = s.get("demand_profile", "stable")
        target = float(s.get("service_target", 0.95))

        traces = {}
        for pol in policies:
            tr = simulate_trace(
                item, pol, horizon=horizon, demand_mode=demand_mode,
                demand_profile=demand_profile, service_target=target, seed=42)
            if tr:
                tr["policy"] = pol
                traces[POLICY_LABELS.get(pol, pol)] = tr
        if not traces:
            st.warning("No trace could be generated for this part.")
            return

        st.plotly_chart(
            _trace_chart(traces, f"Part {part} — AS-IS vs TO-BE ({horizon}d, 1 rep)"),
            use_container_width=True,
        )
        st.caption(
            "AS-IS (current SAP) vs the accepted TO-BE methodology for this part. "
            "Bars = daily demand · solid line = on-hand stock · triangles = "
            "planned supply landing · dotted line (lower panel) = inventory value (€)."
        )


def _build_tobe_excel(run, summary: dict, detail: list) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        exec_rows = [
            ("Run", f"#{run.id} — {run.scenario_name or '—'}"),
            ("Σ inventory — AS-IS €", summary.get("total_asis_stock_value")),
            ("Σ inventory — TO-BE €", summary.get("total_tobe_stock_value")),
            ("Working capital freed €", summary.get("working_capital_freed")),
            ("Working capital freed %", summary.get("working_capital_freed_pct")),
            ("Avg fill — AS-IS", summary.get("avg_asis_fill")),
            ("Avg fill — TO-BE", summary.get("avg_tobe_fill")),
            ("Parts switched", summary.get("n_switched")),
            ("Parts staying AS-IS", summary.get("n_unchanged")),
        ]
        pd.DataFrame(exec_rows, columns=["Metric", "Value"]).to_excel(
            xl, sheet_name="TO-BE Summary", index=False)
        if summary.get("methodology_mix"):
            pd.DataFrame(summary["methodology_mix"]).rename(columns={
                "policy_label": "Methodology", "n_items": "Parts",
            })[["Methodology", "Parts"]].to_excel(
                xl, sheet_name="Methodology Mix", index=False)
        if detail:
            pd.DataFrame(detail).rename(columns={
                "part_number": "Part", "description": "Description",
                "as_is_mrp_type": "AS-IS MRP Type",
                "to_be_policy_label": "TO-BE methodology", "switched": "Switched",
                "as_is_fill": "AS-IS fill", "to_be_fill": "TO-BE fill",
                "as_is_stock_value": "AS-IS stock value",
                "to_be_stock_value": "TO-BE stock value",
                "delta_fill": "Δ fill", "delta_stock_value": "Δ stock value (freed)",
                "target_safety_stock": "Target Safety Stock",
                "target_reorder_point": "Target Reorder Point",
                "target_lot": "Target Lot",
            }).drop(columns=["item_id", "to_be_policy"], errors="ignore").to_excel(
                xl, sheet_name="Per-part TO-BE", index=False)
    buf.seek(0)
    return buf.read()


def _build_excel(run, summary: dict, psum: list, recs: list, rows: list) -> bytes:
    """Serialise the (optionally part-filtered) results into a multi-sheet .xlsx."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        # Run metadata
        meta = pd.DataFrame([
            ("Run", f"#{run.id} — {run.scenario_name or '—'}"),
            ("Created", run.created_at.strftime("%Y-%m-%d %H:%M")),
            ("Horizon (days)", summary.get("horizon_days")),
            ("Replications", summary.get("n_replications")),
            ("Demand mode", summary.get("demand_mode")),
            ("Demand profile", summary.get("demand_profile")),
            ("Target fill rate", summary.get("service_target")),
            ("Parts simulated", summary.get("n_items")),
        ], columns=["Field", "Value"])
        meta.to_excel(xl, sheet_name="Run Info", index=False)

        # Executive summary (portfolio AS-IS vs recommended)
        if summary.get("total_asis_stock_value") is not None:
            exec_rows = [
                ("Σ inventory — AS-IS (current SAP) €", summary.get("total_asis_stock_value")),
                ("Σ inventory — recommended €", summary.get("total_reco_stock_value")),
                ("Working capital freed €", summary.get("working_capital_freed")),
                ("Working capital freed %", summary.get("working_capital_freed_pct")),
                ("Avg fill — AS-IS", summary.get("avg_asis_fill")),
                ("Avg fill — recommended", summary.get("avg_reco_fill")),
                ("Parts unmanaged today (ND)", summary.get("n_unmanaged")),
                ("Parts where no policy meets target", summary.get("n_no_policy_meets")),
                ("Parts already optimal (keep current)", summary.get("n_keep_current")),
            ]
            pd.DataFrame(exec_rows, columns=["Metric", "Value"]).to_excel(
                xl, sheet_name="Executive Summary", index=False)

        # SAP prescription (per part, actionable settings)
        if recs and "as_is_fill" in recs[0]:
            sp = pd.DataFrame(recs)[[
                c for c in (
                    "part_number", "description", "keep_current",
                    "as_is_mrp_type", "as_is_fill", "as_is_stock_value",
                    "best_policy_label", "target_mrp_type",
                    "target_safety_stock", "target_reorder_point", "target_lot",
                    "target_tor", "target_toy", "target_tog",
                    "best_fill", "best_stock_value",
                    "delta_fill", "delta_stock_value", "no_policy_meets",
                ) if c in recs[0]
            ]].rename(columns={
                "part_number": "Part",
                "description": "Description",
                "keep_current": "Keep current",
                "as_is_mrp_type": "AS-IS MRP Type",
                "as_is_fill": "AS-IS fill",
                "as_is_stock_value": "AS-IS stock value",
                "best_policy_label": "Recommended methodology",
                "target_mrp_type": "Target MRP Type",
                "target_safety_stock": "Target Safety Stock",
                "target_reorder_point": "Target Reorder Point",
                "target_lot": "Target Lot",
                "target_tor": "DDMRP TOR", "target_toy": "DDMRP TOY", "target_tog": "DDMRP TOG",
                "best_fill": "Recommended fill",
                "best_stock_value": "Recommended stock value",
                "delta_fill": "Δ fill", "delta_stock_value": "Δ stock value (freed)",
                "no_policy_meets": "No policy meets target",
            })
            sp.to_excel(xl, sheet_name="SAP Prescription", index=False)

        if psum:
            ps = pd.DataFrame(psum).rename(columns={
                "policy_label": "Policy",
                "avg_fill_rate": "Avg fill",
                "avg_csl": "Avg CSL",
                "total_stock_value": "Total stock value",
                "avg_turnover": "Avg turnover",
                "total_holding_cost": "Total holding cost/yr",
                "n_items": "Parts",
            })
            keep = ["Policy", "Avg fill", "Avg CSL", "Total stock value",
                    "Avg turnover", "Total holding cost/yr", "Parts"]
            ps[[c for c in keep if c in ps.columns]].to_excel(
                xl, sheet_name="Policy Summary", index=False)

        if recs:
            rd = pd.DataFrame(recs).rename(columns={
                "part_number": "Part",
                "is_ddmrp_eligible": "DDMRP eligible",
                "best_policy_label": "Best policy",
                "ddmrp_wins": "DDMRP wins",
                "best_fill": "Best fill",
                "best_stock_value": "Best stock value",
                "best_turnover": "Best turnover",
                "ddmrp_fill": "DDMRP fill",
                "ddmrp_stock_value": "DDMRP stock value",
                "ddmrp_turnover": "DDMRP turnover",
                "best_mrp_policy": "Best MRP policy",
                "best_mrp_stock_value": "Best MRP stock value",
                "ddmrp_stock_delta_vs_mrp": "DDMRP stock delta vs MRP",
            })
            rd = rd.drop(columns=[c for c in ("item_id", "description", "best_policy")
                                  if c in rd.columns])
            rd.to_excel(xl, sheet_name="Recommendation", index=False)

        if rows:
            dd = pd.DataFrame(rows)
            dd["policy"] = dd["policy"].map(lambda p: POLICY_LABELS.get(p, p))
            dd = dd.rename(columns={
                "part_number": "Part",
                "policy": "Policy",
                "unit_fill_rate": "Fill",
                "cycle_service_level": "CSL",
                "avg_on_hand_units": "Avg OH units",
                "avg_on_hand_value": "Avg OH value",
                "turnover_index": "Turnover",
                "n_orders": "# Orders",
                "annual_holding_cost": "Holding cost/yr",
                "ordering_cost_window": "Ordering cost (window)",
                "meets_target": "Meets target",
                "unit_cost": "Unit cost",
            })
            dd = dd.drop(columns=[c for c in ("item_id", "description")
                                  if c in dd.columns])
            front = ["Part", "Policy", "Fill", "CSL", "Avg OH units", "Avg OH value",
                     "Turnover", "# Orders", "Holding cost/yr"]
            ordered = [c for c in front if c in dd.columns] + \
                      [c for c in dd.columns if c not in front]
            dd[ordered].to_excel(xl, sheet_name="Detail by Part x Policy", index=False)

    buf.seek(0)
    return buf.read()
