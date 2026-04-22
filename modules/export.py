"""
Excel Export module — Streamlit page.
Exports replenishment signals, buffer parameters, and demand/supply data to .xlsx.
"""

import io
import streamlit as st
import pandas as pd
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import (
    PatternFill, Font, Alignment, Border, Side
)
from openpyxl.utils import get_column_letter
from database.db import get_session, Item, Buffer, DemandEntry, SupplyEntry


STATUS_FILL = {
    "red":     PatternFill("solid", fgColor="FADBD8"),
    "yellow":  PatternFill("solid", fgColor="FDEBD0"),
    "green":   PatternFill("solid", fgColor="D5F5E3"),
    "unknown": PatternFill("solid", fgColor="F2F3F4"),
}

HEADER_FILL = PatternFill("solid", fgColor="2C3E50")
HEADER_FONT = Font(color="FFFFFF", bold=True)
THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)


def show():
    st.header("Export to Excel")
    st.caption("Generate Excel reports for replenishment signals and buffer data.")

    tab_signals, tab_buffers, tab_demand, tab_supply = st.tabs([
        "Replenishment Signals", "Buffer Parameters", "Demand Entries", "Supply Entries"
    ])

    with tab_signals:
        _export_signals()

    with tab_buffers:
        _export_buffer_params()

    with tab_demand:
        _export_demand()

    with tab_supply:
        _export_supply()


# ---------------------------------------------------------------------------
# Replenishment Signals export
# ---------------------------------------------------------------------------

