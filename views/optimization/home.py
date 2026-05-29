"""
Optimization Sessions — Home dispatcher.

Renders the catalogue of deterministic-optimization sessions and routes
into each session view. Each card mirrors a chapter-skill from the book
but executes a real solver (no LLM in the loop here).
"""

from __future__ import annotations

import streamlit as st


SESSIONS: list[dict] = [
    {
        "key":   "ses01_sourcing",
        "title": "Session 1 — Sourcing Allocation",
        "icon":  "📦",
        "tag":   "Ch2 LP + Ch3 IP",
        "blurb": "Allocate item demand across suppliers to minimise total cost while balancing reliability and lead time. Solver: PuLP / CBC.",
        "status": "available",
    },
    {
        "key":   "ses02_network",
        "title": "Session 2 — Multi-Echelon Network Flow",
        "icon":  "🌐",
        "tag":   "Ch4 Network LP",
        "blurb": "Push flow from plants → DCs → customers at minimum cost; visualise the Sankey of optimal lanes.",
        "status": "available",
    },
    {
        "key":   "ses03_facility",
        "title": "Session 3 — Facility Location",
        "icon":  "🏭",
        "tag":   "Ch7 UFLP / CFLP",
        "blurb": "Decide which warehouses or plants to open under capacity constraints to minimise fixed + variable cost.",
        "status": "available",
    },
    {
        "key":   "ses04_decoupling",
        "title": "Session 4 — Decoupling Point Placement",
        "icon":  "🛡️",
        "tag":   "Ch10 SSPP",
        "blurb": "Choose stocking nodes along the BOM/network to minimise total safety stock while meeting service.",
        "status": "coming",
    },
    {
        "key":   "ses05_mps",
        "title": "Session 5 — Master Production Schedule",
        "icon":  "🏗️",
        "tag":   "Ch8 Lot-sizing MILP",
        "blurb": "Plan multi-period production with setups, holding, and capacity constraints.",
        "status": "coming",
    },
    {
        "key":   "ses06_scheduling",
        "title": "Session 6 — Scheduling & Rollout",
        "icon":  "📅",
        "tag":   "Ch11 WSPT + Ch12 RCPSP",
        "blurb": "Sequence the red queue and plan multi-task projects under resource constraints.",
        "status": "coming",
    },
    {
        "key":   "ses07_routing",
        "title": "Session 7 — Vehicle Routing",
        "icon":  "🚚",
        "tag":   "Ch13 TSP + Ch14 VRP",
        "blurb": "Build minimum-cost routes for a delivery fleet. Solver: OR-Tools.",
        "status": "coming",
    },
    {
        "key":   "ses08_finance",
        "title": "Session 8 — Credit Term & Cash Trade-off",
        "icon":  "💰",
        "tag":   "Ch15 NLP",
        "blurb": "Find the supplier credit term that balances cost-of-capital against demand elasticity.",
        "status": "coming",
    },
]


def show() -> None:
    # ── Routing ──────────────────────────────────────────────────────────────
    selected = st.session_state.get("optimization_session")
    if selected:
        if selected == "ses01_sourcing":
            from views.optimization import ses01_sourcing
            ses01_sourcing.show()
            return
        if selected == "ses02_network":
            from views.optimization import ses02_network
            ses02_network.show()
            return
        if selected == "ses03_facility":
            from views.optimization import ses03_facility
            ses03_facility.show()
            return
        # Future sessions will be wired here as they ship.
        st.warning(f"Session `{selected}` is not yet available.")
        if st.button("← Back to catalogue"):
            st.session_state.optimization_session = None
            st.rerun()
        return

    # ── Catalogue ────────────────────────────────────────────────────────────
    st.title("🧮 Optimization Sessions")
    st.caption(
        "Deterministic operations-research solvers — eight sessions inspired "
        "by Haitao Li, *Optimization Modeling for Supply Chain Applications* "
        "(2023). Inputs come from your live DDMRP master data."
    )
    st.divider()

    cols = st.columns(2)
    for idx, ses in enumerate(SESSIONS):
        col = cols[idx % 2]
        with col:
            available = ses["status"] == "available"
            badge_color = "#16A34A" if available else "#6B7280"
            badge_label = "AVAILABLE" if available else "COMING SOON"
            st.markdown(
                f"<div style='border:1px solid #1f2937;border-radius:10px;"
                f"padding:14px 16px;margin-bottom:12px;background:#0F172A'>"
                f"<div style='display:flex;justify-content:space-between;"
                f"align-items:center;margin-bottom:6px'>"
                f"<span style='font-size:1.05rem;font-weight:700;color:#E8F0FF'>"
                f"{ses['icon']} {ses['title']}</span>"
                f"<span style='background:{badge_color};color:#fff;"
                f"padding:2px 8px;border-radius:8px;font-size:0.62rem;"
                f"font-weight:700;letter-spacing:0.05em'>{badge_label}</span>"
                f"</div>"
                f"<div style='font-size:0.7rem;color:#7A92BB;margin-bottom:6px'>"
                f"{ses['tag']}</div>"
                f"<div style='font-size:0.83rem;color:#CBD5E1;line-height:1.4'>"
                f"{ses['blurb']}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if available:
                if st.button(f"Open · {ses['title'].split('—')[0].strip()}",
                             key=f"open_{ses['key']}",
                             use_container_width=True,
                             type="primary"):
                    st.session_state.optimization_session = ses["key"]
                    st.rerun()
