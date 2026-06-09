"""
Supplier-Part Matrix — manage the many-to-many association between parts
(Items) and the suppliers that can provide them.

Historically a part carried a single `Item.default_supplier_id`. This page
lets a part be sourced from several suppliers, each link carrying its own
commercial terms (unit cost, lead time, MOQ, preferred flag). The links are
stored in the `item_suppliers` table and consumed by Session 1 (Sourcing
Allocation).

Existing master data is referenced, not duplicated: Items remain the system
of record for the part, Suppliers for the vendor. A blank/zero per-link value
inherits from the Item or Supplier master at solve time.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from database.db import (
    get_session, Item, Supplier, ItemSupplier, Base, engine,
)
from database.auth import get_company_id
from modules.importer import (
    build_item_supplier_template,
    import_item_suppliers,
    export_item_suppliers,
    render_import_widget,
)


def _ensure_tables_exist() -> None:
    """Defensive create_all so the table exists right after a deploy."""
    try:
        Base.metadata.create_all(engine, tables=[ItemSupplier.__table__])
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────────────────
# Loaders
# ───────────────────────────────────────────────────────────────────────────

def _load_links(company_id: int) -> list[ItemSupplier]:
    sess = get_session()
    try:
        return (sess.query(ItemSupplier)
                    .filter(ItemSupplier.company_id == company_id)
                    .order_by(ItemSupplier.item_id, ItemSupplier.supplier_id)
                    .all())
    finally:
        sess.close()


def _item_options(company_id: int) -> dict[int, str]:
    sess = get_session()
    try:
        return {
            it.id: f"{it.part_number} · {it.description or ''}".strip(" ·")
            for it in (sess.query(Item)
                           .filter(Item.company_id == company_id)
                           .order_by(Item.part_number).all())
        }
    finally:
        sess.close()


def _sync_preferred(sess, company_id: int) -> list[str]:
    """
    Enforce at-most-one preferred supplier per part and sync the chosen
    preferred link back to `Item.default_supplier_id` (the legacy single-supplier
    field, kept authoritative in one place).

    Returns a list of human-readable warnings for any conflicts resolved.
    Does NOT clear an item's default_supplier_id when it has no preferred link —
    a manually-set default in Material Master is left untouched.
    """
    warnings: list[str] = []
    links = (sess.query(ItemSupplier)
                 .filter(ItemSupplier.company_id == company_id)
                 .all())
    by_item: dict[int, list] = {}
    for l in links:
        by_item.setdefault(l.item_id, []).append(l)

    for item_id, ls in by_item.items():
        prefs = [l for l in ls if l.is_preferred and (l.status or "active") != "inactive"]
        chosen = None
        if len(prefs) > 1:
            chosen = prefs[0]
            for extra in prefs[1:]:
                extra.is_preferred = False
            warnings.append(
                f"Part #{item_id}: had {len(prefs)} preferred suppliers — "
                f"kept one, cleared {len(prefs) - 1}."
            )
        elif len(prefs) == 1:
            chosen = prefs[0]

        if chosen is not None:
            item = (sess.query(Item)
                        .filter(Item.id == item_id, Item.company_id == company_id)
                        .first())
            if item is not None and item.default_supplier_id != chosen.supplier_id:
                item.default_supplier_id = chosen.supplier_id
    return warnings


def _supplier_options(company_id: int) -> dict[int, str]:
    sess = get_session()
    try:
        return {
            s.id: f"{s.code} · {s.name}"
            for s in (sess.query(Supplier)
                          .filter(Supplier.company_id == company_id,
                                  Supplier.status != "inactive")
                          .order_by(Supplier.code).all())
        }
    finally:
        sess.close()


# ───────────────────────────────────────────────────────────────────────────
# View
# ───────────────────────────────────────────────────────────────────────────

def show() -> None:
    _ensure_tables_exist()
    company_id = get_company_id()

    st.title("🔗 Supplier-Part Matrix")
    st.caption(
        "Assign one or more suppliers to each part. Each link carries its own "
        "unit cost, lead time, MOQ, and a preferred flag. Used by Session 1 "
        "(Sourcing Allocation). Item & Supplier master data is referenced, not duplicated."
    )

    item_opts = _item_options(company_id)
    sup_opts  = _supplier_options(company_id)

    if not item_opts:
        st.warning("No items found. Add parts in **Material Master** first.")
        return
    if not sup_opts:
        st.warning("No suppliers found. Add suppliers in **Supplier Master** first.")
        return

    links = _load_links(company_id)

    k1, k2, k3 = st.columns(3)
    k1.metric("Parts with a supplier", len({l.item_id for l in links}))
    k2.metric("Associations", len(links))
    k3.metric("Suppliers used", len({l.supplier_id for l in links}))

    st.divider()

    tab_manage, tab_io = st.tabs(["📝 Manage", "⬆️ Import / Export"])

    with tab_manage:
        _render_add_form(company_id, item_opts, sup_opts, links)
        st.divider()
        _render_matrix(company_id, item_opts, sup_opts, links)

    with tab_io:
        _render_import_export()


# ───────────────────────────────────────────────────────────────────────────
# Add form
# ───────────────────────────────────────────────────────────────────────────

def _render_add_form(company_id, item_opts, sup_opts, links) -> None:
    st.subheader("Add association")
    existing = {(l.item_id, l.supplier_id) for l in links}

    with st.form("add_item_supplier", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            item_id = st.selectbox(
                "Part *", options=list(item_opts.keys()),
                format_func=lambda x: item_opts[x],
            )
        with c2:
            supplier_id = st.selectbox(
                "Supplier *", options=list(sup_opts.keys()),
                format_func=lambda x: sup_opts[x],
            )

        c3, c4, c5 = st.columns(3)
        with c3:
            unit_cost = st.number_input("Unit cost (€)", min_value=0.0, value=0.0, step=0.01,
                                        help="0 → inherit Item.unit_cost at solve time.")
        with c4:
            lead_time_days = st.number_input("Lead time (days)", min_value=0, value=0, step=1,
                                             help="0 → inherit the Supplier's default lead time.")
        with c5:
            min_order_qty = st.number_input("Min order qty", min_value=0.0, value=0.0, step=1.0,
                                            help="0 → inherit Item.min_order_qty.")

        c6, c7 = st.columns(2)
        with c6:
            is_preferred = st.checkbox("Preferred (primary source)")
        with c7:
            status = st.selectbox("Status", ["active", "inactive"], index=0)

        submitted = st.form_submit_button("➕ Add association", type="primary")

    if submitted:
        if (item_id, supplier_id) in existing:
            st.error("That part ↔ supplier association already exists.")
            return
        sess = get_session()
        try:
            # A new preferred link supersedes any existing preferred for this part.
            if is_preferred:
                for other in (sess.query(ItemSupplier)
                                  .filter(ItemSupplier.company_id == company_id,
                                          ItemSupplier.item_id == int(item_id),
                                          ItemSupplier.is_preferred.is_(True))
                                  .all()):
                    other.is_preferred = False
            sess.add(ItemSupplier(
                company_id=company_id,
                item_id=int(item_id),
                supplier_id=int(supplier_id),
                unit_cost=float(unit_cost),
                lead_time_days=int(lead_time_days),
                min_order_qty=float(min_order_qty),
                is_preferred=bool(is_preferred),
                status=status,
            ))
            sess.flush()
            _sync_preferred(sess, company_id)
            sess.commit()
            st.success("Association added.")
            st.rerun()
        except Exception as exc:
            sess.rollback()
            st.error(f"Could not add: {exc}")
        finally:
            sess.close()


# ───────────────────────────────────────────────────────────────────────────
# Editable matrix
# ───────────────────────────────────────────────────────────────────────────

def _render_matrix(company_id, item_opts, sup_opts, links) -> None:
    st.subheader("Current associations")
    if not links:
        st.info("No associations yet. Add one above or import from Excel.")
        return

    rows = [{
        "id":             l.id,
        "Part":           item_opts.get(l.item_id, f"#{l.item_id}"),
        "Supplier":       sup_opts.get(l.supplier_id, f"#{l.supplier_id}"),
        "Unit Cost (€)":  float(l.unit_cost or 0.0),
        "Lead Time (d)":  int(l.lead_time_days or 0),
        "Min Order Qty":  float(l.min_order_qty or 0.0),
        "Preferred":      bool(l.is_preferred),
        "Status":         l.status or "active",
        "Delete":         False,
    } for l in links]

    df = pd.DataFrame(rows)
    edited = st.data_editor(
        df,
        use_container_width=True,
        hide_index=True,
        disabled=["id", "Part", "Supplier"],
        column_config={
            "id":            None,
            "Unit Cost (€)": st.column_config.NumberColumn(format="€ %.2f", min_value=0.0),
            "Lead Time (d)": st.column_config.NumberColumn(format="%d", min_value=0),
            "Min Order Qty": st.column_config.NumberColumn(format="%.0f", min_value=0.0),
            "Preferred":     st.column_config.CheckboxColumn(),
            "Status":        st.column_config.SelectboxColumn(options=["active", "inactive"]),
            "Delete":        st.column_config.CheckboxColumn(help="Tick to delete on save"),
        },
        key="sp_matrix_editor",
    )

    if st.button("💾 Save changes", type="primary"):
        _persist_matrix(company_id, edited)


def _persist_matrix(company_id, edited: pd.DataFrame) -> None:
    sess = get_session()
    deleted = updated = 0
    try:
        by_id = {
            l.id: l for l in
            sess.query(ItemSupplier).filter(ItemSupplier.company_id == company_id).all()
        }
        for _, r in edited.iterrows():
            link = by_id.get(int(r["id"]))
            if link is None:
                continue
            if bool(r["Delete"]):
                sess.delete(link)
                deleted += 1
                continue
            link.unit_cost      = float(r["Unit Cost (€)"] or 0.0)
            link.lead_time_days = int(r["Lead Time (d)"] or 0)
            link.min_order_qty  = float(r["Min Order Qty"] or 0.0)
            link.is_preferred   = bool(r["Preferred"])
            link.status         = str(r["Status"] or "active")
            updated += 1
        sess.flush()
        warns = _sync_preferred(sess, company_id)
        sess.commit()
        msg = f"Saved — {updated} updated, {deleted} deleted."
        if warns:
            st.warning(" ".join(warns))
        st.success(msg)
        st.rerun()
    except Exception as exc:
        sess.rollback()
        st.error(f"Save failed: {exc}")
    finally:
        sess.close()


# ───────────────────────────────────────────────────────────────────────────
# Import / Export
# ───────────────────────────────────────────────────────────────────────────

def _render_import_export() -> None:
    st.subheader("Export")
    st.caption("Download all current associations in the import template layout.")
    st.download_button(
        "⬇️ Export associations (.xlsx)",
        data=export_item_suppliers(),
        file_name="supplier_part_matrix_export.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="sp_export_btn",
    )

    st.divider()

    render_import_widget(
        label="Supplier-Part",
        template_fn=build_item_supplier_template,
        import_fn=import_item_suppliers,
        template_filename="supplier_part_template.xlsx",
        key="item_suppliers",
    )
