"""
Prioritized Share Allocator — Streamlit page (deck slides 98-107).

When a constraint exists (truck capacity, minimum spend, budget), DDMRP
recommends allocating the available supply across competing replenishment
needs by NFP penetration into the buffer, not by FIFO or equal-share.

Two optimisation modes (slide 100-107):
  • Coverage optimisation  — fill items in order of lowest NFP/TOG ratio
    (most penetrated first) until the constraint is exhausted.
  • Discount optimisation  — fill to reach a price break / full-truck by
    choosing the combination that maximises coverage within the constraint.

The allocator uses the current NFP from the buffers table plus item-level
unit_cost and min_order_qty.  Results show which items get a full order,
which get a partial, and what residual constraint remains.
"""

from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dataclasses import dataclass, field
from typing import Optional

from database.db import get_session, Item, Buffer
from database.auth import get_company_id


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AllocationLine:
    """Allocation decision for one item."""
    item_id:        int
    part_number:    str
    description:    str
    nfp:            float
    tog:            float
    tor:            float
    unit_cost:      float
    moq:            float
    penetration:    float       # NFP / TOG  (lower = more urgent, 0 = at TOR)
    needed_qty:     float       # TOG - NFP  (what's needed to reach TOG)
    allocated_qty:  float       # what we're actually ordering
    allocated_cost: float
    status:         str         # "full" | "partial" | "skipped"
    note:           str = ""


@dataclass
class AllocationResult:
    """Complete allocation plan."""
    mode:               str             # "coverage" | "discount"
    constraint_type:    str             # "units" | "eur"
    constraint_value:   float
    constraint_used:    float
    constraint_residual: float
    lines:              list[AllocationLine] = field(default_factory=list)

    @property
    def total_allocated_qty(self) -> float:
        return sum(l.allocated_qty for l in self.lines)

    @property
    def total_cost(self) -> float:
        return sum(l.allocated_cost for l in self.lines)

    @property
    def items_filled(self) -> int:
        return sum(1 for l in self.lines if l.status == "full")

    @property
    def items_partial(self) -> int:
        return sum(1 for l in self.lines if l.status == "partial")


# ---------------------------------------------------------------------------
# Allocation engine
# ---------------------------------------------------------------------------

def _load_candidates() -> list[AllocationLine]:
    """Load all items that need replenishment (NFP ≤ TOY)."""
    session = get_session()
    try:
        items   = {it.id: it for it in session.query(Item).filter(Item.company_id == get_company_id()).all()}
        buffers = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    candidates = []
    for iid, it in items.items():
        buf = buffers.get(iid)
        if buf is None:
            continue
        tog = buf.top_of_green or 0.0
        tor = buf.top_of_red   or 0.0
        toy = buf.top_of_yellow or 0.0
        nfp = buf.net_flow_position or 0.0

        if nfp > toy or tog <= 0:
            continue   # already green, no action needed

        needed = max(0.0, tog - nfp)
        moq    = it.min_order_qty or 0.0
        needed = max(needed, moq)
        unit_c = it.unit_cost or 0.0
        penetration = (nfp / tog) if tog > 0 else 0.0

        candidates.append(AllocationLine(
            item_id=iid,
            part_number=it.part_number,
            description=it.description,
            nfp=nfp,
            tog=tog,
            tor=tor,
            unit_cost=unit_c,
            moq=moq,
            penetration=penetration,
            needed_qty=needed,
            allocated_qty=0.0,
            allocated_cost=0.0,
            status="skipped",
        ))

    # Sort by penetration ascending (most penetrated = lowest NFP/TOG first)
    return sorted(candidates, key=lambda c: c.penetration)


