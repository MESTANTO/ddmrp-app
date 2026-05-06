"""
Material Master UI module — Streamlit page.
Allows users to create, view, edit, and delete items with DDMRP parameters.
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from database.db import get_session, Item, BufferProfile, Supplier, DemandEntry
from database.auth import get_company_id
from modules.buffer_engine import calculate_zones
from modules.importer import render_import_widget, build_material_template, import_materials
from modules.param_calculator import (
    calculate_params, calculate_all_params,
    apply_params, apply_all_params, CalcParams,
)


# ---------------------------------------------------------------------------
# DDMRP Buffer Profile bands (deck slides 51-54)
# ---------------------------------------------------------------------------

ITEM_TYPES = ["M", "I", "P", "D"]
ITEM_TYPE_LABELS = {
    "M": "M — Manufactured (finished)",
    "I": "I — Intermediate (semi-finished)",
    "P": "P — Purchased",
    "D": "D — Distributed",
}

LTF_BANDS = {  # (lo_inclusive, hi_inclusive)
    "S": (0.61, 1.00),
    "M": (0.41, 0.60),
    "L": (0.20, 0.40),
}
VF_BANDS = {
    "L": (0.00, 0.40),
    "M": (0.41, 0.60),
    "H": (0.61, 1.00),
}


def _validate_band(value: float, band: tuple, name: str) -> bool:
    lo, hi = band
    if value is None:
        return True
    return lo <= float(value) <= hi


@st.cache_data(ttl=300)
def _load_profiles(company_id: int) -> dict:
    """Return mapping {profile_name: plain dict} — cached 5 min per company."""
    session = get_session()
    try:
        profs = session.query(BufferProfile).filter(
            BufferProfile.company_id == company_id
        ).order_by(BufferProfile.name).all()
        return {
            p.name: {
                "id": p.id,
                "lt_category": p.lt_category,
                "var_category": p.var_category,
                "default_ltf": p.default_ltf,
                "default_vf": p.default_vf,
            }
            for p in profs
        }
    finally:
        session.close()


@st.cache_data(ttl=300)
def _load_suppliers(company_id: int) -> dict:
    """Return mapping {display_label: plain dict} — cached 5 min per company."""
    session = get_session()
    try:
        sups = session.query(Supplier).filter(
            Supplier.company_id == company_id
        ).order_by(Supplier.code).all()
        return {f"{s.code} — {s.name}": {"id": s.id, "code": s.code} for s in sups}
    finally:
        session.close()


def show():
    st.header("Material Master")
    st.caption("Define items and their DDMRP parameters (ADU, Lead Time, Variability, etc.)")

    render_import_widget(
        label="Items",
        template_fn=build_material_template,
        import_fn=import_materials,
        template_filename="DDMRP_Items_Template.xlsx",
        key="material_master",
    )

    tab_list, tab_add, tab_edit, tab_calc = st.tabs([
        "Item List", "Add Item", "Edit / Delete", "🔄 Recalculate Parameters"
    ])

    with tab_list:
        _show_item_list()

    with tab_add:
        _show_add_item()

    with tab_edit:
        _show_edit_item()

    with tab_calc:
        _show_param_calculator()


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def _compute_adu_from_actual(lookback_days: int) -> list[dict]:
    """
    For every item, sum actual demand entries in the past `lookback_days`
    and divide by the period to get a simple historical ADU.
    Returns a list of dicts ready for a DataFrame.
    """
    today_dt = datetime.utcnow()
    since    = today_dt - timedelta(days=lookback_days)

    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == get_company_id()).order_by(Item.part_number).all()
        results = []
        for it in items:
            entries = (
                session.query(DemandEntry)
                .filter(
                    DemandEntry.item_id    == it.id,
                    DemandEntry.demand_type == "actual",
                    DemandEntry.demand_date >= since,
                    DemandEntry.demand_date <= today_dt,
                ).all()
            )
            total_qty   = sum(e.quantity for e in entries)
            demand_days = len({e.demand_date.date() for e in entries})
            calc_adu    = round(total_qty / lookback_days, 4)
            delta_pct   = (
                f"{((calc_adu - it.adu) / it.adu) * 100:+.1f}%"
                if it.adu else "—"
            )
            results.append({
                "item_id":      it.id,
                "Part Number":  it.part_number,
                "Description":  it.description,
                "Current ADU":  it.adu,
                "Calc ADU":     calc_adu,
                "Δ":            delta_pct,
                "Demand Days":  demand_days,
                "Total Qty":    round(total_qty, 2),
            })
    finally:
        session.close()
    return results


def _apply_adu_results(results: list[dict], part_numbers: list[str] | None = None):
    """Write calculated ADU back to the Item rows."""
    session = get_session()
    try:
        for r in results:
            if part_numbers is not None and r["Part Number"] not in part_numbers:
                continue
            item = session.query(Item).get(r["item_id"])
            if item:
                item.adu = r["Calc ADU"]
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


def _show_adu_from_demand():
    """Quick ADU recalculation from actual demand — shown as an expander in Item List."""
    with st.expander("🔄 Recalculate ADU from Actual Demand", expanded=False):
        col1, col2 = st.columns([1, 3])
        with col1:
            lookback = st.number_input(
                "Lookback period (days)", min_value=7, max_value=730,
                value=90, step=7,
                help="Number of past calendar days of actual demand to use.",
                key="adu_lookback",
            )

        with col2:
            st.markdown(
                "Calculates **ADU = total actual demand ÷ lookback days** for every item. "
                "Only `actual` demand entries are used. Preview the changes before applying."
            )

        if st.button("📊 Calculate ADU", key="calc_adu_btn", type="primary"):
            with st.spinner("Calculating…"):
                st.session_state["adu_results"] = _compute_adu_from_actual(int(lookback))

        results = st.session_state.get("adu_results")
        if not results:
            return

        df = pd.DataFrame([{k: v for k, v in r.items() if k != "item_id"} for r in results])

        def _style_row(row):
            try:
                changed = abs(float(row["Calc ADU"]) - float(row["Current ADU"])) > 0.001
            except Exception:
                changed = False
            bg = "#EBF5FB" if changed else "#FFFFFF"
            return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

        st.dataframe(
            df.style.apply(_style_row, axis=1),
            use_container_width=True, hide_index=True,
        )
        st.caption(
            f"Blue rows = ADU value will change. "
            f"Items with 0 demand days had no actual entries in the last {lookback} days."
        )

        st.divider()
        confirmed = st.checkbox(
            "I have reviewed the values and want to apply them",
            key="adu_apply_confirm",
        )

        ca, cb, _ = st.columns([1, 2, 2])
        with ca:
            if st.button("✅ Apply to All", type="primary",
                         disabled=not confirmed, key="adu_apply_all"):
                try:
                    _apply_adu_results(results)
                    st.success(f"ADU updated for {len(results)} item(s).")
                    st.session_state.pop("adu_results", None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

        with cb:
            selected = st.multiselect(
                "Or apply to specific items",
                [r["Part Number"] for r in results],
                key="adu_apply_select",
            )
            if selected and confirmed:
                if st.button("Apply to Selected", key="adu_apply_sel_btn"):
                    try:
                        _apply_adu_results(results, selected)
                        st.success(f"ADU updated for: {', '.join(selected)}")
                        st.session_state.pop("adu_results", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")


def _show_item_list():
    _show_adu_from_demand()
    st.divider()

    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == get_company_id()).order_by(Item.part_number).all()

        if not items:
            st.info("No items yet. Go to the **Add Item** tab to create one.")
            return

        rows = []
        for it in items:
            zones = calculate_zones(it)
            rows.append({
                "Part Number": it.part_number,
                "Description": it.description,
                "Category": it.category,
                "UoM": it.unit_of_measure,
                "Type": it.item_type or "P",
                "Profile": it.buffer_profile.name if it.buffer_profile else "",
                "ADU": it.adu,
                "DLT (days)": it.dlt,
                "LT Factor": it.lead_time_factor,
                "Var. Factor": it.variability_factor,
                "MOQ": it.min_order_qty,
                "Order Cycle": it.order_cycle,
                "On Hand": it.on_hand,
                "Default Supplier": it.supplier.code if it.supplier else "",
                "Unit Cost (€)": round(it.unit_cost or 0.0, 2),
                "Ordering Cost (€)": round(it.ordering_cost or 0.0, 2),
                "Holding %": round((it.holding_cost_pct or 0.0) * 100, 2),
                "TOG": round(zones.top_of_green, 2),
                "TOY": round(zones.top_of_yellow, 2),
                "TOR": round(zones.top_of_red, 2),
                "Avg Inv Target": round(zones.avg_inventory_target, 2),
            })
    finally:
        session.close()

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"{len(rows)} item(s) in database.")


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------

def _show_add_item():
    profiles = _load_profiles(get_company_id())
    profile_options = ["(none — manual LTF/VF)"] + list(profiles.keys())
    suppliers = _load_suppliers(get_company_id())
    supplier_options = ["— None —"] + list(suppliers.keys())

    with st.form("add_item_form", clear_on_submit=True):
        st.subheader("New Item")
        col1, col2 = st.columns(2)

        with col1:
            part_number = st.text_input("Part Number *", placeholder="e.g. RM-001")
            description = st.text_input("Description *", placeholder="e.g. Raw Material A")
            category = st.text_input("Category", placeholder="e.g. Raw Material")
            uom = st.selectbox("Unit of Measure", ["EA", "KG", "LT", "M", "MT", "PC"])
            on_hand = st.number_input("Current On-Hand Qty", min_value=0.0, value=0.0, step=1.0)
            item_type = st.selectbox(
                "DDMRP Item Type",
                options=ITEM_TYPES,
                index=ITEM_TYPES.index("P"),
                format_func=lambda k: ITEM_TYPE_LABELS[k],
                help="Slide 50: M=Manufactured, I=Intermediate, P=Purchased, D=Distributed.",
            )
            profile_name = st.selectbox(
                "Buffer Profile",
                options=profile_options,
                help=("Slides 50-54 — Item Type x Lead Time category x Variability category. "
                      "Selecting a profile auto-fills LTF/VF inside its canonical band."),
            )

        with col2:
            adu = st.number_input(
                "ADU — Average Daily Usage", min_value=0.0, value=10.0, step=1.0,
                help="How many units are consumed per day on average."
            )
            dlt = st.number_input(
                "DLT — Decoupled Lead Time (days)", min_value=0.0, value=5.0, step=0.5,
                help="Lead time from this decoupling point."
            )
            ltf = st.selectbox(
                "Lead Time Factor (LTF)",
                options=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0],
                index=4,
                help="LTF bands: L=0.20-0.40, M=0.41-0.60, S=0.61-1.0."
            )
            vf = st.selectbox(
                "Variability Factor (VF)",
                options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0],
                index=5,
                help="VF bands: L=0.0-0.40, M=0.41-0.60, H=0.61-1.0."
            )
            moq = st.number_input("Minimum Order Quantity (MOQ)", min_value=0.0, value=0.0, step=1.0)
            order_cycle = st.number_input(
                "Order Cycle (days)", min_value=0.0, value=0.0, step=1.0,
                help="How frequently you place orders. Used in Green Zone calculation."
            )

        st.markdown("**📈 ASOH Parameters** (Adjusted Spike Horizon, deck slide 83 — optional)")
        as1, as2 = st.columns(2)
        with as1:
            spike_horizon = st.number_input(
                "Spike Horizon (days)", min_value=0, value=0, step=1,
                help="Days ahead in which qualifying demand spikes are detected. 0 → defaults to DLT.",
            )
        with as2:
            spike_factor = st.number_input(
                "Spike Threshold Factor (× ADU)", min_value=0.0, value=0.0, step=0.1,
                help="A demand entry above (factor × ADU) becomes a spike. 0 → use global default (2.0).",
            )

        st.markdown("**💰 Cost Parameters** (optional — for Safety Stock & EOQ)")
        cc1, cc2, cc3 = st.columns(3)
        with cc1:
            unit_cost = st.number_input(
                "Unit Cost (€)", min_value=0.0, value=0.0, step=0.01,
                help="Purchase or production cost per unit.",
            )
        with cc2:
            ordering_cost = st.number_input(
                "Ordering Cost (€/order)", min_value=0.0, value=0.0, step=1.0,
                help="Fixed cost per order. Leave 0 to use global default on the Safety Stock page.",
            )
        with cc3:
            holding_pct = st.number_input(
                "Holding Cost (% / year)", min_value=0.0, max_value=100.0, value=0.0, step=1.0,
                help="Annual holding cost as a % of unit cost. Leave 0 to use global default.",
            )

        sup_label = st.selectbox("Default Supplier", supplier_options,
                                 help="The supplier this part is normally purchased from.")

        submitted = st.form_submit_button("Add Item", type="primary")

    if submitted:
        if not part_number or not description:
            st.error("Part Number and Description are required.")
            return

        # Resolve buffer profile + validate LTF / VF bands
        chosen_profile = profiles.get(profile_name) if profile_name in profiles else None
        if chosen_profile is not None:
            ltf_band = LTF_BANDS.get(chosen_profile["lt_category"])
            vf_band  = VF_BANDS.get(chosen_profile["var_category"])
            if ltf_band and not _validate_band(ltf, ltf_band, "LTF"):
                st.error(f"LTF {ltf} is outside the band for category {chosen_profile['lt_category']} "
                         f"(allowed {ltf_band[0]:.2f}-{ltf_band[1]:.2f}).")
                return
            if vf_band and not _validate_band(vf, vf_band, "VF"):
                st.error(f"VF {vf} is outside the band for category {chosen_profile['var_category']} "
                         f"(allowed {vf_band[0]:.2f}-{vf_band[1]:.2f}).")
                return

        session = get_session()
        try:
            existing = session.query(Item).filter_by(part_number=part_number.strip().upper()).first()
            if existing:
                st.error(f"Part number **{part_number}** already exists.")
                return

            item = Item(
                part_number=part_number.strip().upper(),
                description=description.strip(),
                category=category.strip(),
                unit_of_measure=uom,
                item_type=item_type,
                buffer_profile_id=chosen_profile["id"] if chosen_profile else None,
                on_hand=on_hand,
                adu=adu,
                dlt=dlt,
                lead_time_factor=ltf,
                variability_factor=vf,
                min_order_qty=moq,
                order_cycle=order_cycle,
                spike_horizon_days=int(spike_horizon) if spike_horizon else None,
                spike_threshold_factor=float(spike_factor) if spike_factor else None,
                unit_cost=unit_cost,
                ordering_cost=ordering_cost,
                holding_cost_pct=holding_pct / 100.0,
                default_supplier_id=suppliers[sup_label]["id"] if sup_label != "— None —" else None,
                company_id=get_company_id(),
            )
            session.add(item)
            session.commit()
            st.success(f"Item **{part_number.upper()}** created successfully!")

            # Show preview of calculated buffer zones
            zones = calculate_zones(item)
            st.subheader("Calculated Buffer Zones Preview")
            cols = st.columns(3)
            cols[0].metric("Top of Red (TOR)", f"{zones.top_of_red:.1f}")
            cols[1].metric("Top of Yellow (TOY)", f"{zones.top_of_yellow:.1f}")
            cols[2].metric("Top of Green (TOG)", f"{zones.top_of_green:.1f}")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Edit / Delete
# ---------------------------------------------------------------------------

def _show_edit_item():
    session = get_session()
    try:
        items = session.query(Item).filter(Item.company_id == get_company_id()).order_by(Item.part_number).all()
        item_options = {f"{it.part_number} — {it.description}": it.id for it in items}
    finally:
        session.close()

    if not item_options:
        st.info("No items to edit. Add one first.")
        return

    selected_label = st.selectbox("Select Item", list(item_options.keys()))
    selected_id = item_options[selected_label]

    profiles = _load_profiles(get_company_id())
    profile_options = ["(none — manual LTF/VF)"] + list(profiles.keys())

    session = get_session()
    try:
        item = session.query(Item).get(selected_id)

        with st.form("edit_item_form"):
            st.subheader(f"Editing: {item.part_number}")
            col1, col2 = st.columns(2)

            with col1:
                description = st.text_input("Description", value=item.description)
                category = st.text_input("Category", value=item.category)
                uom = st.selectbox("Unit of Measure", ["EA", "KG", "LT", "M", "MT", "PC"],
                                   index=["EA", "KG", "LT", "M", "MT", "PC"].index(item.unit_of_measure)
                                   if item.unit_of_measure in ["EA", "KG", "LT", "M", "MT", "PC"] else 0)
                on_hand = st.number_input("On-Hand Qty", value=float(item.on_hand), step=1.0)
                cur_type = item.item_type if item.item_type in ITEM_TYPES else "P"
                item_type = st.selectbox(
                    "DDMRP Item Type",
                    options=ITEM_TYPES,
                    index=ITEM_TYPES.index(cur_type),
                    format_func=lambda k: ITEM_TYPE_LABELS[k],
                )
                cur_profile_name = item.buffer_profile.name if item.buffer_profile else "(none — manual LTF/VF)"
                profile_idx = profile_options.index(cur_profile_name) if cur_profile_name in profile_options else 0
                profile_name = st.selectbox(
                    "Buffer Profile",
                    options=profile_options,
                    index=profile_idx,
                    help="Slides 50-54. Selecting a profile validates LTF/VF against the band on save.",
                )

            with col2:
                adu = st.number_input("ADU", value=float(item.adu), step=1.0)
                dlt = st.number_input("DLT (days)", value=float(item.dlt), step=0.5)
                ltf_options = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
                ltf = st.selectbox("Lead Time Factor",
                                   ltf_options,
                                   index=ltf_options.index(item.lead_time_factor)
                                   if item.lead_time_factor in ltf_options else 4)
                vf_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
                vf = st.selectbox("Variability Factor",
                                  vf_options,
                                  index=vf_options.index(item.variability_factor)
                                  if item.variability_factor in vf_options else 5)
                moq = st.number_input("MOQ", value=float(item.min_order_qty), step=1.0)
                order_cycle = st.number_input("Order Cycle (days)", value=float(item.order_cycle), step=1.0)

            st.markdown("**📈 ASOH Parameters** (Adjusted Spike Horizon, slide 83 — optional)")
            as1, as2 = st.columns(2)
            with as1:
                spike_horizon = st.number_input(
                    "Spike Horizon (days)", min_value=0, step=1,
                    value=int(item.spike_horizon_days) if item.spike_horizon_days else 0,
                    help="0 → defaults to DLT.",
                )
            with as2:
                spike_factor = st.number_input(
                    "Spike Threshold Factor (× ADU)", min_value=0.0, step=0.1,
                    value=float(item.spike_threshold_factor) if item.spike_threshold_factor else 0.0,
                    help="0 → use global default (2.0).",
                )

            st.markdown("**💰 Cost Parameters** (optional — for Safety Stock & EOQ)")
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                unit_cost = st.number_input(
                    "Unit Cost (€)", min_value=0.0,
                    value=float(item.unit_cost or 0.0), step=0.01,
                )
            with cc2:
                ordering_cost = st.number_input(
                    "Ordering Cost (€/order)", min_value=0.0,
                    value=float(item.ordering_cost or 0.0), step=1.0,
                    help="Leave 0 to use global default on the Safety Stock page.",
                )
            with cc3:
                holding_pct = st.number_input(
                    "Holding Cost (% / year)", min_value=0.0, max_value=100.0,
                    value=float((item.holding_cost_pct or 0.0) * 100.0), step=1.0,
                    help="Leave 0 to use global default.",
                )

            col_save, col_delete = st.columns([3, 1])
            save = col_save.form_submit_button("Save Changes", type="primary")
            delete = col_delete.form_submit_button("Delete Item", type="secondary")

        if save:
            # Validate LTF/VF against profile bands
            chosen_profile = profiles.get(profile_name) if profile_name in profiles else None
            if chosen_profile is not None:
                ltf_band = LTF_BANDS.get(chosen_profile["lt_category"])
                vf_band  = VF_BANDS.get(chosen_profile["var_category"])
                if ltf_band and not _validate_band(ltf, ltf_band, "LTF"):
                    st.error(f"LTF {ltf} is outside the band for category {chosen_profile['lt_category']} "
                             f"(allowed {ltf_band[0]:.2f}-{ltf_band[1]:.2f}).")
                    return
                if vf_band and not _validate_band(vf, vf_band, "VF"):
                    st.error(f"VF {vf} is outside the band for category {chosen_profile['var_category']} "
                             f"(allowed {vf_band[0]:.2f}-{vf_band[1]:.2f}).")
                    return

            session2 = get_session()
            try:
                it = session2.query(Item).get(selected_id)
                it.description = description
                it.category = category
                it.unit_of_measure = uom
                it.item_type = item_type
                it.buffer_profile_id = chosen_profile["id"] if chosen_profile else None
                it.on_hand = on_hand
                it.adu = adu
                it.dlt = dlt
                it.lead_time_factor = ltf
                it.variability_factor = vf
                it.min_order_qty = moq
                it.order_cycle = order_cycle
                it.spike_horizon_days = int(spike_horizon) if spike_horizon else None
                it.spike_threshold_factor = float(spike_factor) if spike_factor else None
                it.unit_cost = unit_cost
                it.ordering_cost = ordering_cost
                it.holding_cost_pct = holding_pct / 100.0
                session2.commit()
                st.success("Item updated successfully!")
                st.rerun()
            finally:
                session2.close()

        if delete:
            session2 = get_session()
            try:
                it = session2.query(Item).get(selected_id)
                session2.delete(it)
                session2.commit()
                st.success(f"Item {item.part_number} deleted.")
                st.rerun()
            finally:
                session2.close()

    finally:
        session.close()


# ---------------------------------------------------------------------------
# Dynamic Parameter Calculator
# ---------------------------------------------------------------------------

def _show_param_calculator():
    st.subheader("🔄 Dynamic Parameter Recalculation")
    st.markdown(
        "Calculates **ADU**, **DLT**, **Lead Time Factor** and **Variability Factor** "
        "automatically from your demand history and open supply orders.\n\n"
        "| Parameter | Source | Method |\n"
        "|---|---|---|\n"
        "| **ADU** | Demand entries | Total demand ÷ period days (past / forward / blended) |\n"
        "| **VF** | Demand entries | Coefficient of variation of daily demand |\n"
        "| **DLT** | Supply entries | Average days until open supply orders arrive |\n"
        "| **LTF** | Supply entries | Coefficient of variation of supply lead times |\n"
    )
    st.divider()

    # ── Configuration ──────────────────────────────────────────────────────
    st.markdown("**Configuration**")
    col1, col2, col3 = st.columns(3)

    with col1:
        adu_method = st.selectbox(
            "ADU Method",
            options=["blended", "past", "forward"],
            format_func=lambda x: {
                "blended": "Blended (past + forecast)",
                "past":    "Past only (historical)",
                "forward": "Forward only (forecast)",
            }[x],
        )
        lookback_days = st.number_input(
            "Lookback period (days)", min_value=7, max_value=365, value=60, step=7,
            help="How many past days of actual demand to analyse.",
        )

    with col2:
        forward_days = st.number_input(
            "Forward period (days)", min_value=7, max_value=180, value=30, step=7,
            help="How many future days of forecast demand to include.",
        )
        if adu_method == "blended":
            past_weight    = st.slider("Past demand weight", 0.1, 0.9, 0.6, 0.1)
            forward_weight = round(1.0 - past_weight, 1)
            st.caption(f"Forward weight: **{forward_weight}**")
        else:
            past_weight, forward_weight = 1.0, 0.0

    with col3:
        scope = st.radio("Apply to", ["All items", "Selected item"])
        selected_item_id = None
        if scope == "Selected item":
            session = get_session()
            try:
                items = session.query(Item).filter(Item.company_id == get_company_id()).order_by(Item.part_number).all()
                item_opts = {f"{it.part_number} — {it.description}": it.id for it in items}
            finally:
                session.close()
            if item_opts:
                sel_label        = st.selectbox("Item", list(item_opts.keys()), key="calc_item_sel")
                selected_item_id = item_opts[sel_label]

    st.divider()

    # ── Run ─────────────────────────────────────────────────────────────────
    if st.button("📊 Calculate Parameters", type="primary"):
        with st.spinner("Analysing demand and supply data…"):
            if scope == "All items":
                results = calculate_all_params(
                    lookback_days=int(lookback_days), forward_days=int(forward_days),
                    adu_method=adu_method, past_weight=past_weight,
                    forward_weight=forward_weight,
                )
            else:
                session = get_session()
                try:
                    item = session.query(Item).get(selected_item_id)
                    results = [calculate_params(
                        item, int(lookback_days), int(forward_days),
                        adu_method, past_weight, forward_weight,
                    )]
                finally:
                    session.close()
        st.session_state["calc_results"] = results
        st.success(f"Calculated for {len(results)} item(s). Review below, then apply.")

    results = st.session_state.get("calc_results")
    if not results:
        st.info("Configure settings above and click **Calculate Parameters** to preview.")
        return

    # ── Preview table ───────────────────────────────────────────────────────
    st.subheader("Preview — Current vs Calculated")
    st.caption("Blue-tinted rows = value changed from current.")

    rows = []
    for c in results:
        def _d(new, old):
            return f"{((new-old)/old)*100:+.1f}%" if old else "—"
        rows.append({
            "Part Number":    c.part_number,
            "Description":    c.description,
            "ADU (now)":      c.current_adu,
            "ADU (calc)":     c.adu,
            "ADU Δ":          _d(c.adu, c.current_adu),
            "DLT (now)":      c.current_dlt,
            "DLT (calc)":     c.dlt,
            "DLT Δ":          _d(c.dlt, c.current_dlt),
            "LTF (now)":      c.current_ltf,
            "LTF (calc)":     c.lead_time_factor,
            "VF (now)":       c.current_vf,
            "VF (calc)":      c.variability_factor,
            "Past ADU":       c.past_adu,
            "Fwd ADU":        c.forward_adu,
            "CV demand":      c.cv_demand,
            "CV lead time":   c.cv_lt,
            "Demand days":    c.n_demand_days,
            "Supply entries": c.n_supply_entries,
            "Data quality":   "✅ OK" if c.demand_data_sufficient and c.supply_data_sufficient
                              else ("⚠️ Low demand" if not c.demand_data_sufficient
                                    else "⚠️ Low supply"),
        })

    df = pd.DataFrame(rows)

    def _style(row):
        changed = abs(float(row["ADU (calc)"]) - float(row["ADU (now)"])) > 0.01
        bg = "#EBF5FB" if changed else "#FFFFFF"
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    st.dataframe(df.style.apply(_style, axis=1), use_container_width=True, hide_index=True)

    # ── Single-item diagnostics ──────────────────────────────────────────────
    if len(results) == 1:
        c = results[0]
        st.divider()
        st.subheader(f"Diagnostics — {c.part_number}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Past ADU",      f"{c.past_adu:.2f}")
        m2.metric("Forward ADU",   f"{c.forward_adu:.2f}")
        m3.metric("CV Demand",     f"{c.cv_demand:.3f}",
                  help="< 0.20 = very low  |  0.40–0.60 = medium  |  > 0.80 = very high")
        m4.metric("CV Lead Time",  f"{c.cv_lt:.3f}",
                  help="< 0.20 = very stable  |  > 0.60 = highly variable")

        i1, i2 = st.columns(2)
        i1.info(
            f"**Demand data:** {c.n_demand_days} days with actual demand "
            f"(lookback: {int(lookback_days)} days)\n\n"
            f"{'✅ Sufficient' if c.demand_data_sufficient else '⚠️ Not enough — ADU may be underestimated'}"
        )
        i2.info(
            f"**Supply data:** {c.n_supply_entries} open supply orders\n\n"
            f"{'✅ DLT/LTF calculated from supply' if c.supply_data_sufficient else '⚠️ Not enough supply data — DLT/LTF kept from existing values'}"
        )

    st.divider()

    # ── Apply ────────────────────────────────────────────────────────────────
    st.markdown("**Apply calculated parameters**")
    st.warning(
        "This overwrites ADU, DLT, Lead Time Factor and Variability Factor. "
        "Order Cycle and MOQ are not changed.",
        icon="⚠️",
    )
    confirmed = st.checkbox(
        "I have reviewed the values and want to apply them",
        key="apply_params_confirm",
    )

    ca, cb, _ = st.columns([1, 2, 2])
    with ca:
        if st.button("✅ Apply to All", type="primary",
                     disabled=not confirmed, use_container_width=True):
            try:
                apply_all_params(results)
                st.success(f"✅ Parameters applied to {len(results)} item(s).")
                st.session_state.pop("calc_results", None)
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

    with cb:
        if len(results) > 1:
            apply_parts = st.multiselect(
                "Or apply to specific items only",
                [c.part_number for c in results],
                key="apply_select",
            )
            if apply_parts and confirmed:
                if st.button("Apply to Selected", key="apply_selected_btn"):
                    to_apply = [c for c in results if c.part_number in apply_parts]
                    try:
                        apply_all_params(to_apply)
                        st.success(f"Applied to: {', '.join(apply_parts)}")
                        st.session_state.pop("calc_results", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
