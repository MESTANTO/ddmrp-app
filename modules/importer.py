"""
Shared Excel import utilities.
Each import function returns (success_count, error_list).
Template generators return bytes ready for st.download_button.
"""

import io
import pandas as pd
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

HEADER_FILL  = PatternFill("solid", fgColor="2C3E50")
EXAMPLE_FILL = PatternFill("solid", fgColor="EBF5FB")
HEADER_FONT  = Font(color="FFFFFF", bold=True)
EXAMPLE_FONT = Font(color="1A5276", italic=True)
THIN_BORDER  = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _wb_to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def _write_header(ws, headers: list[dict], row: int = 1):
    """
    headers: list of dicts with keys: name, width (optional), note (optional)
    """
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col_idx, value=h["name"])
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = THIN_BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = h.get("width", 18)
    ws.row_dimensions[row].height = 28


def _write_example_row(ws, values: list, row: int = 2):
    for col_idx, val in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col_idx, value=val)
        cell.fill = EXAMPLE_FILL
        cell.font = EXAMPLE_FONT
        cell.alignment = Alignment(horizontal="center")
        cell.border = THIN_BORDER


def _add_instructions(ws, text: str, row: int, col_span: int):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=col_span)
    cell = ws.cell(row=row, column=1, value=text)
    cell.fill = PatternFill("solid", fgColor="FEF9E7")
    cell.font = Font(italic=True, color="7D6608", size=9)
    cell.alignment = Alignment(wrap_text=True)


def _read_uploaded_file(uploaded_file):
    """Read an uploaded Streamlit file into a DataFrame. Returns None on error."""
    try:
        df = pd.read_excel(uploaded_file, header=0, skiprows=2)  # skip header + example row
        df.columns = df.columns.str.strip()
        df = df.dropna(how="all")
        return df
    except Exception as e:
        return None


# ---------------------------------------------------------------------------
# Material Master template + importer
# ---------------------------------------------------------------------------

MATERIAL_HEADERS = [
    {"name": "Part Number *",        "width": 16},
    {"name": "Description *",        "width": 28},
    {"name": "Category",             "width": 16},
    {"name": "Unit of Measure",      "width": 14},
    {"name": "ADU *",                "width": 12},
    {"name": "DLT (days) *",         "width": 14},
    {"name": "Lead Time Factor",     "width": 16},
    {"name": "Variability Factor",   "width": 16},
    {"name": "Min Order Qty",        "width": 14},
    {"name": "Order Cycle (days)",   "width": 16},
    {"name": "On Hand",              "width": 12},
]

MATERIAL_EXAMPLE = [
    "RM-001", "Raw Material A", "Raw Material", "KG",
    10.0, 5.0, 0.5, 0.5, 50.0, 7.0, 100.0,
]


def build_material_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Items"

    _add_instructions(ws,
        "Instructions: Fill from row 4 onward. Row 2 is an example (do not delete row 1 or 2). "
        "Fields marked * are required. Lead Time Factor and Variability Factor: use values 0.1–1.0. "
        "Unit of Measure options: EA, KG, LT, M, MT, PC.",
        row=1, col_span=len(MATERIAL_HEADERS))
    _write_header(ws, MATERIAL_HEADERS, row=2)
    _write_example_row(ws, MATERIAL_EXAMPLE, row=3)

    # Dropdown validation for UoM
    dv_uom = DataValidation(type="list", formula1='"EA,KG,LT,M,MT,PC"', allow_blank=True)
    ws.add_data_validation(dv_uom)
    dv_uom.sqref = "D4:D1000"

    # Dropdown validation for LTF and VF
    dv_factor = DataValidation(type="list", formula1='"0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,1.0"',
                               allow_blank=True)
    ws.add_data_validation(dv_factor)
    dv_factor.sqref = "G4:H1000"

    ws.freeze_panes = "A4"
    return _wb_to_bytes(wb)