def allocate_coverage(
    constraint_type: str,   # "units" | "eur"
    constraint_value: float,
) -> AllocationResult:
    """
    Coverage optimisation (slide 102):
    Fill items from most-penetrated to least, each getting TOG–NFP,
    until the constraint is exhausted.  Partial fills are allowed.
    """
    candidates = _load_candidates()
    remaining  = constraint_value
    lines      = []

    for c in candidates:
        if remaining <= 0:
            lines.append(c)
            continue

        if constraint_type == "eur":
            if c.unit_cost <= 0:
                # No cost known — give full order if units remain
                c.allocated_qty  = c.needed_qty
                c.allocated_cost = 0.0
                c.status         = "full"
            else:
                max_by_cost = remaining / c.unit_cost
                if max_by_cost >= c.needed_qty:
                    c.allocated_qty  = c.needed_qty
                    c.allocated_cost = c.needed_qty * c.unit_cost
                    c.status         = "full"
                    remaining       -= c.allocated_cost
                else:
                    # Partial — respect MOQ floor
                    alloc = max(0.0, max_by_cost)
                    if c.moq > 0 and alloc < c.moq:
                        c.status = "skipped"
                        c.note   = f"Budget too small for MOQ ({c.moq:.0f} units)"
                    else:
                        c.allocated_qty  = round(alloc, 2)
                        c.allocated_cost = c.allocated_qty * c.unit_cost
                        c.status         = "partial"
                        remaining       -= c.allocated_cost
        else:  # "units"
            if remaining >= c.needed_qty:
                c.allocated_qty  = c.needed_qty
                c.allocated_cost = c.needed_qty * c.unit_cost
                c.status         = "full"
                remaining       -= c.needed_qty
            else:
                alloc = remaining
                if c.moq > 0 and alloc < c.moq:
                    c.status = "skipped"
                    c.note   = f"Remaining units ({alloc:.0f}) < MOQ ({c.moq:.0f})"
                else:
                    c.allocated_qty  = round(alloc, 2)
                    c.allocated_cost = c.allocated_qty * c.unit_cost
                    c.status         = "partial"
                    remaining        = 0

        lines.append(c)

    used = constraint_value - max(remaining, 0)
    return AllocationResult(
        mode="coverage",
        constraint_type=constraint_type,
        constraint_value=constraint_value,
        constraint_used=used,
        constraint_residual=max(remaining, 0),
        lines=lines,
    )


def allocate_discount(
    target_value: float,    # target spend / units to hit the price break
    constraint_type: str,   # "units" | "eur"
) -> AllocationResult:
    """
    Discount/truck optimisation (slide 106):
    First fill all urgent (red) items fully; then top up from most-penetrated
    yellow items until we hit the target spend / unit count.
    Any residual goes to the next most-penetrated item even if it's green.
    """
    candidates = _load_candidates()

    # Also add green items so we can top up the truck
    session = get_session()
    try:
        all_items   = {it.id: it for it in session.query(Item).filter(Item.company_id == get_company_id()).all()}
        all_buffers = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    green_tops: list[AllocationLine] = []
    needed_ids = {c.item_id for c in candidates}
    for iid, it in all_items.items():
        if iid in needed_ids:
            continue
        buf = all_buffers.get(iid)
        if not buf:
            continue
        tog  = buf.top_of_green or 0.0
        nfp  = buf.net_flow_position or 0.0
        pct  = (nfp / tog) if tog > 0 else 1.0
        green_tops.append(AllocationLine(
            item_id=iid, part_number=it.part_number,
            description=it.description, nfp=nfp,
            tog=tog, tor=buf.top_of_red or 0.0,
            unit_cost=it.unit_cost or 0.0, moq=it.min_order_qty or 0.0,
            penetration=pct, needed_qty=max(0, tog - nfp),
            allocated_qty=0.0, allocated_cost=0.0, status="skipped",
        ))
    # Sort green top-ups by penetration (lowest first = most need for uplift)
    green_tops.sort(key=lambda c: c.penetration)

    ordered = candidates + green_tops  # red/yellow first, then green top-ups
    remaining = target_value
    lines = []

    for c in ordered:
        if remaining <= 0:
            lines.append(c)
            continue
        if c.needed_qty <= 0:
            lines.append(c)
            continue

        if constraint_type == "eur":
            budget_for_item = min(remaining, c.needed_qty * max(c.unit_cost, 0))
            alloc_qty = (budget_for_item / c.unit_cost) if c.unit_cost > 0 else c.needed_qty
        else:
            alloc_qty = min(remaining, c.needed_qty)

        alloc_qty = round(alloc_qty, 2)
        if c.moq > 0 and alloc_qty > 0 and alloc_qty < c.moq:
            alloc_qty = c.moq  # round up to MOQ

        c.allocated_qty  = alloc_qty
        c.allocated_cost = alloc_qty * c.unit_cost
        c.status         = "full" if abs(alloc_qty - c.needed_qty) < 0.01 else "partial"
        if c.item_id not in needed_ids:
            c.note = "Green top-up (truck fill)"
        remaining -= (c.allocated_cost if constraint_type == "eur" else alloc_qty)
        lines.append(c)

    used = target_value - max(remaining, 0)
    return AllocationResult(
        mode="discount",
        constraint_type=constraint_type,
        constraint_value=target_value,
        constraint_used=used,
        constraint_residual=max(remaining, 0),
        lines=lines,
    )


# ---------------------------------------------------------------------------
# Streamlit page
# ---------------------------------------------------------------------------

STATUS_BG = {
    "full":    "#D5F5E3",
    "partial": "#FDEBD0",
    "skipped": "#F2F3F4",
}
STATUS_EMOJI = {
    "full":    "✅",
    "partial": "⚡",
    "skipped": "—",
}


