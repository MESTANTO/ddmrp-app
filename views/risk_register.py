"""
Risk Register — Streamlit page.

Catalog of supply chain risks: per supplier, per item or network-level.
Each row carries inherent Likelihood × Impact score, a mitigation
strategy from Tang's 9, and a residual score post-mitigation.

Users can create / edit / close risks directly; the AI agent can
propose new risks via the Pending Changes queue.
"""

from __future__ import annotations

from datetime import datetime, date
import pandas as pd
import plotly.express as px
import streamlit as st

from database.auth import get_company_id
from database.db import get_session, Supplier, Item
from modules.risk_register import (
    RISK_CATEGORIES, RISK_NODES, RISK_STATUSES, TANG_STRATEGIES,
    create_risk, update_risk, delete_risk,
    list_risks, risk_summary,
)


def show():
    st.header("⚠️ Risk Register")
    st.caption(
        "Catalog of supply chain risks with Tang's 9 mitigation strategies. "
        "Likelihood × Impact gives the inherent score (1–25); residual score "
        "tracks the post-mitigation exposure."
    )

    company_id = get_company_id()
    if not company_id:
        st.error("No active company.")
        return

    # ── Summary cards
    summary = risk_summary(company_id)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Open",       summary.open_count)
    c2.metric("Mitigating", summary.mitigating_count)
    c3.metric("Accepted",   summary.accepted_count)
    c4.metric("Closed",     summary.closed_count)
    c5.metric("Inherent exposure", summary.total_inherent_exposure,
              delta=f"Residual {summary.total_residual_exposure}",
              delta_color="off")

    tab_active, tab_add, tab_edit, tab_heatmap, tab_history = st.tabs([
        "🔴 Active", "➕ Add Risk", "✏️ Edit / Close",
        "🗺️ Heatmap", "📚 History",
    ])

    with tab_active:
        _render_active_tab(company_id)

    with tab_add:
        _render_add_tab(company_id)

    with tab_edit:
        _render_edit_tab(company_id)

    with tab_heatmap:
        _render_heatmap_tab(company_id)

    with tab_history:
        _render_history_tab(company_id)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — load supplier + item options
# ─────────────────────────────────────────────────────────────────────────────

def _load_options(company_id: int):
    session = get_session()
    try:
        sups = (session.query(Supplier)
                .filter(Supplier.company_id == company_id)
                .order_by(Supplier.code).all())
        items = (session.query(Item)
                 .filter(Item.company_id == company_id)
                 .order_by(Item.part_number).all())
    finally:
        session.close()
    sup_options = {"— none —": None}
    sup_options.update({f"{s.code} — {s.name}": s.id for s in sups})
    item_options = {"— none —": None}
    item_options.update({f"{i.part_number} — {i.description}": i.id for i in items})
    return sup_options, item_options


def _risks_to_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Active risks table
# ─────────────────────────────────────────────────────────────────────────────

def _render_active_tab(company_id: int):
    f1, f2, f3 = st.columns(3)
    f_category = f1.selectbox("Category filter", ["(all)"] + RISK_CATEGORIES,
                              key="risk_active_cat")
    f_status   = f2.selectbox("Status",  ["(all)", "open", "mitigating"],
                              key="risk_active_status")
    f_node     = f3.selectbox("Node",    ["(all)"] + RISK_NODES,
                              key="risk_active_node")

    rows = list_risks(
        company_id=company_id,
        status=None if f_status == "(all)" else f_status,
        category=None if f_category == "(all)" else f_category,
        limit=200,
    )
    # Manual node filter (list_risks doesn't expose it)
    if f_node != "(all)":
        rows = [r for r in rows if r["node"] == f_node]

    # Active = not closed/accepted by default
    if f_status == "(all)":
        rows = [r for r in rows if r["status"] in ("open", "mitigating")]

    if not rows:
        st.info("No active risks match the filters.")
        return

    df = _risks_to_df(rows)
    display_cols = ["id", "title", "category", "node", "supplier_code", "part_number",
                    "likelihood", "impact", "inherent_score",
                    "mitigation_strategy", "residual_score",
                    "status", "owner", "due_date"]
    df = df[display_cols]
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config={
                     "inherent_score": st.column_config.NumberColumn("Inherent", format="%d"),
                     "residual_score": st.column_config.NumberColumn("Residual", format="%d"),
                 })