def import_materials(uploaded_file) -> tuple[int, list[str]]:
    from database.db import get_session, Item

    try:
        df = pd.read_excel(uploaded_file, header=1, skiprows=[2])  # header at row 2, skip row 3 (example)
    except Exception as e:
        return 0, [f"Could not read file: {e}"]

    df.columns = df.columns.str.strip()
    df = df.dropna(how="all")
    df.columns = [c.replace(" *", "").strip() for c in df.columns]

    errors = []
    success = 0

    # --- Pre-validate all rows before touching the DB ---
    new_items = []
    for idx, row in df.iterrows():
        row_num = idx + 4
        part = str(row.get("Part Number", "")).strip().upper()
        desc = str(row.get("Description", "")).strip()
        if not part or part == "NAN":
            continue
        if not desc or desc == "nan":
            errors.append(f"Row {row_num}: Description is required for {part}.")
            continue
        try:
            new_items.append(dict(
                part_number = part,
                description = desc,
                category    = str(row.get("Category", "") or "").strip(),
                unit_of_measure = str(row.get("Unit of Measure", "EA") or "EA").strip(),
                adu  = float(row.get("ADU", 0) or 0),
                dlt  = float(row.get("DLT (days)", 0) or 0),
                lead_time_factor  = float(row.get("Lead Time Factor", 0.5) or 0.5),
                variability_factor= float(row.get("Variability Factor", 0.5) or 0.5),
                min_order_qty = float(row.get("Min Order Qty", 0) or 0),
                order_cycle   = float(row.get("Order Cycle (days)", 0) or 0),
                on_hand       = float(row.get("On Hand", 0) or 0),
            ))
        except (ValueError, TypeError) as e:
            errors.append(f"Row {row_num} ({part}): Invalid numeric value — {e}")

    if not new_items:
        return 0, errors + ["No valid rows found in file — existing data was NOT deleted."]

    session = get_session()
    try:
        # DELETE all existing items (cascades to demand, supply, buffers)
        session.query(Item).delete()
        session.flush()

        # INSERT fresh data from file
        for d in new_items:
            uom = d["unit_of_measure"] if d["unit_of_measure"] in ["EA","KG","LT","M","MT","PC"] else "EA"
            session.add(Item(
                part_number=d["part_number"], description=d["description"],
                category=d["category"], unit_of_measure=uom,
                adu=d["adu"], dlt=d["dlt"],
                lead_time_factor=d["lead_time_factor"],
                variability_factor=d["variability_factor"],
                min_order_qty=d["min_order_qty"],
                order_cycle=d["order_cycle"],
                on_hand=d["on_hand"],
            ))
            success += 1

        session.commit()
    except Exception as e:
        session.rollback()
        errors.append(f"Database error: {e}")
        success = 0
    finally:
        session.close()

    return success, errors


# ---------------------------------------------------------------------------
# Demand template + importer
# ---------------------------------------------------------------------------

DEMAND_HEADERS = [
    {"name": "Part Number *",   "width": 16},
    {"name": "Demand Type *",   "width": 14},
    {"name": "Quantity *",      "width": 12},
    {"name": "Demand Date *",   "width": 16},
    {"name": "Order Reference", "width": 18},
    {"name": "Notes",           "width": 28},
]

DEMAND_EXAMPLE = ["RM-001", "actual", 50.0, "2026-04-01", "SO-12345", "Optional notes"]


def build_demand_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Demand"

    _add_instructions(ws,
        "Instructions: Fill from row 4 onward. Demand Type: 'actual' or 'forecast'. "
        "Date format: YYYY-MM-DD. Part Number must exist in Material Master.",
        row=1, col_span=len(DEMAND_HEADERS))
    _write_header(ws, DEMAND_HEADERS, row=2)
    _write_example_row(ws, DEMAND_EXAMPLE, row=3)

    dv = DataValidation(type="list", formula1='"actual,forecast"', allow_blank=False)
    ws.add_data_validation(dv)
    dv.sqref = "B4:B1000"

    ws.freeze_panes = "A4"
    return _wb_to_bytes(wb)


