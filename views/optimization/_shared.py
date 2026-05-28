"""
Shared helpers across optimization-session views.

Only UI-level utilities live here — solver logic stays in
`modules/optimization/`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import streamlit as st

from modules.optimization.solver_base import list_runs, load_run


def status_badge(status: str) -> str:
    """Return an HTML pill for a run status."""
    color = {
        "solved":     "#16A34A",
        "infeasible": "#DC2626",
        "failed":     "#DC2626",
        "running":    "#F59E0B",
        "pending":    "#6B7280",
    }.get(status, "#6B7280")
    return (
        f"<span style='background:{color};color:#fff;padding:2px 8px;"
        f"border-radius:10px;font-size:0.7rem;font-weight:600'>{status.upper()}</span>"
    )


def render_run_history(company_id: int, session_key: str, on_select=None) -> int | None:
    """
    Render a compact list of recent runs for this session.

    Returns the run_id the user clicked on (or None). The caller is
    responsible for loading + displaying the run.
    """
    runs = list_runs(company_id, session_key=session_key, limit=10)
    if not runs:
        st.info("No previous runs in the last 10 days. Configure inputs and run a scenario to begin.")
        return None

    st.caption("RECENT RUNS (last 10, expire after 10 days)")
    selected = None
    for r in runs:
        cols = st.columns([2, 3, 1.2, 1.2, 1])
        with cols[0]:
            st.markdown(
                f"**#{r.id}** · {r.scenario_name or '—'}<br>"
                f"<span style='font-size:0.7rem;color:#6B7280'>"
                f"{r.created_at.strftime('%Y-%m-%d %H:%M')}</span>",
                unsafe_allow_html=True,
            )
        with cols[1]:
            obj = f"€ {r.objective_value:,.0f}" if r.objective_value is not None else "—"
            st.markdown(
                f"objective: **{obj}**<br>"
                f"<span style='font-size:0.7rem;color:#6B7280'>"
                f"solver: {r.solver_used} · {r.runtime_ms or 0} ms</span>",
                unsafe_allow_html=True,
            )
        with cols[2]:
            st.markdown(status_badge(r.status), unsafe_allow_html=True)
        with cols[3]:
            ttl = (r.expires_at - datetime.utcnow()).days
            st.caption(f"expires in {max(ttl, 0)}d")
        with cols[4]:
            if st.button("Open", key=f"open_run_{r.id}"):
                selected = r.id
        st.divider()

    if selected and on_select:
        on_select(selected)
    return selected


def load_run_payloads(run_id: int, company_id: int) -> dict[str, Any] | None:
    """
    Load a run + return its artefacts as a flat dict keyed by `kind`.
    """
    res = load_run(run_id, company_id)
    if res is None:
        return None
    run, artefacts = res
    out: dict[str, Any] = {"_run": run}
    for a in artefacts:
        try:
            out[a.kind] = json.loads(a.payload_json)
        except Exception:
            out[a.kind] = {}
    return out
