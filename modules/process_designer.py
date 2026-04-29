"""
Manufacturing Process Designer — Streamlit page.
Users define a process flow (sequence of operations/materials),
link items to nodes, and mark which nodes have a DDMRP buffer (decoupling points).

Two key behaviours (per DDMRP methodology):
  1. Marking a node as "has_buffer = True" auto-creates the Buffer row for the
     linked item, making it a real decoupling point in the buffer engine.
  2. The process map is visualised as a strict top-down tree (Sugiyama-style
     hierarchical layout) so the manufacturing flow direction is always clear.
     Edges carry the item's DLT so you can see cumulative lead-time per path.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import networkx as nx
from datetime import datetime

from database.db import get_session, Item, Buffer, Process, ProcessNode, ProcessEdge
from modules.importer import render_import_widget, build_process_template, import_process_nodes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def show():
    st.header("Manufacturing Process Designer")
    st.caption(
        "Define your manufacturing flow, assign items to each step, "
        "and mark **decoupling points** where DDMRP buffers will be placed. "
        "Marking a node as a buffer **automatically creates the Buffer record** "
        "for the linked item."
    )

    render_import_widget(
        label="Process Nodes",
        template_fn=build_process_template,
        import_fn=import_process_nodes,
        template_filename="DDMRP_ProcessNodes_Template.xlsx",
        key="process_nodes",
    )

    tab_manage, tab_design, tab_view = st.tabs([
        "Manage Processes", "Design Process", "Process Map",
    ])

    with tab_manage:
        _manage_processes()

    with tab_design:
        _design_process()

    with tab_view:
        _view_process_map()


# ---------------------------------------------------------------------------
# Buffer auto-creation helper
# ---------------------------------------------------------------------------

def _ensure_buffer_for_item(item_id: int) -> bool:
    """
    Create a Buffer row for item_id if one doesn't exist yet.
    Returns True if a new Buffer was created, False if it already existed.
    """
    session = get_session()
    try:
        existing = session.query(Buffer).filter_by(item_id=item_id).first()
        if existing:
            return False
        session.add(Buffer(item_id=item_id, last_calculated=datetime.utcnow()))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def _remove_buffer_for_item(item_id: int):
    """Remove the Buffer row for item_id (when has_buffer is un-ticked)."""
    session = get_session()
    try:
        buf = session.query(Buffer).filter_by(item_id=item_id).first()
        if buf:
            session.delete(buf)
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Manage Processes (create / delete)
# ---------------------------------------------------------------------------

def _manage_processes():
    st.subheader("Processes")

    session = get_session()
    try:
        processes = session.query(Process).order_by(Process.name).all()
        process_data = [{"ID": p.id, "Name": p.name, "Description": p.description,
                         "Nodes": len(p.nodes)} for p in processes]
    finally:
        session.close()

    if process_data:
        st.dataframe(pd.DataFrame(process_data), use_container_width=True, hide_index=True)
    else:
        st.info("No processes yet. Create one below.")

    st.divider()
    st.subheader("Create New Process")
    with st.form("new_process_form", clear_on_submit=True):
        name = st.text_input("Process Name *", placeholder="e.g. Assembly Line A")
        description = st.text_area("Description", height=80)
        submitted = st.form_submit_button("Create Process", type="primary")

    if submitted:
        if not name.strip():
            st.error("Process name is required.")
            return
        session = get_session()
        try:
            proc = Process(name=name.strip(), description=description.strip())
            session.add(proc)
            session.commit()
            st.success(f"Process **{name}** created.")
            st.rerun()
        finally:
            session.close()

    # Delete
    session = get_session()
    try:
        processes = session.query(Process).order_by(Process.name).all()
        del_options = {f"{p.id} — {p.name}": p.id for p in processes}
    finally:
        session.close()

    if del_options:
        st.divider()
        st.subheader("Delete Process")
        del_label = st.selectbox("Select process to delete", list(del_options.keys()),
                                 key="del_proc_sel")
        if st.button("Delete Process", type="secondary"):
            session = get_session()
            try:
                p = session.query(Process).get(del_options[del_label])
                if p:
                    session.delete(p)
                    session.commit()
                    st.success("Process deleted.")
                    st.rerun()
            finally:
                session.close()


# ---------------------------------------------------------------------------
# Design Process: add nodes and edges
# ---------------------------------------------------------------------------

def _design_process():
    session = get_session()
    try:
        processes = session.query(Process).order_by(Process.name).all()
        proc_options = {f"{p.id} — {p.name}": p.id for p in processes}
    finally:
        session.close()

    if not proc_options:
        st.info("Create a process first in the **Manage Processes** tab.")
        return

    selected_label = st.selectbox("Select Process to Edit", list(proc_options.keys()),
                                  key="design_proc_sel")
    process_id = proc_options[selected_label]

    session = get_session()
    try:
        proc = session.query(Process).get(process_id)
        nodes = sorted(proc.nodes, key=lambda n: n.sequence)
        node_list = [{"ID": n.id, "Seq": n.sequence, "Label": n.label,
                      "Type": n.node_type,
                      "Buffer": "✅ YES" if n.has_buffer else "—",
                      "Item": n.item.part_number if n.item else "—"} for n in nodes]
        edges = proc.edges
        edge_list = [{"Edge ID": e.id, "From": e.source.label, "To": e.target.label}
                     for e in edges]
    finally:
        session.close()

    col_nodes, col_edges = st.columns(2)

    with col_nodes:
        st.subheader("Nodes (Process Steps)")
        if node_list:
            st.dataframe(pd.DataFrame(node_list), use_container_width=True, hide_index=True)
        else:
            st.info("No nodes yet.")

        st.divider()
        st.markdown("**Add Node**")

        session = get_session()
        try:
            items = session.query(Item).order_by(Item.part_number).all()
            item_options = {"— None —": None}
            item_options.update({f"{it.part_number} — {it.description}": it.id
                                  for it in items})
        finally:
            session.close()

        with st.form(f"add_node_{process_id}", clear_on_submit=True):
            label      = st.text_input("Node Label *", placeholder="e.g. Cut & Weld")
            node_type  = st.selectbox("Node Type",
                                      ["operation", "material", "buffer"],
                                      help="operation = process step; material = item/component; "
                                           "buffer = explicit buffer marker")
            item_label = st.selectbox("Linked Item (optional)", list(item_options.keys()))
            has_buffer = st.checkbox(
                "Place DDMRP Buffer here (decoupling point)",
                help="Ticking this will automatically create a Buffer record for the "
                     "linked item, activating it as a decoupling point in the buffer engine.",
            )
            sequence   = st.number_input("Sequence (order in process)",
                                         min_value=0, value=len(node_list), step=1)
            add_node   = st.form_submit_button("Add Node", type="primary")

        if add_node:
            if not label.strip():
                st.error("Label is required.")
            else:
                item_id = item_options[item_label]
                session = get_session()
                try:
                    node = ProcessNode(
                        process_id=process_id,
                        item_id=item_id,
                        label=label.strip(),
                        node_type=node_type,
                        has_buffer=has_buffer,
                        sequence=sequence,
                    )
                    session.add(node)
                    session.commit()

                    # ── Auto-create Buffer row when decoupling point is marked ──
                    if has_buffer and item_id:
                        created = _ensure_buffer_for_item(item_id)
                        if created:
                            st.info(
                                f"✅ Buffer record created for **{item_label.split(' — ')[0]}**. "
                                "Run **Replenishment Signals** to calculate zone sizes."
                            )
                        else:
                            st.info(
                                f"Buffer record already exists for **{item_label.split(' — ')[0]}**."
                            )
                    st.success(f"Node **{label}** added.")
                    st.rerun()
                finally:
                    session.close()

        # ── Toggle has_buffer on existing nodes ──
        if node_list:
            st.divider()
            st.markdown("**Toggle Buffer / Remove Node**")

            session = get_session()
            try:
                nodes_raw = (session.query(ProcessNode)
                             .filter_by(process_id=process_id)
                             .order_by(ProcessNode.sequence).all())
                node_del_opts = {
                    f"{n.sequence}: {n.label} ({'Buffer' if n.has_buffer else 'No buffer'})": n.id
                    for n in nodes_raw
                }
            finally:
                session.close()

            sel_node_label = st.selectbox("Select node", list(node_del_opts.keys()),
                                          key="sel_node_action")
            sel_node_id = node_del_opts[sel_node_label]

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("🔄 Toggle Buffer", key="toggle_buf_btn"):
                    session = get_session()
                    try:
                        n = session.query(ProcessNode).get(sel_node_id)
                        n.has_buffer = not n.has_buffer
                        session.commit()
                        if n.has_buffer and n.item_id:
                            created = _ensure_buffer_for_item(n.item_id)
                            msg = "created" if created else "already existed"
                            st.success(
                                f"Buffer {'enabled' if n.has_buffer else 'disabled'} "
                                f"on **{n.label}**. Buffer record {msg}."
                            )
                        elif not n.has_buffer and n.item_id:
                            st.info(
                                f"Buffer disabled on **{n.label}**. "
                                "The Buffer record is kept — delete it manually in Material Master "
                                "if this item should no longer be a decoupling point."
                            )
                        else:
                            st.success(f"Buffer toggled on **{n.label}**.")
                        st.rerun()
                    finally:
                        session.close()
            with col_b:
                if st.button("🗑️ Remove Node", key="del_node_btn"):
                    session = get_session()
                    try:
                        n = session.query(ProcessNode).get(sel_node_id)
                        session.delete(n)
                        session.commit()
                        st.success("Node removed.")
                        st.rerun()
                    finally:
                        session.close()

    with col_edges:
        st.subheader("Connections (Edges)")
        if edge_list:
            st.dataframe(pd.DataFrame(edge_list), use_container_width=True, hide_index=True)
        else:
            st.info("No connections yet.")

        st.divider()
        st.markdown("**Add Connection**")

        session = get_session()
        try:
            nodes_raw = (session.query(ProcessNode)
                         .filter_by(process_id=process_id)
                         .order_by(ProcessNode.sequence).all())
            node_opts = {f"{n.sequence}: {n.label}": n.id for n in nodes_raw}
        finally:
            session.close()

        if len(node_opts) >= 2:
            with st.form(f"add_edge_{process_id}", clear_on_submit=True):
                from_node = st.selectbox("From Node (upstream)", list(node_opts.keys()),
                                         key="from_node")
                to_node   = st.selectbox("To Node (downstream)", list(node_opts.keys()),
                                         key="to_node")
                add_edge  = st.form_submit_button("Add Connection", type="primary")

            if add_edge:
                src_id = node_opts[from_node]
                tgt_id = node_opts[to_node]
                if src_id == tgt_id:
                    st.error("Source and target must be different nodes.")
                else:
                    session = get_session()
                    try:
                        edge = ProcessEdge(process_id=process_id,
                                           source_id=src_id, target_id=tgt_id)
                        session.add(edge)
                        session.commit()
                        st.success("Connection added.")
                        st.rerun()
                    finally:
                        session.close()

            # Delete edge
            if edge_list:
                session = get_session()
                try:
                    edges_raw = (session.query(ProcessEdge)
                                 .filter_by(process_id=process_id).all())
                    edge_del = {
                        f"#{e.id}: {e.source.label} → {e.target.label}": e.id
                        for e in edges_raw
                    }
                finally:
                    session.close()

                sel_edge = st.selectbox("Delete connection", list(edge_del.keys()),
                                        key="del_edge_sel")
                if st.button("🗑️ Remove Connection", key="del_edge_btn"):
                    session = get_session()
                    try:
                        e = session.query(ProcessEdge).get(edge_del[sel_edge])
                        session.delete(e)
                        session.commit()
                        st.success("Connection removed.")
                        st.rerun()
                    finally:
                        session.close()
        else:
            st.info("Add at least 2 nodes to create connections.")


# ---------------------------------------------------------------------------
# Process Map — hierarchical tree visualisation
# ---------------------------------------------------------------------------

def _view_process_map():
    session = get_session()
    try:
        processes = session.query(Process).order_by(Process.name).all()
        proc_options = {f"{p.id} — {p.name}": p.id for p in processes}
    finally:
        session.close()

    if not proc_options:
        st.info("No processes defined yet.")
        return

    selected_label = st.selectbox("Select Process to View", list(proc_options.keys()),
                                  key="view_proc_sel")
    process_id = proc_options[selected_label]

    session = get_session()
    try:
        proc = session.query(Process).get(process_id)
        nodes = sorted(proc.nodes, key=lambda n: n.sequence)
        node_data = [
            (n.id, n.label, n.node_type, n.has_buffer,
             n.item.part_number if n.item else None,
             n.item.dlt if n.item else 0.0)
            for n in nodes
        ]
        edge_data = [(e.source_id, e.target_id) for e in proc.edges]
    finally:
        session.close()

    if not node_data:
        st.info("No nodes in this process. Design it first.")
        return

    show_dlt  = st.checkbox("Show DLT labels on edges", value=True, key="show_dlt")
    show_item = st.checkbox("Show item codes on nodes",  value=True, key="show_item")

    fig = _build_tree_graph(node_data, edge_data, show_dlt=show_dlt, show_item=show_item)
    st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "🟦 Operation  |  🟩 Buffer / decoupling point  |  🟨 Material  "
        "— Flow runs **top → bottom**. Edge numbers = item DLT (days)."
    )

    # Buffer summary table
    buf_nodes = [(label, part, dlt) for _, label, _, has_buf, part, dlt in node_data if has_buf]
    if buf_nodes:
        st.divider()
        st.subheader("Decoupling Points in this Process")
        st.dataframe(pd.DataFrame(buf_nodes, columns=["Node", "Part Number", "Item DLT (days)"]),
                     use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Tree layout engine (pure Python — no graphviz dependency)
# ---------------------------------------------------------------------------

def _hierarchical_layout(G: nx.DiGraph) -> dict:
    """
    Compute (x, y) positions for a tree/DAG using a top-down Sugiyama-style
    level assignment:
      - Level 0 = source nodes (no incoming edges)
      - Level k = max level of any predecessor + 1
      - Within each level, nodes are spaced equally on the x-axis
      - y = -level  (so level-0 is at top, leaves at bottom)

    For disconnected nodes (no edges at all), they are placed at level 0.
    """
    if not G.nodes:
        return {}

    # Compute levels via longest-path from any source
    levels: dict[int, int] = {}
    for node in nx.topological_sort(G):
        preds = list(G.predecessors(node))
        if not preds:
            levels[node] = 0
        else:
            levels[node] = max(levels.get(p, 0) for p in preds) + 1

    # Group nodes by level
    from collections import defaultdict
    level_groups: dict[int, list] = defaultdict(list)
    for node, lvl in levels.items():
        level_groups[lvl].append(node)

    # Assign x positions: within each level, space nodes at x = 0, 1, 2, ...
    # Centre each level on x=0 for a symmetric look
    pos = {}
    y_gap = 2.0
    x_gap = 2.5
    for lvl, group in level_groups.items():
        n = len(group)
        for i, node in enumerate(sorted(group)):   # sort for determinism
            x = (i - (n - 1) / 2.0) * x_gap
            y = -lvl * y_gap
            pos[node] = (x, y)

    return pos


def _build_tree_graph(node_data, edge_data, show_dlt=True, show_item=True):
    """
    Build a Plotly figure of the process as a top-down tree.

    node_data: list of (id, label, ntype, has_buffer, part_number, dlt)
    edge_data: list of (src_id, tgt_id)
    """
    G = nx.DiGraph()
    node_meta = {}
    for nid, label, ntype, has_buf, part, dlt in node_data:
        G.add_node(nid)
        node_meta[nid] = dict(label=label, ntype=ntype,
                               has_buffer=has_buf, part=part, dlt=dlt or 0.0)
    for src, tgt in edge_data:
        if src in node_meta and tgt in node_meta:
            G.add_edge(src, tgt)

    # Handle cycles gracefully (shouldn't happen in valid process, but be safe)
    try:
        pos = _hierarchical_layout(G)
    except nx.NetworkXUnfeasible:
        pos = nx.spring_layout(G, seed=42)

    # ── Node colours ─────────────────────────────────────────────────────────
    def _color(ntype, has_buf):
        if has_buf:   return "#27AE60"   # green — buffer / decoupling
        if ntype == "material": return "#F39C12"  # amber — material
        return "#2980B9"                  # blue — operation

    def _border(has_buf):
        return "#1A5E20" if has_buf else "#1A252F"

    node_ids = list(G.nodes)
    node_x   = [pos[n][0] for n in node_ids]
    node_y   = [pos[n][1] for n in node_ids]
    colors   = [_color(node_meta[n]["ntype"], node_meta[n]["has_buffer"]) for n in node_ids]
    borders  = [_border(node_meta[n]["has_buffer"]) for n in node_ids]

    # Node label: show item code below process label if requested
    def _node_text(n):
        m = node_meta[n]
        txt = m["label"]
        if show_item and m["part"]:
            txt += f"<br><i style='font-size:10px'>{m['part']}</i>"
        if m["has_buffer"]:
            txt += "<br>🟩 BUFFER"
        return txt

    node_labels = [_node_text(n) for n in node_ids]

    hover_texts = [
        f"<b>{node_meta[n]['label']}</b>"
        f"{'<br>Part: ' + node_meta[n]['part'] if node_meta[n]['part'] else ''}"
        f"<br>Type: {node_meta[n]['ntype']}"
        f"<br>DLT: {node_meta[n]['dlt']:.1f} d"
        f"{'<br><b>⬛ DECOUPLING POINT</b>' if node_meta[n]['has_buffer'] else ''}"
        for n in node_ids
    ]

    # ── Edge traces with arrows ───────────────────────────────────────────────
    traces = []
    for src, tgt in edge_data:
        if src not in pos or tgt not in pos:
            continue
        x0, y0 = pos[src]
        x1, y1 = pos[tgt]

        # Slightly offset endpoints so arrow doesn't overlap node circle
        dx, dy = x1 - x0, y1 - y0
        length = (dx**2 + dy**2) ** 0.5 or 1.0
        ux, uy = dx / length, dy / length
        offset = 0.35
        x0s, y0s = x0 + ux * offset, y0 + uy * offset
        x1e, y1e = x1 - ux * offset, y1 - uy * offset

        src_is_buf = node_meta[src]["has_buffer"]
        line_color = "#27AE60" if src_is_buf else "#7F8C8D"
        line_dash  = "dot" if src_is_buf else "solid"

        traces.append(go.Scatter(
            x=[x0s, x1e, None], y=[y0s, y1e, None],
            mode="lines",
            line=dict(width=2, color=line_color, dash=line_dash),
            hoverinfo="none",
            showlegend=False,
        ))

        # Arrow head (small marker at target end)
        traces.append(go.Scatter(
            x=[x1e], y=[y1e],
            mode="markers",
            marker=dict(symbol="arrow", size=10, color=line_color,
                        angleref="previous"),
            hoverinfo="none",
            showlegend=False,
        ))

        # DLT label on edge midpoint
        if show_dlt:
            src_dlt = node_meta[src]["dlt"]
            if src_dlt > 0:
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                traces.append(go.Scatter(
                    x=[mx], y=[my],
                    mode="text",
                    text=[f"<b>{src_dlt:.0f}d</b>"],
                    textfont=dict(size=10, color="#555"),
                    hoverinfo="none",
                    showlegend=False,
                ))

    # ── Node trace ───────────────────────────────────────────────────────────
    traces.append(go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        text=node_labels,
        textposition="bottom center",
        textfont=dict(size=11),
        marker=dict(
            size=36,
            color=colors,
            line=dict(width=2.5, color=borders),
            symbol="circle",
        ),
        hovertext=hover_texts,
        hoverinfo="text",
        showlegend=False,
    ))

    # ── Legend entries ────────────────────────────────────────────────────────
    for color, label in [("#2980B9", "Operation"), ("#F39C12", "Material"),
                         ("#27AE60", "Buffer / Decoupling Point")]:
        traces.append(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(size=12, color=color),
            name=label, showlegend=True,
        ))

    all_y = [pos[n][1] for n in node_ids] if pos else [0]
    y_min, y_max = min(all_y) - 1.5, max(all_y) + 1.5

    fig = go.Figure(data=traces)
    fig.update_layout(
        height=max(500, len(set(v[1] for v in pos.values())) * 120 + 100),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False,
                   range=[min(v[0] for v in pos.values()) - 1.5,
                          max(v[0] for v in pos.values()) + 1.5] if pos else [-2, 2]),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False,
                   range=[y_min, y_max]),
        margin=dict(t=30, b=30, l=20, r=20),
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", y=-0.08, x=0.5, xanchor="center"),
    )
    return fig
