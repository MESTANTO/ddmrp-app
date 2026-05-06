"""
Demand & Supply Input module — Streamlit page.
Users log actual/forecast demand and supply orders per item.
Also allows updating on-hand inventory.
"""

import streamlit as st
import pandas as pd
from datetime import datetime, date
from database.db import get_session, Item, DemandEntry, SupplyEntry
from database.auth import get_company_id
from modules.importer import (
    render_import_widget,
    build_demand_template, import_demand,
    build_supply_template, import_supply,
)


def show():
    st.header("Demand & Supply")
    st.caption("Log demand orders, supply orders, and update on-hand inventory.")

    tab_demand, tab_supply, tab_onhand, tab_view = st.tabs([
        "Log Demand", "Log Supply", "Update On-Hand", "View Entries"
    ])

    with tab_demand:
        _log_demand()

    with tab_supply:
        _log_supply()

    with tab_onhand:
        _update_on_hand()

    with tab_view:
        _view_entries()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=120)
def _load_item_options(company_id: int) -> dict:
    """Return {label: item_id} mapping — cached 2 min."""
    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == company_id).order_by(Item.part_number).all()
        return {f"{it.part_number} — {it.description}": it.id for it in items}
    finally:
        session.close()


def _item_selector(key_suffix=""):
    options = _load_item_options(get_company_id())

    if not options:
        st.warning("No items found. Please add items in **Material Master** first.")
        return None, None

    label = st.selectbox("Select Item", list(options.keys()), key=f"item_sel_{key_suffix}")
    return options[label], label.split(" — ")[0]


# ---------------------------------------------------------------------------
# Log Demand
# ---------------------------------------------------------------------------

def _log_demand():
    st.subheader("Log Demand Entry")
    render_import_widget(
        label="Demand",
        template_fn=build_demand_template,
        import_fn=import_demand,
        template_filename="DDMRP_Demand_Template.xlsx",
        key="demand",
    )
    st.divider()
    item_id, part_number = _item_selector("demand")
    if item_id is None:
        return

    with st.form("demand_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            demand_type = st.radio("Demand Type", ["actual", "forecast"],
                                   help="Actual = confirmed orders; Forecast = planned demand.")
            quantity = st.number_input("Quantity *", min_value=0.01, value=10.0, step=1.0)
        with col2:
            demand_date = st.date_input("Demand Date *", value=date.today())
            order_ref = st.text_input("Order Reference", placeholder="e.g. SO-12345")
            notes = st.text_area("Notes", height=80)

        submitted = st.form_submit_button("Add Demand Entry", type="primary")

    if submitted:
        session = get_session()
        try:
            entry = DemandEntry(
                item_id=item_id,
                demand_type=demand_type,
                quantity=quantity,
                demand_date=datetime.combine(demand_date, datetime.min.time()),
                order_reference=order_ref.strip(),
                notes=notes.strip(),
            )
            session.add(entry)
            session.commit()
            st.success(f"Demand entry added for **{part_number}**: {quantity} units on {demand_date}.")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Log Supply
# ---------------------------------------------------------------------------

def _log_supply():
    st.subheader("Log Supply Entry")
    render_import_widget(
        label="Supply",
        template_fn=build_supply_template,
        import_fn=import_supply,
        template_filename="DDMRP_Supply_Template.xlsx",
        key="supply",
    )
    st.divider()
    item_id, part_number = _item_selector("supply")
    if item_id is None:
        return

    with st.form("supply_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            supply_type = st.radio("Supply Type", ["purchase_order", "production_order"],
                                   format_func=lambda x: x.replace("_", " ").title())
            quantity = st.number_input("Quantity *", min_value=0.01, value=50.0, step=1.0)
        with col2:
            due_date = st.date_input("Expected Due Date *", value=date.today())
            order_ref = st.text_input("Order Reference", placeholder="e.g. PO-9876")
            notes = st.text_area("Notes", height=80)

        submitted = st.form_submit_button("Add Supply Entry", type="primary")

    if submitted:
        session = get_session()
        try:
            entry = SupplyEntry(
                item_id=item_id,
                supply_type=supply_type,
                quantity=quantity,
                due_date=datetime.combine(due_date, datetime.min.time()),
                order_reference=order_ref.strip(),
                notes=notes.strip(),
            )
            session.add(entry)
            session.commit()
            st.success(f"Supply entry added for **{part_number}**: {quantity} units due {due_date}.")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Update On-Hand
# ---------------------------------------------------------------------------

def _update_on_hand():
    st.subheader("Update On-Hand Inventory")
    st.caption("Set the current physical stock for each item.")

    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == get_company_id()).order_by(Item.part_number).all()
    finally:
        session.close()

    if not items:
        st.info("No items found.")
        return

    with st.form("onhand_form"):
        updates = {}
        for item in items:
            updates[item.id] = st.number_input(
                f"{item.part_number} — {item.description}",
                value=float(item.on_hand),
                min_value=0.0,
                step=1.0,
                key=f"oh_{item.id}",
            )

        submitted = st.form_submit_button("Save All On-Hand Values", type="primary")

    if submitted:
        session = get_session()
        try:
            for item_id, qty in updates.items():
                it = session.query(Item).get(item_id)
                if it:
                    it.on_hand = qty
            session.commit()
            st.success("On-hand inventory updated for all items.")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# View Entries
# ---------------------------------------------------------------------------

def _view_entries():
    st.subheader("View All Demand & Supply Entries")

    view_type = st.radio("Show", ["Demand", "Supply"], horizontal=True)

    session = get_session()
    try:
        if view_type == "Demand":
            entries = (
                session.query(DemandEntry, Item)
                .join(Item, DemandEntry.item_id == Item.id)
                .order_by(DemandEntry.demand_date.desc())
                .all()
            )
            if not entries:
                st.info("No demand entries yet.")
                return
            rows = [{
                "ID": e.id,
                "Part Number": it.part_number,
                "Description": it.description,
                "Type": e.demand_type,
                "Quantity": e.quantity,
                "Date": e.demand_date.strftime("%Y-%m-%d"),
                "Reference": e.order_reference,
                "Notes": e.notes,
            } for e, it in entries]
        else:
            entries = (
                session.query(SupplyEntry, Item)
                .join(Item, SupplyEntry.item_id == Item.id)
                .order_by(SupplyEntry.due_date.desc())
                .all()
            )
            if not entries:
                st.info("No supply entries yet.")
                return
            rows = [{
                "ID": e.id,
                "Part Number": it.part_number,
                "Description": it.description,
                "Type": e.supply_type.replace("_", " ").title(),
                "Quantity": e.quantity,
                "Due Date": e.due_date.strftime("%Y-%m-%d"),
                "Reference": e.order_reference,
                "Notes": e.notes,
            } for e, it in entries]
    finally:
        session.close()

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} entr{'y' if len(rows)==1 else 'ies'} found.")

    # Delete by ID
    st.divider()
    st.caption("Delete an entry by ID:")
    del_col1, del_col2 = st.columns([2, 1])
    del_id = del_col1.number_input("Entry ID to delete", min_value=1, step=1, value=1)
    if del_col2.button("Delete", type="secondary"):
        session = get_session()
        try:
            model = DemandEntry if view_type == "Demand" else SupplyEntry
            entry = session.query(model).get(int(del_id))
            if entry:
                session.delete(entry)
                session.commit()
                st.success(f"Entry {del_id} deleted.")
                st.rerun()
            else:
                st.error(f"No entry found with ID {del_id}.")
        finally:
            session.close()