def show():
    st.header("Prioritized Share Allocator")
    st.caption(
        "DDMRP allocation of a constrained replenishment order "
        "(deck slides 98-107). Distributes available supply across competing "
        "buffer needs by NFP penetration, not FIFO."
    )

    session = get_session()
    try:
        has_buffers = session.query(Buffer).count() > 0
    finally:
        session.close()

    if not has_buffers:
        st.info("Run buffer calculations first (Replenishment Signals page).")
        return

    tab_cov, tab_disc = st.tabs([
        "📊 Coverage Optimisation",
        "🚚 Discount / Truck Fill",
    ])

    with tab_cov:
        _coverage_ui()

    with tab_disc:
        _discount_ui()


def _coverage_ui():
    st.subheader("Coverage Optimisation")
    st.caption(
        "Fill items from most-penetrated to least until the constraint "
        "(budget or available units) is exhausted."
    )

    col1, col2 = st.columns(2)
    with col1:
        ctype = st.radio("Constraint type", ["Budget (€)", "Available units"],
                         key="cov_ctype", horizontal=True)
    with col2:
        cval = st.number_input(
            "Constraint value",
            min_value=0.0, value=10000.0, step=500.0, key="cov_cval",
        )

    if st.button("▶  Run Coverage Allocation", type="primary", key="cov_run"):
        ct = "eur" if "€" in ctype else "units"
        result = allocate_coverage(ct, cval)
        st.session_state["cov_result"] = result

    result: Optional[AllocationResult] = st.session_state.get("cov_result")
    if result:
        _render_result(result)


def _discount_ui():
    st.subheader("Discount / Truck-Fill Optimisation")
    st.caption(
        "Reach a target spend or unit count (price-break / full truck) "
        "by filling red/yellow items first, then topping up with green items."
    )

    col1, col2 = st.columns(2)
    with col1:
        ctype = st.radio("Target type", ["Target spend (€)", "Target units"],
                         key="disc_ctype", horizontal=True)
    with col2:
        cval = st.number_input(
            "Target value",
            min_value=0.0, value=20000.0, step=500.0, key="disc_cval",
        )

    if st.button("▶  Run Discount Allocation", type="primary", key="disc_run"):
        ct = "eur" if "€" in ctype else "units"
        result = allocate_discount(cval, ct)
        st.session_state["disc_result"] = result

    result: Optional[AllocationResult] = st.session_state.get("disc_result")
    if result:
        _render_result(result)


def _render_result(result: AllocationResult):
    st.divider()

    # Summary KPIs
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Items Needing Order", len([l for l in result.lines if l.penetration < 1.0]))
    c2.metric("✅ Fully Filled",    result.items_filled)
    c3.metric("⚡ Partial",          result.items_partial)
    c4.metric("Total Qty Ordered",   f"{result.total_allocated_qty:,.0f}")
    c5.metric("Total Cost (€)",      f"€ {result.total_cost:,.0f}")

    constraint_label = "Budget" if result.constraint_type == "eur" else "Units"
    st.progress(
        min(1.0, result.constraint_used / result.constraint_value)
        if result.constraint_value > 0 else 0.0,
        text=f"{constraint_label} used: {result.constraint_used:,.0f} / {result.constraint_value:,.0f}"
             f"  (residual: {result.constraint_residual:,.0f})",
    )

    # Detail table
    rows = []
    for l in result.lines:
        rows.append({
            "Status":      f"{STATUS_EMOJI.get(l.status, '—')} {l.status.capitalize()}",
            "Part Number": l.part_number,
            "Description": l.description[:35],
            "NFP":         round(l.nfp, 1),
            "TOG":         round(l.tog, 1),
            "Penetration": f"{l.penetration*100:.0f}%",
            "Needed Qty":  round(l.needed_qty, 1),
            "Allocated":   round(l.allocated_qty, 1),
            "Cost (€)":    f"€ {l.allocated_cost:,.0f}" if l.allocated_cost else "—",
            "Note":        l.note or "",
        })

    df = pd.DataFrame(rows)

    def _sty(row):
        key = row["Status"].split()[-1].lower()
        bg  = STATUS_BG.get(key, "#FFFFFF")
        return [f"background-color: {bg}; color: #1A1A1A"] * len(row)

    st.dataframe(df.style.apply(_sty, axis=1), use_container_width=True, hide_index=True)

    # Waterfall chart — allocated qty per item
    allocated = [l for l in result.lines if l.allocated_qty > 0]
    if allocated:
        fig = go.Figure(go.Bar(
            x=[l.part_number for l in allocated],
            y=[l.allocated_qty for l in allocated],
            marker_color=["#2ECC71" if l.status == "full" else "#F39C12"
                          for l in allocated],
            text=[f"{l.allocated_qty:.0f}" for l in allocated],
            textposition="outside",
        ))
        fig.update_layout(
            height=320,
            xaxis_title="Item",
            yaxis_title="Allocated Qty",
            margin=dict(t=20, b=40, l=10, r=10),
            plot_bgcolor="white",
        )
        st.plotly_chart(fig, use_container_width=True)