def _export_signals():
    st.subheader("Replenishment Signals Export")
    st.caption("Exports all items with their current buffer status and suggested order quantities.")

    if st.button("Generate Signals Report", type="primary"):
        wb = _build_signals_workbook()
        buf = _wb_to_bytes(wb)
        filename = f"DDMRP_Signals_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        st.download_button(
            label="Download Signals Report",
            data=buf,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def _build_signals_workbook() -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Replenishment Signals"

    headers = [
        "Status", "Part Number", "Description", "Category",
        "On Hand", "On Order (open)", "Net Flow Position",
        "Top of Red", "Top of Yellow", "Top of Green",
        "Suggested Order Qty", "ADU", "DLT (days)", "Last Calculated",
    ]
    _write_header_row(ws, headers, row=1)

    session = get_session()
    try:
        items = session.query(Item).order_by(Item.part_number).all()
        buffers = {b.item_id: b for b in session.query(Buffer).all()}

        priority = {"red": 0, "yellow": 1, "green": 2}
        items_sorted = sorted(
            items,
            key=lambda it: priority.get(buffers[it.id].status if it.id in buffers else "green", 2)
        )

        for row_idx, item in enumerate(items_sorted, start=2):
            buf = buffers.get(item.id)
            status = buf.status if buf else "unknown"
            fill = STATUS_FILL.get(status, STATUS_FILL["unknown"])

            row_data = [
                status.upper(),
                item.part_number,
                item.description,
                item.category,
                item.on_hand,
                buf.net_flow_position - item.on_hand if buf else 0,  # approximation of on-order
                round(buf.net_flow_position, 2) if buf else 0,
                round(buf.top_of_red, 2) if buf else 0,
                round(buf.top_of_yellow, 2) if buf else 0,
                round(buf.top_of_green, 2) if buf else 0,
                round(buf.suggested_order_qty, 2) if buf else 0,
                item.adu,
                item.dlt,
                buf.last_calculated.strftime("%Y-%m-%d %H:%M") if buf and buf.last_calculated else "",
            ]

            for col_idx, value in enumerate(row_data, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.fill = fill
                cell.border = THIN_BORDER
                cell.alignment = Alignment(horizontal="center")
    finally:
        session.close()

    _autofit_columns(ws)
    return wb


# ---------------------------------------------------------------------------
# Buffer Parameters export
# ---------------------------------------------------------------------------

def _export_buffer_params():
    st.subheader("Buffer Parameters Export")
    st.caption("Exports all DDMRP parameters and calculated zone sizes for every item.")

    if st.button("Generate Buffer Parameters Report", type="primary"):
        wb = _build_params_workbook()
        buf = _wb_to_bytes(wb)
        filename = f"DDMRP_BufferParams_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        st.download_button(
            label="Download Buffer Parameters",
            data=buf,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def _build_params_workbook() -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Buffer Parameters"

    headers = [
        "Part Number", "Description", "Category", "UoM",
        "ADU", "DLT (days)", "Lead Time Factor", "Variability Factor",
        "Min Order Qty", "Order Cycle (days)",
        "Red Zone Base", "Red Zone Safety", "Red Zone (TOR)",
        "Yellow Zone", "Green Zone",
        "Top of Red", "Top of Yellow", "Top of Green",
    ]
    _write_header_row(ws, headers, row=1)

    from modules.buffer_engine import calculate_zones
    session = get_session()
    try:
        items = session.query(Item).order_by(Item.part_number).all()
        for row_idx, item in enumerate(items, start=2):
            z = calculate_zones(item)
            row_data = [
                item.part_number, item.description, item.category, item.unit_of_measure,
                item.adu, item.dlt, item.lead_time_factor, item.variability_factor,
                item.min_order_qty, item.order_cycle,
                round(z.red_zone_base, 2), round(z.red_zone_safety, 2), round(z.red_zone, 2),
                round(z.yellow_zone, 2), round(z.green_zone, 2),
                round(z.top_of_red, 2), round(z.top_of_yellow, 2), round(z.top_of_green, 2),
            ]
            for col_idx, value in enumerate(row_data, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = THIN_BORDER
                cell.alignment = Alignment(horizontal="center")
    finally:
        session.close()

    _autofit_columns(ws)
    return wb


# ---------------------------------------------------------------------------
# Demand export
# ---------------------------------------------------------------------------

def _export_demand():
    st.subheader("Demand Entries Export")
    if st.button("Generate Demand Report", type="primary"):
        wb = Workbook()
        ws = wb.active
        ws.title = "Demand Entries"
        headers = ["ID", "Part Number", "Description", "Type",
                   "Quantity", "Demand Date", "Reference", "Notes"]
        _write_header_row(ws, headers)

        session = get_session()
        try:
            entries = (
                session.query(DemandEntry, Item)
                .join(Item)
                .order_by(DemandEntry.demand_date)
                .all()
            )
            for row_idx, (e, it) in enumerate(entries, start=2):
                row_data = [
                    e.id, it.part_number, it.description, e.demand_type,
                    e.quantity, e.demand_date.strftime("%Y-%m-%d"),
                    e.order_reference, e.notes,
                ]
                for col_idx, val in enumerate(row_data, start=1):
                    ws.cell(row=row_idx, column=col_idx, value=val).border = THIN_BORDER
        finally:
            session.close()

        _autofit_columns(ws)
        buf = _wb_to_bytes(wb)
        st.download_button(
            "Download Demand Report",
            data=buf,
            file_name=f"DDMRP_Demand_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ---------------------------------------------------------------------------
# Supply export
# ---------------------------------------------------------------------------

def _export_supply():
    st.subheader("Supply Entries Export")
    if st.button("Generate Supply Report", type="primary"):
        wb = Workbook()
        ws = wb.active
        ws.title = "Supply Entries"
        headers = ["ID", "Part Number", "Description", "Type",
                   "Quantity", "Due Date", "Reference", "Notes"]
        _write_header_row(ws, headers)

        session = get_session()
        try:
            entries = (
                session.query(SupplyEntry, Item)
                .join(Item)
                .order_by(SupplyEntry.due_date)
                .all()
            )
            for row_idx, (e, it) in enumerate(entries, start=2):
                row_data = [
                    e.id, it.part_number, it.description,
                    e.supply_type.replace("_", " ").title(),
                    e.quantity, e.due_date.strftime("%Y-%m-%d"),
                    e.order_reference, e.notes,
                ]
                for col_idx, val in enumerate(row_data, start=1):
                    ws.cell(row=row_idx, column=col_idx, value=val).border = THIN_BORDER
        finally:
            session.close()

        _autofit_columns(ws)
        buf = _wb_to_bytes(wb)
        st.download_button(
            "Download Supply Report",
            data=buf,
            file_name=f"DDMRP_Supply_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_header_row(ws, headers, row=1):
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        cell.border = THIN_BORDER


def _autofit_columns(ws, min_width=10, max_width=40):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max(max_len + 2, min_width), max_width)


def _wb_to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
