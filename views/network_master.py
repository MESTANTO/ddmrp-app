"""
Network Master — manage Locations, Lanes, and per-location demand.

Multi-echelon supply-chain optimization needs an explicit network of physical
nodes (plants, DCs, customers, supplier plants) and the transportation lanes
between them. This page is the master-data entry point used by Session 2
(Multi-Echelon Network Flow), Session 3 (Facility Location), Session 4
(Decoupling Point Placement), and Session 7 (Routing).

Existing data is referenced, not duplicated:
  - Supplier rows can be exposed as Locations of node_type='supplier' via a
    "Seed from Supplier" action. The Location keeps a FK back to the
    Supplier row, so updates to address / lead-time stay in one place.
  - Item rows stay the system of record for ADU / unit_cost. Per-location
    demand splits live in LocationDemand and reference items.id.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from database.db import (
    get_session, Location, Lane, LocationDemand, Supplier, Item,
    Base, engine,
)
from database.auth import get_company_id


def _ensure_tables_exist() -> None:
    """
    Defensive: make sure the network tables exist before any query runs.

    On Streamlit Cloud, init_db() is cached with @st.cache_resource and
    only runs once per worker process. Right after a deploy that adds new
    tables, a worker may still be serving with stale-state, so we re-issue
    create_all() here. The call is idempotent on Postgres + SQLite — no
    cost if the tables already exist.
    """
    try:
        Base.metadata.create_all(engine, tables=[
            Location.__table__, Lane.__table__, LocationDemand.__table__,
        ])
    except Exception:
        # If migrations failed, the query below will raise a clearer error.
        pass


NODE_TYPES = ["plant", "dc", "customer", "supplier"]
LANE_MODES = ["truck", "rail", "ocean", "air", "parcel"]


# ───────────────────────────────────────────────────────────────────────────
# Loaders
# ───────────────────────────────────────────────────────────────────────────

def _load_locations(company_id: int) -> list[Location]:
    sess = get_session()
    try:
        return (sess.query(Location)
                    .filter(Location.company_id == company_id)
                    .order_by(Location.node_type, Location.code)
                    .all())
    finally:
        sess.close()


def _load_lanes(company_id: int) -> list[Lane]:
    sess = get_session()
    try:
        return (sess.query(Lane)
                    .filter(Lane.company_id == company_id)
                    .order_by(Lane.id)
                    .all())
    finally:
        sess.close()


def _location_options(company_id: int, types: list[str] | None = None) -> dict[int, str]:
    sess = get_session()
    try:
        q = sess.query(Location).filter(Location.company_id == company_id,
                                        Location.status == "active")
        if types:
            q = q.filter(Location.node_type.in_(types))
        return {
            loc.id: f"{loc.code} · {loc.name} [{loc.node_type}]"
            for loc in q.order_by(Location.node_type, Location.code).all()
        }
    finally:
        sess.close()


# ───────────────────────────────────────────────────────────────────────────
# View
# ───────────────────────────────────────────────────────────────────────────

def show() -> None:
    _ensure_tables_exist()
    company_id = get_company_id()

    st.title("🌐 Network Master")
    st.caption(
        "Define the physical network used by multi-echelon optimization: "
        "Locations (plants, DCs, customers, supplier plants) and the Lanes "
        "connecting them. Supplier master data is referenced — not duplicated."
    )

    tabs = st.tabs(["📍 Locations", "🛣️ Lanes", "📦 Per-Location Demand"])

    with tabs[0]:
        _render_locations_tab(company_id)
    with tabs[1]:
        _render_lanes_tab(company_id)
    with tabs[2]:
        _render_demand_tab(company_id)


# ───────────────────────────────────────────────────────────────────────────
# Tab: Locations
# ───────────────────────────────────────────────────────────────────────────

def _render_locations_tab(company_id: int) -> None:
    locs = _load_locations(company_id)

    c_a, c_b, c_c, c_d = st.columns(4)
    c_a.metric("Plants",    sum(1 for l in locs if l.node_type == "plant"))
    c_b.metric("DCs",       sum(1 for l in locs if l.node_type == "dc"))
    c_c.metric("Customers", sum(1 for l in locs if l.node_type == "customer"))
    c_d.metric("Suppliers", sum(1 for l in locs if l.node_type == "supplier"))

    st.divider()

    # ── Quick seed from existing suppliers ─────────────────────────────────
    with st.expander("⚡ Quick seed from existing Supplier master"):
        st.markdown(
            "Promote selected suppliers to Locations of `node_type='supplier'`. "
            "Code, name, country, city are copied; a FK back to the supplier "
            "row is kept so contact data stays in one place."
        )
        sess = get_session()
        try:
            seeded_supplier_ids = {
                loc.supplier_id for loc in locs if loc.supplier_id is not None
            }
            available_sups = (
                sess.query(Supplier)
                    .filter(Supplier.company_id == company_id)
                    .filter(~Supplier.id.in_(seeded_supplier_ids) if seeded_supplier_ids else Supplier.id.isnot(None))
                    .all()
            )
        finally:
            sess.close()

        if not available_sups:
            st.info("All active suppliers are already mirrored as Locations.")
        else:
            sup_labels = {s.id: f"{s.code} · {s.name}" for s in available_sups}
            picked = st.multiselect("Suppliers to mirror as Locations",
                                    options=list(sup_labels.keys()),
                                    format_func=lambda x: sup_labels[x])
            if st.button("Seed Locations", type="primary", disabled=not picked):
                sess = get_session()
                try:
                    for sid in picked:
                        s = sess.query(Supplier).get(sid)
                        if s is None:
                            continue
                        sess.add(Location(
                            company_id=company_id,
                            code=f"L-{s.code}",
                            name=s.name,
                            node_type="supplier",
                            country=s.country or "",
                            city=s.city or "",
                            address=s.address or "",
                            supplier_id=s.id,
                            status="active",
                            notes=f"Auto-seeded from Supplier {s.code}",
                        ))
                    sess.commit()
                    st.success(f"Seeded {len(picked)} Location(s).")
                    st.rerun()
                except Exception as exc:
                    sess.rollback()
                    st.error(f"Seed failed: {exc}")
                finally:
                    sess.close()

    # ── Add / edit form ────────────────────────────────────────────────────
    st.subheader("Add / Edit Location")
    edit_id = st.session_state.get("nm_edit_location_id")
    edit_loc = None
    if edit_id:
        sess = get_session()
        try:
            edit_loc = sess.query(Location).filter_by(
                id=edit_id, company_id=company_id).first()
        finally:
            sess.close()

    with st.form("loc_form", clear_on_submit=(edit_loc is None)):
        c1, c2 = st.columns(2)
        with c1:
            code = st.text_input("Code *", value=edit_loc.code if edit_loc else "")
            name = st.text_input("Name *", value=edit_loc.name if edit_loc else "")
            node_type = st.selectbox(
                "Node type *", NODE_TYPES,
                index=NODE_TYPES.index(edit_loc.node_type) if edit_loc else 0,
            )
            country = st.text_input("Country", value=edit_loc.country if edit_loc else "")
            city    = st.text_input("City",    value=edit_loc.city    if edit_loc else "")
        with c2:
            throughput = st.number_input(
                "Throughput capacity (units/day, 0 = ∞)",
                min_value=0.0,
                value=float(edit_loc.throughput_capacity) if edit_loc else 0.0,
                step=100.0,
            )
            storage = st.number_input(
                "Storage capacity (units, 0 = ∞)",
                min_value=0.0,
                value=float(edit_loc.storage_capacity) if edit_loc else 0.0,
                step=100.0,
            )
            fixed_open = st.number_input(
                "Fixed open cost (€/period) — Ch7 CFLP",
                min_value=0.0,
                value=float(edit_loc.fixed_open_cost) if edit_loc else 0.0,
                step=500.0,
            )
            holding = st.number_input(
                "Holding cost (€/unit/day) — IRP/SSPP",
                min_value=0.0,
                value=float(edit_loc.holding_cost_per_unit_day) if edit_loc else 0.0,
                step=0.01, format="%.4f",
            )
            status = st.selectbox(
                "Status", ["active", "inactive"],
                index=0 if (not edit_loc or edit_loc.status == "active") else 1,
            )
        address = st.text_area("Address", value=edit_loc.address if edit_loc else "", height=68)
        notes   = st.text_area("Notes",   value=edit_loc.notes   if edit_loc else "", height=68)

        cols = st.columns([1, 1, 4])
        with cols[0]:
            submit = st.form_submit_button("💾 Save", type="primary",
                                           use_container_width=True)
        with cols[1]:
            cancel = st.form_submit_button("Cancel", use_container_width=True) if edit_loc else False

        if submit:
            if not code.strip() or not name.strip():
                st.error("Code and Name are required.")
            else:
                sess = get_session()
                try:
                    if edit_loc:
                        loc = sess.query(Location).get(edit_loc.id)
                    else:
                        loc = Location(company_id=company_id)
                        sess.add(loc)
                    loc.code = code.strip()
                    loc.name = name.strip()
                    loc.node_type = node_type
                    loc.country = country.strip()
                    loc.city = city.strip()
                    loc.address = address
                    loc.throughput_capacity = throughput
                    loc.storage_capacity = storage
                    loc.fixed_open_cost = fixed_open
                    loc.holding_cost_per_unit_day = holding
                    loc.status = status
                    loc.notes = notes
                    sess.commit()
                    st.session_state.nm_edit_location_id = None
                    st.success(f"Location {code} saved.")
                    st.rerun()
                except Exception as exc:
                    sess.rollback()
                    st.error(f"Save failed: {exc}")
                finally:
                    sess.close()
        if cancel:
            st.session_state.nm_edit_location_id = None
            st.rerun()

    # ── List ───────────────────────────────────────────────────────────────
    st.subheader(f"Locations ({len(locs)})")
    if not locs:
        st.info("No locations yet. Add one above or seed from suppliers.")
        return

    rows = [{
        "ID": l.id,
        "Code": l.code,
        "Name": l.name,
        "Type": l.node_type,
        "Country": l.country,
        "City": l.city,
        "Throughput/day": l.throughput_capacity,
        "Storage": l.storage_capacity,
        "Fixed open cost €": l.fixed_open_cost,
        "Holding €/u/d": l.holding_cost_per_unit_day,
        "Status": l.status,
    } for l in locs]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=320)

    with st.expander("✏️ Edit / 🗑️ Delete a location"):
        loc_map = {l.id: f"{l.code} · {l.name} [{l.node_type}]" for l in locs}
        pick = st.selectbox("Pick location",
                            options=list(loc_map.keys()),
                            format_func=lambda x: loc_map[x])
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✏️ Load into form", use_container_width=True):
                st.session_state.nm_edit_location_id = pick
                st.rerun()
        with c2:
            if st.button("🗑️ Delete", use_container_width=True, type="secondary"):
                sess = get_session()
                try:
                    # Block delete if lanes reference it
                    in_use = (sess.query(Lane)
                                  .filter((Lane.origin_id == pick) |
                                          (Lane.dest_id   == pick))
                                  .count())
                    if in_use:
                        st.error(f"Cannot delete — {in_use} lane(s) use this location.")
                    else:
                        sess.query(LocationDemand).filter_by(location_id=pick).delete()
                        sess.query(Location).filter_by(id=pick).delete()
                        sess.commit()
                        st.success("Deleted.")
                        st.rerun()
                except Exception as exc:
                    sess.rollback()
                    st.error(f"Delete failed: {exc}")
                finally:
                    sess.close()


# ───────────────────────────────────────────────────────────────────────────
# Tab: Lanes
# ───────────────────────────────────────────────────────────────────────────

def _render_lanes_tab(company_id: int) -> None:
    lanes = _load_lanes(company_id)
    loc_opts = _location_options(company_id)

    if not loc_opts:
        st.warning("Add Locations first before defining Lanes.")
        return

    st.subheader("Add / Edit Lane")
    edit_id = st.session_state.get("nm_edit_lane_id")
    edit_lane = None
    if edit_id:
        sess = get_session()
        try:
            edit_lane = sess.query(Lane).filter_by(
                id=edit_id, company_id=company_id).first()
        finally:
            sess.close()

    with st.form("lane_form", clear_on_submit=(edit_lane is None)):
        c1, c2 = st.columns(2)
        with c1:
            origin = st.selectbox(
                "Origin *", options=list(loc_opts.keys()),
                format_func=lambda x: loc_opts.get(x, "—"),
                index=list(loc_opts.keys()).index(edit_lane.origin_id) if edit_lane and edit_lane.origin_id in loc_opts else 0,
            )
            dest = st.selectbox(
                "Destination *", options=list(loc_opts.keys()),
                format_func=lambda x: loc_opts.get(x, "—"),
                index=list(loc_opts.keys()).index(edit_lane.dest_id) if edit_lane and edit_lane.dest_id in loc_opts else 0,
            )
            mode = st.selectbox(
                "Mode", LANE_MODES,
                index=LANE_MODES.index(edit_lane.mode) if edit_lane and edit_lane.mode in LANE_MODES else 0,
            )
        with c2:
            cost = st.number_input(
                "Cost per unit (€)",
                min_value=0.0,
                value=float(edit_lane.cost_per_unit) if edit_lane else 0.0,
                step=0.01, format="%.4f",
            )
            ltd  = st.number_input(
                "Lead time (days)",
                min_value=0, value=int(edit_lane.lead_time_days) if edit_lane else 0,
            )
            cap = st.number_input(
                "Capacity (units / period, 0 = ∞)",
                min_value=0.0,
                value=float(edit_lane.capacity_units) if edit_lane else 0.0,
                step=100.0,
            )
            status = st.selectbox(
                "Status", ["active", "inactive"],
                index=0 if (not edit_lane or edit_lane.status == "active") else 1,
            )
        notes = st.text_area("Notes", value=edit_lane.notes if edit_lane else "", height=68)

        cols = st.columns([1, 1, 4])
        with cols[0]:
            submit = st.form_submit_button("💾 Save", type="primary",
                                           use_container_width=True)
        with cols[1]:
            cancel = st.form_submit_button("Cancel", use_container_width=True) if edit_lane else False

        if submit:
            if origin == dest:
                st.error("Origin and Destination must differ.")
            else:
                sess = get_session()
                try:
                    if edit_lane:
                        lane = sess.query(Lane).get(edit_lane.id)
                    else:
                        lane = Lane(company_id=company_id)
                        sess.add(lane)
                    lane.origin_id = origin
                    lane.dest_id = dest
                    lane.mode = mode
                    lane.cost_per_unit = cost
                    lane.lead_time_days = int(ltd)
                    lane.capacity_units = cap
                    lane.status = status
                    lane.notes = notes
                    sess.commit()
                    st.session_state.nm_edit_lane_id = None
                    st.success("Lane saved.")
                    st.rerun()
                except Exception as exc:
                    sess.rollback()
                    st.error(f"Save failed: {exc}")
                finally:
                    sess.close()
        if cancel:
            st.session_state.nm_edit_lane_id = None
            st.rerun()

    st.subheader(f"Lanes ({len(lanes)})")
    if not lanes:
        st.info("No lanes yet.")
        return

    rows = []
    for ln in lanes:
        rows.append({
            "ID": ln.id,
            "Origin": loc_opts.get(ln.origin_id, f"#{ln.origin_id}"),
            "Destination": loc_opts.get(ln.dest_id, f"#{ln.dest_id}"),
            "Mode": ln.mode,
            "Cost €/u": ln.cost_per_unit,
            "Lead-time": ln.lead_time_days,
            "Capacity": ln.capacity_units if ln.capacity_units else "∞",
            "Status": ln.status,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=320)

    with st.expander("✏️ Edit / 🗑️ Delete a lane"):
        lane_map = {
            ln.id: f"#{ln.id} · {loc_opts.get(ln.origin_id, '?')} → {loc_opts.get(ln.dest_id, '?')}"
            for ln in lanes
        }
        pick = st.selectbox("Pick lane",
                            options=list(lane_map.keys()),
                            format_func=lambda x: lane_map[x])
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✏️ Load into form", key="ln_edit_btn", use_container_width=True):
                st.session_state.nm_edit_lane_id = pick
                st.rerun()
        with c2:
            if st.button("🗑️ Delete", key="ln_del_btn", use_container_width=True, type="secondary"):
                sess = get_session()
                try:
                    sess.query(Lane).filter_by(id=pick).delete()
                    sess.commit()
                    st.success("Deleted.")
                    st.rerun()
                except Exception as exc:
                    sess.rollback()
                    st.error(f"Delete failed: {exc}")
                finally:
                    sess.close()


# ───────────────────────────────────────────────────────────────────────────
# Tab: Per-Location Demand
# ───────────────────────────────────────────────────────────────────────────

def _render_demand_tab(company_id: int) -> None:
    st.markdown(
        "Split each item's aggregate ADU across customer Locations. If you "
        "leave an item without rows here, the multi-echelon solver falls back "
        "to a single virtual customer carrying `items.adu × horizon`."
    )

    customer_opts = _location_options(company_id, types=["customer"])
    if not customer_opts:
        st.warning("Add at least one customer Location first.")
        return

    sess = get_session()
    try:
        items = (sess.query(Item)
                     .filter(Item.company_id == company_id)
                     .order_by(Item.part_number)
                     .all())
        demands = (sess.query(LocationDemand)
                       .filter(LocationDemand.company_id == company_id)
                       .all())
    finally:
        sess.close()

    if not items:
        st.info("No items in master data.")
        return

    item_opts = {i.id: f"{i.part_number} · {i.description}" for i in items}

    st.subheader("Add / Update Demand Split")
    with st.form("dem_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([3, 3, 2])
        with c1:
            pick_item = st.selectbox("Item *", options=list(item_opts.keys()),
                                     format_func=lambda x: item_opts[x])
        with c2:
            pick_loc = st.selectbox("Customer Location *", options=list(customer_opts.keys()),
                                    format_func=lambda x: customer_opts[x])
        with c3:
            qty = st.number_input("Monthly demand", min_value=0.0, step=10.0)
        submit = st.form_submit_button("💾 Save", type="primary",
                                       use_container_width=True)
        if submit:
            sess = get_session()
            try:
                existing = (sess.query(LocationDemand)
                                .filter_by(company_id=company_id,
                                          item_id=pick_item,
                                          location_id=pick_loc)
                                .first())
                if existing:
                    existing.monthly_demand = qty
                else:
                    sess.add(LocationDemand(
                        company_id=company_id,
                        item_id=pick_item,
                        location_id=pick_loc,
                        monthly_demand=qty,
                    ))
                sess.commit()
                st.success("Saved.")
                st.rerun()
            except Exception as exc:
                sess.rollback()
                st.error(f"Save failed: {exc}")
            finally:
                sess.close()

    st.subheader(f"Demand entries ({len(demands)})")
    if not demands:
        st.info("No per-location demand entries yet.")
        return

    rows = [{
        "ID": d.id,
        "Item": item_opts.get(d.item_id, f"#{d.item_id}"),
        "Customer": customer_opts.get(d.location_id, f"#{d.location_id}"),
        "Monthly demand": d.monthly_demand,
    } for d in demands]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=320)

    with st.expander("🗑️ Delete an entry"):
        dem_map = {
            d.id: f"#{d.id} · {item_opts.get(d.item_id, '?')} → {customer_opts.get(d.location_id, '?')}: {d.monthly_demand}"
            for d in demands
        }
        pick = st.selectbox("Pick entry", options=list(dem_map.keys()),
                            format_func=lambda x: dem_map[x])
        if st.button("🗑️ Delete", key="dem_del_btn"):
            sess = get_session()
            try:
                sess.query(LocationDemand).filter_by(id=pick).delete()
                sess.commit()
                st.success("Deleted.")
                st.rerun()
            except Exception as exc:
                sess.rollback()
                st.error(f"Delete failed: {exc}")
            finally:
                sess.close()