# ─────────────────────────────────────────────────────────────────────────────
# Add risk
# ─────────────────────────────────────────────────────────────────────────────

def _render_add_tab(company_id: int):
    sup_options, item_options = _load_options(company_id)

    with st.form("risk_add_form", clear_on_submit=True):
        title = st.text_input("Title *", placeholder="Short headline (e.g. 'Single source for resin')")
        description = st.text_area("Description", placeholder="What is the risk, what triggers it?")

        c1, c2, c3 = st.columns(3)
        category = c1.selectbox("Category", RISK_CATEGORIES,
                                index=RISK_CATEGORIES.index("operational"))
        node     = c2.selectbox("Node",     RISK_NODES,
                                index=RISK_NODES.index("supplier"))
        status   = c3.selectbox("Status",   ["open", "mitigating"], index=0)

        c4, c5 = st.columns(2)
        sup_pick  = c4.selectbox("Linked supplier",  list(sup_options.keys()))
        item_pick = c5.selectbox("Linked item",      list(item_options.keys()))

        c6, c7, c8 = st.columns(3)
        likelihood = c6.slider("Likelihood (1–5)", 1, 5, 3)
        impact     = c7.slider("Impact (1–5)",     1, 5, 3)
        c8.metric("Inherent score", likelihood * impact)

        c9, c10 = st.columns(2)
        strategy = c9.selectbox("Mitigation strategy (Tang's 9)", TANG_STRATEGIES,
                                index=TANG_STRATEGIES.index("other"))
        owner    = c10.text_input("Owner / responsible")
        mitigation_notes = st.text_area("Mitigation notes",
                                        placeholder="Concrete actions, dates, contracts…")

        c11, c12, c13 = st.columns(3)
        r_l = c11.slider("Residual likelihood", 0, 5, 0,
                         help="0 = leave blank (no mitigation assessment yet)")
        r_i = c12.slider("Residual impact",     0, 5, 0)
        due = c13.date_input("Due date", value=None)

        submit = st.form_submit_button("➕ Add risk", type="primary",
                                       use_container_width=True)
        if submit:
            if not title.strip():
                st.error("Title is required.")
                return
            try:
                rid = create_risk(company_id, {
                    "title":         title.strip(),
                    "description":   description.strip(),
                    "category":      category,
                    "node":          node,
                    "supplier_id":   sup_options[sup_pick],
                    "item_id":       item_options[item_pick],
                    "likelihood":    likelihood,
                    "impact":        impact,
                    "mitigation_strategy": strategy,
                    "mitigation_notes":    mitigation_notes.strip(),
                    "residual_likelihood": r_l if r_l > 0 else None,
                    "residual_impact":     r_i if r_i > 0 else None,
                    "status":        status,
                    "owner":         owner.strip(),
                    "due_date":      due.isoformat() if isinstance(due, date) else None,
                })
                st.success(f"Risk #{rid} added.")
            except Exception as exc:
                st.error(f"Could not add risk: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Edit / Close
# ─────────────────────────────────────────────────────────────────────────────

def _render_edit_tab(company_id: int):
    rows = list_risks(company_id=company_id, limit=500)
    if not rows:
        st.info("No risks yet.")
        return

    pick_options = {f"#{r['id']} — {r['title']} ({r['status']})": r["id"]
                    for r in rows}
    pick = st.selectbox("Pick a risk to edit", list(pick_options.keys()),
                        key="risk_edit_pick")
    rid = pick_options[pick]
    risk = next(r for r in rows if r["id"] == rid)

    with st.form("risk_edit_form"):
        title = st.text_input("Title", value=risk["title"])
        description = st.text_area("Description", value=risk["description"])

        c1, c2, c3 = st.columns(3)
        category = c1.selectbox("Category", RISK_CATEGORIES,
                                index=RISK_CATEGORIES.index(risk["category"])
                                if risk["category"] in RISK_CATEGORIES else 0)
        node     = c2.selectbox("Node", RISK_NODES,
                                index=RISK_NODES.index(risk["node"])
                                if risk["node"] in RISK_NODES else 0)
        status   = c3.selectbox("Status", RISK_STATUSES,
                                index=RISK_STATUSES.index(risk["status"])
                                if risk["status"] in RISK_STATUSES else 0)

        c4, c5, c6 = st.columns(3)
        likelihood = c4.slider("Likelihood", 1, 5, int(risk["likelihood"] or 3))
        impact     = c5.slider("Impact",     1, 5, int(risk["impact"] or 3))
        c6.metric("Inherent score", likelihood * impact)

        strategy = st.selectbox("Mitigation strategy", TANG_STRATEGIES,
                                index=TANG_STRATEGIES.index(risk["mitigation_strategy"])
                                if risk["mitigation_strategy"] in TANG_STRATEGIES else 0)
        mitigation_notes = st.text_area("Mitigation notes",
                                        value=risk["mitigation_notes"])

        c7, c8, c9 = st.columns(3)
        r_l = c7.slider("Residual likelihood", 0, 5,
                        int(risk["residual_likelihood"] or 0))
        r_i = c8.slider("Residual impact", 0, 5,
                        int(risk["residual_impact"] or 0))
        owner = c9.text_input("Owner", value=risk["owner"])

        due_default = (datetime.fromisoformat(risk["due_date"]).date()
                       if risk["due_date"] else None)
        due = st.date_input("Due date", value=due_default)

        b1, b2 = st.columns(2)
        save_btn  = b1.form_submit_button("💾 Save changes", type="primary",
                                           use_container_width=True)
        del_btn   = b2.form_submit_button("🗑️ Delete risk",
                                           use_container_width=True)

        if save_btn:
            try:
                ok = update_risk(company_id, rid, {
                    "title":         title.strip(),
                    "description":   description.strip(),
                    "category":      category,
                    "node":          node,
                    "status":        status,
                    "likelihood":    likelihood,
                    "impact":        impact,
                    "mitigation_strategy": strategy,
                    "mitigation_notes":    mitigation_notes.strip(),
                    "residual_likelihood": r_l if r_l > 0 else None,
                    "residual_impact":     r_i if r_i > 0 else None,
                    "owner":         owner.strip(),
                    "due_date":      due.isoformat() if isinstance(due, date) else None,
                })
                if ok:
                    st.success(f"Risk #{rid} updated.")
                else:
                    st.error("Could not update (not found or cross-company).")
            except Exception as exc:
                st.error(f"Update failed: {exc}")

        if del_btn:
            if delete_risk(company_id, rid):
                st.success(f"Risk #{rid} deleted.")
                st.rerun()
            else:
                st.error("Could not delete.")


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap
# ─────────────────────────────────────────────────────────────────────────────

def _render_heatmap_tab(company_id: int):
    rows = list_risks(company_id=company_id, limit=500)
    rows = [r for r in rows if r["status"] in ("open", "mitigating")]
    if not rows:
        st.info("No active risks to plot.")
        return

    df = _risks_to_df(rows)
    grid = (df.groupby(["likelihood", "impact"])
            .size().reset_index(name="count"))
    pivot = grid.pivot(index="likelihood", columns="impact",
                       values="count").fillna(0).astype(int)
    # Ensure 1..5 axes
    for i in range(1, 6):
        if i not in pivot.index:    pivot.loc[i] = 0
        if i not in pivot.columns:  pivot[i] = 0
    pivot = pivot.sort_index().reindex(sorted(pivot.columns), axis=1)

    fig = px.imshow(
        pivot.values,
        x=[f"Impact {c}" for c in pivot.columns],
        y=[f"Likelihood {r}" for r in pivot.index],
        color_continuous_scale="Reds",
        text_auto=True,
        title="Active risks — count per (Likelihood × Impact) cell",
    )
    fig.update_layout(coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Top exposure (open + mitigating)")
    top = df.sort_values("inherent_score", ascending=False).head(10)
    show_cols = ["id", "title", "category", "node", "supplier_code", "part_number",
                 "likelihood", "impact", "inherent_score",
                 "residual_score", "mitigation_strategy", "status", "owner"]
    st.dataframe(top[show_cols], use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────

def _render_history_tab(company_id: int):
    rows = list_risks(company_id=company_id, limit=500)
    rows = [r for r in rows if r["status"] in ("accepted", "closed")]
    if not rows:
        st.info("No accepted or closed risks yet.")
        return
    df = _risks_to_df(rows)
    show_cols = ["id", "title", "category", "node", "supplier_code", "part_number",
                 "inherent_score", "residual_score",
                 "mitigation_strategy", "status", "owner", "updated_at"]
    st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