def import_demand(uploaded_file) -> tuple[int, list[str]]:
    from database.db import get_session, Item, DemandEntry

    try:
        df = pd.read_excel(uploaded_file, header=1, skiprows=[2])
    except Exception as e:
        return 0, [f"Could not read file: {e}"]

    df.columns = [c.replace(" *", "").strip() for c in df.columns]
    df = df.dropna(how="all")

    errors = []
    new_entries = []

    # --- Pre-validate ---
    session = get_session()
    try:
        item_map = {it.part_number: it.id for it in session.query(Item).all()}
    finally:
        session.close()

    for idx, row in df.iterrows():
        row_num = idx + 4
        part = str(row.get("Part Number", "")).strip().upper()
        if not part or part == "NAN":
            continue
        if part not in item_map:
            errors.append(f"Row {row_num}: Part '{part}' not found in Material Master.")
            continue
        try:
            qty   = float(row.get("Quantity", 0))
            dtype = str(row.get("Demand Type", "actual")).strip().lower()
            if dtype not in ("actual", "forecast"):
                dtype = "actual"
            raw_date = row.get("Demand Date")
            if pd.isna(raw_date):
                errors.append(f"Row {row_num} ({part}): Demand Date is required.")
                continue
            new_entries.append(dict(
                item_id=item_map[part],
                demand_type=dtype,
                quantity=qty,
                demand_date=pd.to_datetime(raw_date).to_pydatetime(),
                order_reference=str(row.get("Order Reference", "") or "").strip(),
                notes=str(row.get("Notes", "") or "").strip(),
            ))
        except Exception as e:
            errors.append(f"Row {row_num} ({part}): {e}")

    if not new_entries:
        return 0, errors + ["No valid rows found — existing data was NOT deleted."]

    session = get_session()
    try:
        # DELETE all existing demand entries
        session.query(DemandEntry).delete()
        session.flush()

        # INSERT fresh rows
        for d in new_entries:
            session.add(DemandEntry(**d))

        session.commit()
        return len(new_entries), errors
    except Exception as e:
        session.rollback()
        return 0, errors + [f"Database error: {e}"]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Supply template + importer
# ---------------------------------------------------------------------------

SUPPLY_HEADERS = [
    {"name": "Part Number *",   "width": 16},
    {"name": "Supply Type *",   "width": 20},
    {"name": "Quantity *",      "width": 12},
    {"name": "Due Date *",      "width": 16},
    {"name": "Order Reference", "width": 18},
    {"name": "Notes",           "width": 28},
]

SUPPLY_EXAMPLE = ["RM-001", "purchase_order", 200.0, "2026-04-10", "PO-9876", ""]


def build_supply_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Supply"

    _add_instructions(ws,
        "Instructions: Fill from row 4 onward. Supply Type: 'purchase_order' or 'production_order'. "
        "Date format: YYYY-MM-DD. Part Number must exist in Material Master.",
        row=1, col_span=len(SUPPLY_HEADERS))
    _write_header(ws, SUPPLY_HEADERS, row=2)
    _write_example_row(ws, SUPPLY_EXAMPLE, row=3)

    dv = DataValidation(type="list",
                        formula1='"purchase_order,production_order"',
                        allow_blank=False)
    ws.add_data_validation(dv)
    dv.sqref = "B4:B1000"

    ws.freeze_panes = "A4"
    return _wb_to_bytes(wb)


