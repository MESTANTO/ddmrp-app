"""
Manufacturing Process Designer — Streamlit page.
Users define a process flow (sequence of operations/materials),
link items to nodes, and mark which nodes have a DDMRP buffer (decoupling points).
The flow is stored in the DB and visualised as a network graph.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import networkx as nx
from database.db import get_session, Item, Process, ProcessNode, ProcessEdge
from modules.importer import render_import_widget, build_process_template, import_process_nodes


def show():
    st.header("Manufacturing Process Designer")
    st.caption(
        "Define your manufacturing flow, assign items to each step, "
        "and mark **decoupling points** where DDMRP buffers will be placed."
    )

    render_import_widget(
        label="Process Nodes",
        template_fn=build_process_template,
        import_fn=import_process_nodes,
        template_filename="DDMRP_ProcessNodes_Template.xlsx",
        key="process_nodes",
    )

    tab_manage, tab_design, tab_view = st.tabs(["Manage Processes", "Design Process", "Process Map"])

    with tab_manage:
        _manage_processes()

    with tab_design:
        _design_process()

    with tab_view:
        _view_process_map()


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
                    st.success(f"Process deleted.")
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

    # Show existing nodes
    session = get_session()
    try:
        proc = session.query(Process).get(process_id)
        nodes = sorted(proc.nodes, key=lambda n: n.sequence)
        node_list = [{"ID": n.id, "Seq": n.sequence, "Label": n.label,
                      "Type": n.node_type, "Buffer": "YES" if n.has_buffer else "no",
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
            item_options.update({f"{it.part_number} — {it.description}": it.id for it in items})
        finally:
            session.close()

        with st.form(f"add_node_{process_id}", clear_on_submit=True):
            label = st.text_input("Node Label *", placeholder="e.g. Cut & Weld")
            node_type = st.selectbox("Node Type",
                                     ["operation", "material", "buffer"],
                                     help="operation = process step; material = item/component; buffer = explicit buffer marker")
            item_label = st.selectbox("Linked Item (optional)", list(item_options.keys()))
            has_buffer = st.checkbox("Place DDMRP Buffer here (decoupling point)")
            sequence = st.number_input("Sequence (order in process)", min_value=0, value=len(node_list), step=1)
            add_node = st.form_submit_button("Add Node", type="primary")

        if add_node:
            if not label.strip():
                st.error("Label is required.")
            else:
                session = get_session()
                try:
                    node = ProcessNode(
                        process_id=process_id,
                        item_id=item_options[item_label],
                        label=label.strip(),
                        node_type=node_type,
                        has_buffer=has_buffer,
                        sequence=sequence,
                    )
                    session.add(node)
                    session.commit()
                    st.success(f"Node **{label}** added.")
                    st.rerun()
                finally:
                    session.close()

        # Delete node
        if node_list:
            session = get_session()
            try:
                nodes_raw = session.query(ProcessNode).filter_by(process_id=process_id).all()
                node_del_opts = {f"{n.id} — {n.label}": n.id for n in nodes_raw}
            finally:
                session.close()

            del_node_label = st.selectbox("Delete node", list(node_del_opts.keys()), key="del_node")
            if st.button("Remove Node"):
                session = get_session()
                try:
                    n = session.query(ProcessNode).get(node_del_opts[del_node_label])
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
            nodes_raw = session.query(ProcessNode).filter_by(process_id=process_id)\
                               .order_by(ProcessNode.sequence).all()
            node_opts = {f"{n.sequence}: {n.label}": n.id for n in nodes_raw}
        finally:
            session.close()

        if len(node_opts) >= 2:
            with st.form(f"add_edge_{process_id}", clear_on_submit=True):
                from_node = st.selectbox("From Node", list(node_opts.keys()), key="from_node")
                to_node = st.selectbox("To Node", list(node_opts.keys()), key="to_node")
                add_edge = st.form_submit_button("Add Connection", type="primary")

            if add_edge:
                src_id = node_opts[from_node]
                tgt_id = node_opts[to_node]
                if src_id == tgt_id:
                    st.error("Source and target must be different nodes.")
                else:
                    session = get_session()
                    try:
                        edge = ProcessEdge(process_id=process_id,
                                           source_id=src_id,
                                           target_id=tgt_id)
                        session.add(edge)
                        session.commit()
                        st.success("Connection added.")
                        st.rerun()
                    finally:
                        session.close()
        else:
            st.info("Add at least 2 nodes to create connections.")


# ---------------------------------------------------------------------------
# Process Map visualisation
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
        edges = proc.edges
        node_data = [(n.id, n.label, n.node_type, n.has_buffer,
                      n.item.part_number if n.item else None) for n in nodes]
        edge_data = [(e.source_id, e.target_id) for e in edges]
    finally:
        session.close()

    if not node_data:
        st.info("No nodes in this process. Design it first.")
        return

    fig = _build_process_graph(node_data, edge_data)
    st.plotly_chart(fig, use_container_width=True)

    st.caption("🟦 Operation  |  🟩 Buffer (decoupling point)  |  🟨 Material")


def _build_process_graph(node_data, edge_data):
    G = nx.DiGraph()
    for nid, label, ntype, has_buf, part in node_data:
        G.add_node(nid, label=label, ntype=ntype, has_buffer=has_buf, part=part)
    for src, tgt in edge_data:
        G.add_edge(src, tgt)

    # Layout
    try:
        pos = nx.nx_agraph.graphviz_layout(G, prog="dot")
    except Exception:
        # Fallback to spring layout if graphviz not available
        pos = nx.spring_layout(G, seed=42, k=2)

    node_ids = list(G.nodes)
    node_x = [pos[n][0] for n in node_ids]
    node_y = [pos[n][1] for n in node_ids]

    labels = [G.nodes[n]["label"] for n in node_ids]
    has_buf = [G.nodes[n]["has_buffer"] for n in node_ids]
    ntypes = [G.nodes[n]["ntype"] for n in node_ids]

    def node_color(ntype, has_buffer):
        if has_buffer:
            return "#27AE60"   # green = buffer
        if ntype == "material":
            return "#F39C12"   # yellow
        return "#2980B9"       # blue = operation

    colors = [node_color(t, b) for t, b in zip(ntypes, has_buf)]

    # Edge traces
    edge_traces = []
    for src, tgt in edge_data:
        if src in pos and tgt in pos:
            x0, y0 = pos[src]
            x1, y1 = pos[tgt]
            edge_traces.append(go.Scatter(
                x=[x0, x1, None], y=[y0, y1, None],
                mode="lines",
                line=dict(width=2, color="#7F8C8D"),
                hoverinfo="none",
                showlegend=False,
            ))

    node_trace = go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        text=labels,
        textposition="bottom center",
        marker=dict(size=30, color=colors, line=dict(width=2, color="white")),
        hovertext=[
            f"{G.nodes[n]['label']}<br>Type: {G.nodes[n]['ntype']}"
            f"{'<br>⬛ BUFFER' if G.nodes[n]['has_buffer'] else ''}"
            for n in node_ids
        ],
        hoverinfo="text",
        showlegend=False,
    )

    fig = go.Figure(data=edge_traces + [node_trace])
    fig.update_layout(
        height=500,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        margin=dict(t=20, b=20, l=20, r=20),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    return fig
