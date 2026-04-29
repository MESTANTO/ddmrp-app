"""
BOM Engine + Streamlit page (deck slide 26).

Two responsibilities:
  1. compute_dlt(item)  — walks the BOM graph and returns the longest
     unprotected (non-buffered) lead-time chain above `item`.
  2. show()             — Streamlit UI to manage BOM lines and view
     the auto-computed DLT for every item.

DDMRP "Position" step (slide 26):
  DLT = longest path from the item's BOM root to the first
  decoupling point (buffered node) upstream.
  Buffered items break the chain — their DLT does NOT accumulate
  into their parent's DLT.

  Example:
    RM-A (DLT=5, buffered) ─┐
                             ├─ WIP-1 (DLT=3, NOT buffered) ─ FG (DLT=2)
    RM-B (DLT=7, buffered) ─┘

  DLT for WIP-1 = 3          (RM-A and RM-B are buffered → chain stops)
  DLT for FG    = 3 + 2 = 5  (WIP-1 is not buffered → chain continues)
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dataclasses import dataclass, field
from typing import Optional

from database.db import get_session, Item, Buffer, BomLine


# ---------------------------------------------------------------------------
# DLT computation
# ---------------------------------------------------------------------------

@dataclass
class DltResult:
    """Output of compute_dlt() for one item."""
    item_id: int
    part_number: str
    manual_dlt: float           # item.dlt as entered
    computed_dlt: float         # longest unprotected path through BOM
    critical_path: list[str]    # part numbers along the longest path
    is_buffered: bool           # True if this item has a DDMRP buffer


def _is_buffered(item_id: int, buffer_map: dict) -> bool:
    buf = buffer_map.get(item_id)
    return buf is not None


def compute_dlt(
    item: Item,
    bom_map: Optional[dict] = None,
    buffer_map: Optional[dict] = None,
    _visited: Optional[set] = None,
) -> DltResult:
    """
    Recursively walk the BOM to find the longest unprotected DLT chain.

    Parameters
    ----------
    item        : the item whose DLT we're computing
    bom_map     : pre-loaded {parent_item_id: [BomLine, ...]} — pass to avoid N+1 queries
    buffer_map  : pre-loaded {item_id: Buffer} — pass to avoid N+1 queries
    _visited    : cycle guard (internal)
    """
    if bom_map is None or buffer_map is None:
        session = get_session()
        try:
            all_lines = session.query(BomLine).all()
            all_buffers = {b.item_id: b for b in session.query(Buffer).all()}
        finally:
            session.close()
        bom_map    = {}
        for line in all_lines:
            bom_map.setdefault(line.parent_item_id, []).append(line)
        buffer_map = all_buffers

    if _visited is None:
        _visited = set()

    if item.id in _visited:
        # Cycle guard — return item's own DLT to avoid infinite recursion
        return DltResult(item.id, item.part_number, item.dlt or 0.0,
                         item.dlt or 0.0, [item.part_number],
                         _is_buffered(item.id, buffer_map))

    _visited = _visited | {item.id}

    children: list[BomLine] = bom_map.get(item.id, [])
    own_dlt = item.dlt or 0.0

    if not children:
        # Leaf node — DLT = own lead time
        return DltResult(
            item_id=item.id,
            part_number=item.part_number,
            manual_dlt=own_dlt,
            computed_dlt=own_dlt,
            critical_path=[item.part_number],
            is_buffered=_is_buffered(item.id, buffer_map),
        )

    # Recurse over children, but STOP accumulating if child is buffered
    best_child_dlt = 0.0
    best_path: list[str] = []

    session = get_session()
    try:
        child_items = {
            it.id: it for it in session.query(Item).filter(
                Item.id.in_([l.child_item_id for l in children])
            ).all()
        }
    finally:
        session.close()

    for line in children:
        child = child_items.get(line.child_item_id)
        if child is None:
            continue

        if _is_buffered(child.id, buffer_map):
            # Buffered child breaks the chain — its DLT does not propagate
            child_contribution = 0.0
            child_path = [child.part_number]
        else:
            child_result = compute_dlt(child, bom_map, buffer_map, _visited)
            child_contribution = child_result.computed_dlt
            child_path = child_result.critical_path

        if child_contribution > best_child_dlt:
            best_child_dlt = child_contribution
            best_path = child_path

    computed = own_dlt + best_child_dlt
    return DltResult(
        item_id=item.id,
        part_number=item.part_number,
        manual_dlt=own_dlt,
        computed_dlt=computed,
        critical_path=best_path + [item.part_number],
        is_buffered=_is_buffered(item.id, buffer_map),
    )


def compute_all_dlt() -> list[DltResult]:
    """Compute DLT for every item in the database."""
    session = get_session()
    try:
        items = session.query(Item).all()
        all_lines = session.query(BomLine).all()
        all_buffers = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    bom_map: dict = {}
    for line in all_lines:
        bom_map.setdefault(line.parent_item_id, []).append(line)

    results = []
    for item in items:
        try:
            results.append(compute_dlt(item, bom_map, all_buffers))
        except Exception as e:
            print(f"DLT compute error {item.part_number}: {e}")
    return results


# ---------------------------------------------------------------------------
# Streamlit page
# ---------------------------------------------------------------------------

def show():
    st.header("BOM & Automatic DLT")
    st.caption(
        "Define the Bill of Materials. The engine walks each item's upstream chain "
        "and computes the longest **unprotected** (non-buffered) lead-time path "
        "(DDMRP *Position* step — slide 26)."
    )

    tab_bom, tab_dlt, tab_graph = st.tabs([
        "⚙️ Manage BOM",
        "⏱️ Computed DLT",
        "🗺️ BOM Graph",
    ])

    with tab_bom:
        _bom_manager()

    with tab_dlt:
        _dlt_table()

    with tab_graph:
        _bom_graph()


# ---------------------------------------------------------------------------
# Tab 1 — BOM Manager
# ---------------------------------------------------------------------------

def _bom_manager():
    st.subheader("BOM Lines")

    session = get_session()
    try:
        lines = (
            session.query(BomLine)
            .order_by(BomLine.parent_item_id, BomLine.child_item_id)
            .all()
        )
        items = {it.id: it for it in session.query(Item).all()}
    finally:
        session.close()

    if lines:
        rows = [{
            "ID":          l.id,
            "Parent (Assembly)": items[l.parent_item_id].part_number if l.parent_item_id in items else "?",
            "Child (Component)": items[l.child_item_id].part_number  if l.child_item_id  in items else "?",
            "Qty per Assembly":  l.qty,
            "Note":              l.note or "",
        } for l in lines]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No BOM lines yet. Add lines below.")

    st.divider()
    st.subheader("Add BOM Line")

    if not items:
        st.warning("Add items in **Material Master** first.")
        return

    item_options = {f"{it.part_number} — {it.description}": iid
                    for iid, it in items.items()}
    labels = list(item_options.keys())

    col1, col2, col3 = st.columns(3)
    with col1:
        parent_lbl = st.selectbox("Parent (Assembly)", labels, key="bom_parent")
    with col2:
        child_lbl  = st.selectbox("Child (Component)", labels, key="bom_child")
    with col3:
        qty = st.number_input("Qty per assembly", min_value=0.001, value=1.0,
                              step=0.5, key="bom_qty")
    note = st.text_input("Note (optional)", key="bom_note")

    if st.button("➕ Add BOM Line", type="primary"):
        pid = item_options[parent_lbl]
        cid = item_options[child_lbl]
        if pid == cid:
            st.error("Parent and child must be different items.")
        else:
            session = get_session()
            try:
                # Prevent exact duplicate
                exists = session.query(BomLine).filter_by(
                    parent_item_id=pid, child_item_id=cid).first()
                if exists:
                    st.warning("This BOM line already exists.")
                else:
                    session.add(BomLine(parent_item_id=pid, child_item_id=cid,
                                        qty=qty, note=note))
                    session.commit()
                    st.success("BOM line added.")
                    st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
                session.rollback()
            finally:
                session.close()

    # Delete
    if lines:
        st.divider()
        st.subheader("Delete BOM Line")
        line_opts = {f"#{l.id} {items.get(l.parent_item_id,l).part_number if hasattr(items.get(l.parent_item_id), 'part_number') else '?'} → {items.get(l.child_item_id,l).part_number if hasattr(items.get(l.child_item_id), 'part_number') else '?'}": l.id
                     for l in lines}
        sel = st.selectbox("Select line to delete", list(line_opts.keys()), key="bom_del_sel")
        if st.button("🗑️ Delete", type="secondary", key="bom_del_btn"):
            session = get_session()
            try:
                obj = session.query(BomLine).get(line_opts[sel])
                if obj:
                    session.delete(obj)
                    session.commit()
                    st.success("Deleted.")
                    st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
                session.rollback()
            finally:
                session.close()


# ---------------------------------------------------------------------------
# Tab 2 — Computed DLT table
# ---------------------------------------------------------------------------

def _dlt_table():
    st.subheader("Computed DLT vs Manual DLT")
    st.caption(
        "**Computed DLT** = longest unprotected upstream chain. "
        "Where no BOM lines exist, Computed DLT = Manual DLT. "
        "A difference flags items whose manual DLT may be wrong."
    )

    results = compute_all_dlt()
    if not results:
        st.info("No items found.")
        return

    rows = [{
        "Part Number":    r.part_number,
        "Manual DLT":     r.manual_dlt,
        "Computed DLT":   round(r.computed_dlt, 2),
        "Δ (days)":       round(r.computed_dlt - r.manual_dlt, 2),
        "Buffered":       "✅" if r.is_buffered else "—",
        "Critical Path":  " → ".join(r.critical_path),
    } for r in results]

    df = pd.DataFrame(rows)

    def _style(row):
        delta = row["Δ (days)"]
        if delta > 1:
            return ["background-color: #FDEBD0; color: #1A1A1A"] * len(row)
        if delta < -1:
            return ["background-color: #D6EAF8; color: #1A1A1A"] * len(row)
        return [""] * len(row)

    st.dataframe(df.style.apply(_style, axis=1), use_container_width=True, hide_index=True)
    st.caption("Orange = Computed > Manual (manual DLT is understated). Blue = Computed < Manual.")

    # Offer to sync computed DLT back to item records
    st.divider()
    if st.button("🔄 Apply Computed DLT → Item Records", type="primary",
                 help="Overwrites each item's DLT with the BOM-computed value (only where BOM lines exist)."):
        session = get_session()
        updated = 0
        try:
            for r in results:
                if abs(r.computed_dlt - r.manual_dlt) > 0.001:
                    item = session.query(Item).get(r.item_id)
                    if item:
                        item.dlt = r.computed_dlt
                        updated += 1
            session.commit()
            st.success(f"Updated DLT for {updated} item(s). Re-run buffer calculations to apply.")
        except Exception as e:
            st.error(f"Error: {e}")
            session.rollback()
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Tab 3 — BOM Graph
# ---------------------------------------------------------------------------

def _bom_graph():
    st.subheader("BOM Structure Map")

    session = get_session()
    try:
        lines  = session.query(BomLine).all()
        items  = {it.id: it for it in session.query(Item).all()}
        bufs   = {b.item_id for b in session.query(Buffer).all()}
    finally:
        session.close()

    if not lines:
        st.info("No BOM lines defined yet.")
        return

    # Build NetworkX graph for layout
    import networkx as nx
    G = nx.DiGraph()
    for it in items.values():
        G.add_node(it.id, label=it.part_number, buffered=(it.id in bufs))
    for l in lines:
        G.add_edge(l.child_item_id, l.parent_item_id, qty=l.qty)

    try:
        pos = nx.nx_agraph.graphviz_layout(G, prog="dot")
    except Exception:
        pos = nx.spring_layout(G, seed=42)

    # Plotly figure
    edge_x, edge_y = [], []
    for u, v in G.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]

    node_x = [pos[n][0] for n in G.nodes()]
    node_y = [pos[n][1] for n in G.nodes()]
    node_color = ["#3498DB" if G.nodes[n]["buffered"] else "#ECF0F1" for n in G.nodes()]
    node_labels = [G.nodes[n]["label"] for n in G.nodes()]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=edge_x, y=edge_y, mode="lines",
                             line=dict(color="#BDC3C7", width=1.5), hoverinfo="none"))
    fig.add_trace(go.Scatter(
        x=node_x, y=node_y, mode="markers+text",
        text=node_labels, textposition="top center",
        marker=dict(size=20, color=node_color,
                    line=dict(color="#2C3E50", width=1.5)),
        hovertext=node_labels, hoverinfo="text",
    ))
    fig.update_layout(
        height=500,
        showlegend=False,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        margin=dict(l=20, r=20, t=20, b=20),
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True)
    st.caption("🔵 Blue nodes = buffered (decoupling point). White = non-buffered.")