def import_supply(uploaded_file) -> tuple[int, list[str]]:
    from database.db import get_session, Item, SupplyEntry

    try:
        df = pd.read_excel(uploaded_file, header=1, skiprows=[2])
    except Exception as e:
        return 0, [f"Could not read file: {e}"]

    df.columns = [c.replace(" *", "").strip() for c in df.columns]
    df = df.dropna(how="all")

    errors = []
    new_entries = []

    # --- Pre-validate ---
    session = get_session()
    try:
        item_map = {it.part_number: it.id for it in session.query(Item).all()}
    finally:
        session.close()

    for idx, row in df.iterrows():
        row_num = idx + 4
        part = str(row.get("Part Number", "")).strip().upper()
        if not part or part == "NAN":
            continue
        if part not in item_map:
            errors.append(f"Row {row_num}: Part '{part}' not found in Material Master.")
            continue
        try:
            qty   = float(row.get("Quantity", 0))
            stype = str(row.get("Supply Type", "purchase_order")).strip().lower()
            if stype not in ("purchase_order", "production_order"):
                stype = "purchase_order"
            raw_date = row.get("Due Date")
            if pd.isna(raw_date):
                errors.append(f"Row {row_num} ({part}): Due Date is required.")
                continue
            new_entries.append(dict(
                item_id=item_map[part],
                supply_type=stype,
                quantity=qty,
                due_date=pd.to_datetime(raw_date).to_pydatetime(),
                order_reference=str(row.get("Order Reference", "") or "").strip(),
                notes=str(row.get("Notes", "") or "").strip(),
            ))
        except Exception as e:
            errors.append(f"Row {row_num} ({part}): {e}")

    if not new_entries:
        return 0, errors + ["No valid rows found — existing data was NOT deleted."]

    session = get_session()
    try:
        # DELETE all existing supply entries
        session.query(SupplyEntry).delete()
        session.flush()

        # INSERT fresh rows
        for d in new_entries:
            session.add(SupplyEntry(**d))

        session.commit()
        return len(new_entries), errors
    except Exception as e:
        session.rollback()
        return 0, errors + [f"Database error: {e}"]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Process Nodes template + importer
# ---------------------------------------------------------------------------

PROCESS_NODE_HEADERS = [
    {"name": "Process Name *",      "width": 20},
    {"name": "Sequence *",          "width": 12},
    {"name": "Node Label *",        "width": 24},
    {"name": "Node Type *",         "width": 14},
    {"name": "Has Buffer (YES/NO)", "width": 18},
    {"name": "Linked Part Number",  "width": 20},
]

PROCESS_NODE_EXAMPLE = ["Assembly Line A", 1, "Cutting", "operation", "NO", "RM-001"]


def build_process_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Process Nodes"

    _add_instructions(ws,
        "Instructions: Fill from row 4 onward. Process Name must match an existing process "
        "(or a new one will be created). Node Type: operation / material / buffer. "
        "Has Buffer: YES or NO. Linked Part Number is optional.",
        row=1, col_span=len(PROCESS_NODE_HEADERS))
    _write_header(ws, PROCESS_NODE_HEADERS, row=2)
    _write_example_row(ws, PROCESS_NODE_EXAMPLE, row=3)

    dv_type = DataValidation(type="list", formula1='"operation,material,buffer"', allow_blank=False)
    ws.add_data_validation(dv_type)
    dv_type.sqref = "D4:D1000"

    dv_buf = DataValidation(type="list", formula1='"YES,NO"', allow_blank=False)
    ws.add_data_validation(dv_buf)
    dv_buf.sqref = "E4:E1000"

    ws.freeze_panes = "A4"
    return _wb_to_bytes(wb)


