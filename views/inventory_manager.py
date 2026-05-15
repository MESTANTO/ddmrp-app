"""
Inventory Manager Agent — Streamlit dashboard page.

Shows run controls, signal table, signal detail, and charts.
All data is scoped to the logged-in user's company_id.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from database.auth import get_company_id, get_current_user
from database.db import SessionLocal, AgentRun, AgentSignal

from views.ai_advisor import _KNOWN_MODELS   # reuse the model list


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

_SEV_ORDER  = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEV_COLOR  = {
    "critical": "#E74C3C",
    "high":     "#E67E22",
    "medium":   "#F1C40F",
    "low":      "#2980B9",
    "info":     "#7F8C8D",
}
_SEV_BADGE  = {
    "critical": "🔴 CRITICAL",
    "high":     "🟠 HIGH",
    "medium":   "🟡 MEDIUM",
    "low":      "🔵 LOW",
    "info":     "⚪ INFO",
}
_TYPE_LABEL = {
    "stockout_risk":   "⚡ Stockout Risk",
    "overstock":       "📦 Overstock",
    "low_nfp":         "📉 Low NFP",
    "buffer_resizing": "🔧 Buffer Resizing",
    "data_quality":    "🗂 Data Quality",
    "demand_trend":    "📈 Demand Trend",
    "abc_xyz_policy":  "🏷 ABC/XYZ Policy",
    "safety_stock_gap":"🛡 Safety Stock",
    "supplier_risk":   "🚚 Supplier Risk",
    "portfolio":       "🌐 Portfolio",
}


# ---------------------------------------------------------------------------
# DB helpers — all filtered by company_id
# ---------------------------------------------------------------------------

def _load_runs(company_id: int) -> list[dict]:
    session = SessionLocal()
    try:
        runs = (session.query(AgentRun)
                .filter(AgentRun.company_id == company_id)
                .order_by(AgentRun.run_at.desc())
                .limit(50)
                .all())
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in runs]
    finally:
        session.close()


def _load_signals(company_id: int, run_id: int) -> list[dict]:
    session = SessionLocal()
    try:
        sigs = (session.query(AgentSignal)
                .filter(AgentSignal.company_id == company_id,
                        AgentSignal.run_id     == run_id)
                .order_by(AgentSignal.id)
                .all())
        return [{c.name: getattr(s, c.name) for c in s.__table__.columns} for s in sigs]
    finally:
        session.close()


def _mark_actioned(signal_id: int, company_id: int, user_id: int):
    session = SessionLocal()
    try:
        sig = (session.query(AgentSignal)
               .filter(AgentSignal.id         == signal_id,
                       AgentSignal.company_id == company_id)
               .first())
        if sig:
            sig.is_actioned = True
            sig.actioned_at = datetime.utcnow()
            sig.actioned_by = user_id
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _is_run_in_progress(company_id: int) -> bool:
    session = SessionLocal()
    try:
        return (session.query(AgentRun)
                .filter(AgentRun.company_id == company_id,
                        AgentRun.status     == "running")
                .first()) is not None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def show():
    st.header("🤖 Inventory Manager Agent")
    st.caption(
        "An autonomous agent that analyses your full inventory — buffers, demand trends, "
        "ABC/XYZ classification, safety stock, and supplier risk — then surfaces prioritised "
        "findings and recommendations."
    )

    # ── API key ──────────────────────────────────────────────────────────────
    try:
        api_key = st.secrets["NVIDIA_API_KEY"]
    except Exception:
        st.error("**NVIDIA_API_KEY not found.** Add it via Manage app → Secrets.")
        return

    company_id = get_company_id()
    user       = get_current_user()
    user_id    = user["id"] if user else 0

    # ── Run controls ─────────────────────────────────────────────────────────
    with st.container():
        col_model, col_btn = st.columns([3, 1])
        with col_model:
            model = st.selectbox(
                "Model",
                _KNOWN_MODELS,
                index=_KNOWN_MODELS.index("deepseek-ai/deepseek-v3-0324")
                      if "deepseek-ai/deepseek-v3-0324" in _KNOWN_MODELS else 0,
                key="im_model_sel",
                label_visibility="collapsed",
            )
        with col_btn:
            in_progress = _is_run_in_progress(company_id)
            run_clicked = st.button(
                "▶  Run Analysis" if not in_progress else "⏳  Running…",
                type="primary",
                use_container_width=True,
                disabled=in_progress,
                key="im_run_btn",
            )

    if run_clicked:
        _execute_run(company_id, user_id, model, api_key)

    # ── Load run history ─────────────────────────────────────────────────────
    runs = _load_runs(company_id)

    if not runs:
        st.info("No agent runs yet. Click **Run Analysis** to start the first analysis.")
        return

    st.divider()

    # ── Summary metrics ───────────────────────────────────────────────────────
    last_run       = runs[0]
    last_completed = next((r for r in runs if r["status"] == "completed"), None)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Runs", len(runs))
    c2.metric("Last Run", last_run["run_at"].strftime("%d %b %H:%M") if last_run["run_at"] else "—")
    c3.metric("Signals (last run)", last_run.get("signals_generated", 0))
    if last_completed:
        sigs_last = _load_signals(company_id, last_completed["id"])
        critical_high = sum(1 for s in sigs_last if s["severity"] in ("critical", "high"))
        c4.metric("Critical / High", critical_high)
    else:
        c4.metric("Critical / High", "—")

    st.divider()

    # ── Run selector ─────────────────────────────────────────────────────────
    run_options = {
        f"{'✅' if r['status']=='completed' else '❌' if r['status']=='failed' else '⏳'} "
        f"{r['run_at'].strftime('%Y-%m-%d %H:%M')} — {r['signals_generated']} signals "
        f"({r['model_used'].split('/')[-1]})": r["id"]
        for r in runs
    }
    selected_label = st.selectbox("Select run to view", list(run_options.keys()), key="im_run_sel")
    selected_run_id = run_options[selected_label]
    selected_run    = next(r for r in runs if r["id"] == selected_run_id)

    if selected_run["status"] == "failed":
        st.error(f"This run failed: {selected_run.get('error_message', 'Unknown error')}")

    # Load signals for selected run
    signals = _load_signals(company_id, selected_run_id)

    if not signals:
        st.info("No signals for this run.")
        _show_raw_response(selected_run)
        return

    # ── Charts row ────────────────────────────────────────────────────────────
    _show_charts(signals, runs)

    st.divider()

    # ── Signal table ─────────────────────────────────────────────────────────
    _show_signal_table(signals, company_id, user_id)

    st.divider()

    # ── Run history table ─────────────────────────────────────────────────────
    with st.expander("📋 Run History", expanded=False):
        df_runs = pd.DataFrame(runs)
        df_runs["run_at"] = df_runs["run_at"].apply(
            lambda x: x.strftime("%Y-%m-%d %H:%M") if x else ""
        )
        df_runs = df_runs[["run_at", "model_used", "status",
                            "items_analysed", "signals_generated", "duration_seconds"]]
        df_runs.columns = ["Run At", "Model", "Status", "Items", "Signals", "Duration (s)"]
        st.dataframe(df_runs, use_container_width=True, hide_index=True)

    # ── Raw LLM response ─────────────────────────────────────────────────────
    _show_raw_response(selected_run)


# ---------------------------------------------------------------------------
# Execute run (with live progress feedback)
# ---------------------------------------------------------------------------

def _execute_run(company_id: int, user_id: int, model: str, api_key: str):
    from agents.inventory_agent import run_inventory_agent

    placeholder = st.empty()
    with placeholder.container():
        with st.spinner(f"Agent is analysing your inventory with **{model.split('/')[-1]}**… "
                        "This may take 1–3 minutes for large models."):
            try:
                run_dict, signal_dicts = run_inventory_agent(
                    company_id=company_id,
                    user_id=user_id,
                    model=model,
                    api_key=api_key,
                )
                status = run_dict.get("status", "unknown")
                n      = run_dict.get("signals_generated", 0)
                dur    = run_dict.get("duration_seconds", 0)
                if status == "completed":
                    placeholder.success(
                        f"✅ Analysis complete — **{n} signals** generated in {dur:.1f}s."
                    )
                else:
                    placeholder.error(
                        f"❌ Run failed: {run_dict.get('error_message', 'Unknown error')}"
                    )
            except Exception as exc:
                placeholder.error(f"❌ Unexpected error: {exc}")
    st.rerun()


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _show_charts(signals: list[dict], runs: list[dict]):
    col_a, col_b, col_c = st.columns(3)

    # Chart A — severity distribution
    with col_a:
        sev_counts = {}
        for s in signals:
            sev_counts[s["severity"]] = sev_counts.get(s["severity"], 0) + 1
        df_sev = pd.DataFrame([
            {"severity": k, "count": v, "color": _SEV_COLOR.get(k, "#999")}
            for k, v in sorted(sev_counts.items(), key=lambda x: _SEV_ORDER.get(x[0], 9))
        ])
        if not df_sev.empty:
            fig = px.bar(
                df_sev, x="severity", y="count",
                color="severity",
                color_discrete_map={r["severity"]: r["color"] for _, r in df_sev.iterrows()},
                labels={"severity": "", "count": "Signals"},
                title="Signals by Severity",
                height=280,
            )
            fig.update_layout(showlegend=False, margin=dict(t=40, b=20))
            st.plotly_chart(fig, use_container_width=True)

    # Chart B — signal type breakdown
    with col_b:
        type_counts = {}
        for s in signals:
            lbl = _TYPE_LABEL.get(s["signal_type"], s["signal_type"])
            type_counts[lbl] = type_counts.get(lbl, 0) + 1
        df_type = pd.DataFrame(
            [{"type": k, "count": v} for k, v in
             sorted(type_counts.items(), key=lambda x: -x[1])]
        )
        if not df_type.empty:
            fig2 = px.bar(
                df_type, x="count", y="type", orientation="h",
                labels={"count": "Signals", "type": ""},
                title="Signals by Type",
                height=280,
            )
            fig2.update_layout(margin=dict(t=40, b=20), yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig2, use_container_width=True)

    # Chart C — run history trend (signals over time)
    with col_c:
        completed = [r for r in runs if r["status"] == "completed"]
        if len(completed) >= 2:
            df_hist = pd.DataFrame([{
                "run_at": r["run_at"].strftime("%m-%d %H:%M"),
                "signals": r["signals_generated"],
            } for r in reversed(completed[-10:])])
            fig3 = px.line(
                df_hist, x="run_at", y="signals",
                markers=True,
                labels={"run_at": "", "signals": "Signals"},
                title="Signal Trend (last 10 runs)",
                height=280,
            )
            fig3.update_layout(margin=dict(t=40, b=20))
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.caption("Signal trend available after 2+ completed runs.")


# ---------------------------------------------------------------------------
# Signal table + detail panel
# ---------------------------------------------------------------------------

def _show_signal_table(signals: list[dict], company_id: int, user_id: int):
    st.subheader("Findings & Recommendations")

    # Filters
    fc1, fc2, fc3 = st.columns([2, 2, 1])
    with fc1:
        all_types = sorted({s["signal_type"] for s in signals})
        sel_types = st.multiselect(
            "Signal Type", all_types,
            default=all_types, key="im_filter_type",
            format_func=lambda x: _TYPE_LABEL.get(x, x),
        )
    with fc2:
        all_sevs = sorted({s["severity"] for s in signals}, key=lambda x: _SEV_ORDER.get(x, 9))
        sel_sevs = st.multiselect(
            "Severity", all_sevs,
            default=all_sevs, key="im_filter_sev",
        )
    with fc3:
        show_actioned = st.checkbox("Show actioned", value=False, key="im_show_actioned")

    filtered = [
        s for s in signals
        if s["signal_type"] in sel_types
        and s["severity"]    in sel_sevs
        and (show_actioned or not s["is_actioned"])
    ]

    if not filtered:
        st.info("No signals match the current filters.")
        return

    # Build display dataframe
    rows = []
    for s in filtered:
        rows.append({
            "id":          s["id"],
            "Severity":    _SEV_BADGE.get(s["severity"], s["severity"]),
            "Type":        _TYPE_LABEL.get(s["signal_type"], s["signal_type"]),
            "Part #":      s["part_number"],
            "Title":       s["title"],
            "Metric":      (f"{s['metric_name']} = {s['metric_value']:.2f}"
                            if s["metric_value"] is not None and s["metric_name"] else ""),
            "Actioned":    "✅" if s["is_actioned"] else "",
        })

    df = pd.DataFrame(rows)

    # Show table — row selection via selectbox for Streamlit compatibility
    st.caption(f"Showing {len(filtered)} signals")
    selected_idx = st.selectbox(
        "Select a signal to view details",
        options=range(len(filtered)),
        format_func=lambda i: f"{_SEV_BADGE.get(filtered[i]['severity'], '')}  {filtered[i]['title'][:80]}",
        key="im_signal_sel",
        label_visibility="collapsed",
    )

    # Compact table (non-interactive) for overview
    st.dataframe(
        df.drop(columns=["id"]),
        use_container_width=True,
        hide_index=True,
        height=min(40 + 35 * len(df), 420),
    )

    # ── Detail panel ─────────────────────────────────────────────────────────
    if selected_idx is not None and selected_idx < len(filtered):
        sig = filtered[selected_idx]
        _show_signal_detail(sig, company_id, user_id)


def _show_signal_detail(sig: dict, company_id: int, user_id: int):
    sev_color = _SEV_COLOR.get(sig["severity"], "#999")
    st.markdown(
        f"""<div style="border-left:4px solid {sev_color};
                        padding:0.75rem 1rem;
                        background:var(--bg-elevated,#112240);
                        border-radius:6px;margin-top:0.5rem">
            <div style="font-size:0.65rem;font-weight:700;letter-spacing:0.12em;
                        text-transform:uppercase;color:{sev_color}">
                {_SEV_BADGE.get(sig['severity'],sig['severity'])} &nbsp;·&nbsp;
                {_TYPE_LABEL.get(sig['signal_type'],sig['signal_type'])} &nbsp;·&nbsp;
                {sig['part_number']}
            </div>
            <div style="font-size:1.0rem;font-weight:600;margin:0.4rem 0 0.2rem;color:#E8F0FF">
                {sig['title']}
            </div>
        </div>""",
        unsafe_allow_html=True,
    )

    d_col, r_col = st.columns([1, 1])
    with d_col:
        st.markdown("**Analysis**")
        st.markdown(sig["detail"] or "_No detail provided._")
        if sig["metric_name"] and sig["metric_value"] is not None:
            st.caption(
                f"📊 `{sig['metric_name']}` = **{sig['metric_value']:.2f}**"
                + (f"  (threshold: {sig['metric_threshold']:.2f})"
                   if sig["metric_threshold"] is not None else "")
            )

    with r_col:
        st.markdown("**Recommended Action**")
        st.markdown(sig["recommendation"] or "_No recommendation provided._")

        if not sig["is_actioned"]:
            if st.button("✅ Mark as Actioned", key=f"action_{sig['id']}", type="secondary"):
                _mark_actioned(sig["id"], company_id, user_id)
                st.success("Marked as actioned.")
                st.rerun()
        else:
            at = sig["actioned_at"]
            ts = at.strftime("%Y-%m-%d %H:%M") if at else "unknown time"
            st.success(f"Actioned on {ts}")


# ---------------------------------------------------------------------------
# Raw LLM response
# ---------------------------------------------------------------------------

def _show_raw_response(run: dict):
    raw = run.get("llm_raw_response", "")
    if raw:
        with st.expander("🔍 View raw agent output", expanded=False):
            st.text(raw[:8000])
            if len(raw) > 8000:
                st.caption(f"…truncated ({len(raw):,} total chars)")