def import_process_nodes(uploaded_file) -> tuple[int, list[str]]:
    from database.db import get_session, Item, Process, ProcessNode, ProcessEdge

    try:
        df = pd.read_excel(uploaded_file, header=1, skiprows=[2])
    except Exception as e:
        return 0, [f"Could not read file: {e}"]

    df.columns = [c.replace(" *", "").strip() for c in df.columns]
    df = df.dropna(how="all")

    errors = []
    new_nodes = []

    # --- Pre-validate ---
    session = get_session()
    try:
        item_map = {it.part_number: it.id for it in session.query(Item).all()}
    finally:
        session.close()

    # Collect process names referenced in the file
    referenced_processes = set()
    for idx, row in df.iterrows():
        row_num = idx + 4
        proc_name = str(row.get("Process Name", "")).strip()
        label     = str(row.get("Node Label", "")).strip()
        if not proc_name or proc_name == "nan":
            continue
        if not label or label == "nan":
            errors.append(f"Row {row_num}: Node Label is required.")
            continue
        try:
            seq     = int(row.get("Sequence", 0) or 0)
            ntype   = str(row.get("Node Type", "operation") or "operation").strip().lower()
            if ntype not in ("operation", "material", "buffer"):
                ntype = "operation"
            has_buf = str(row.get("Has Buffer (YES/NO)", "NO")).strip().upper() == "YES"
            part_raw= str(row.get("Linked Part Number", "") or "").strip().upper()
        except Exception as e:
            errors.append(f"Row {row_num}: {e}")
            continue

        item_id = None
        if part_raw and part_raw != "NAN":
            if part_raw in item_map:
                item_id = item_map[part_raw]
            else:
                errors.append(f"Row {row_num}: Part '{part_raw}' not found — node will have no item link.")

        referenced_processes.add(proc_name)
        new_nodes.append(dict(
            proc_name=proc_name, label=label, node_type=ntype,
            has_buffer=has_buf, sequence=seq, item_id=item_id,
        ))

    if not new_nodes:
        return 0, errors + ["No valid rows found — existing data was NOT deleted."]

    session = get_session()
    try:
        # DELETE all nodes and edges for processes referenced in the file
        # (edges cascade-delete via ProcessNode relationship)
        for proc_name in referenced_processes:
            proc = session.query(Process).filter_by(name=proc_name).first()
            if proc:
                # Delete edges first to avoid FK issues
                session.query(ProcessEdge).filter_by(process_id=proc.id).delete()
                session.query(ProcessNode).filter_by(process_id=proc.id).delete()
                session.flush()

        # Re-fetch or create processes after deletion
        for node in new_nodes:
            proc = session.query(Process).filter_by(name=node["proc_name"]).first()
            if not proc:
                proc = Process(name=node["proc_name"])
                session.add(proc)
                session.flush()
            node["process_id"] = proc.id

        # INSERT fresh nodes
        for node in new_nodes:
            session.add(ProcessNode(
                process_id=node["process_id"],
                item_id=node["item_id"],
                label=node["label"],
                node_type=node["node_type"],
                has_buffer=node["has_buffer"],
                sequence=node["sequence"],
            ))

        session.commit()
        return len(new_nodes), errors
    except Exception as e:
        session.rollback()
        return 0, errors + [f"Database error: {e}"]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Shared Streamlit UI widget (reusable across pages)
# ---------------------------------------------------------------------------

def render_import_widget(
    label: str,
    template_fn,
    import_fn,
    template_filename: str,
    key: str,
):
    """
    Renders a collapsible import section with:
      1. Download template button
      2. Warning that existing data will be fully replaced
      3. Confirmation checkbox
      4. File uploader
      5. Import button with result feedback
    """
    import streamlit as st

    with st.expander(f"⬆️ Import {label} from Excel", expanded=False):

        # Step 1 — Download template
        st.markdown("**Step 1 — Download the template**")
        st.download_button(
            label=f"⬇️ Download {label} Template",
            data=template_fn(),
            file_name=template_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_{key}",
        )
        st.caption("Fill in your data from row 4 onward (row 3 is a pre-filled example — do not delete it).")

        st.divider()

        # Step 2 — Warning + confirmation
        st.markdown("**Step 2 — Confirm replacement**")
        st.warning(
            f"⚠️ **Importing will permanently DELETE all existing {label} data "
            f"and replace it with the content of your file.** "
            f"This action cannot be undone.",
            icon="⚠️",
        )
        confirmed = st.checkbox(
            f"I understand — replace all existing {label} data with the imported file",
            key=f"confirm_{key}",
            value=False,
        )

        st.divider()

        # Step 3 — Upload and import
        st.markdown("**Step 3 — Upload your filled template**")
        uploaded = st.file_uploader(
            f"Upload filled {label} template (.xlsx)",
            type=["xlsx"],
            key=f"upload_{key}",
        )

        if uploaded:
            if not confirmed:
                st.info("Please tick the confirmation checkbox above to enable the import button.")
            else:
                if st.button(
                    f"🔄 Replace all {label} data with this file",
                    type="primary",
                    key=f"import_btn_{key}",
                    use_container_width=True,
                ):
                    with st.spinner(f"Deleting existing {label} data and importing new data..."):
                        count, errs = import_fn(uploaded)

                    if count:
                        st.success(f"✅ {count} row(s) imported successfully. All previous {label} data has been replaced.")
                    if errs:
                        st.warning(f"{len(errs)} row(s) had issues (these rows were skipped):")
                        for e in errs:
                            st.caption(f"  • {e}")
                    if count:
                        # Reset confirmation so user must re-tick for next import
                        st.rerun()
