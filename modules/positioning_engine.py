"""
DDMRP Strategic Buffer Positioning Engine.

Implements the 6-factor positioning model from the DDMRP book (Ptak & Smith,
Chapter 6 "Strategic Inventory Positioning") and the Italian deck slides
30-38: given an item's positioning attributes (CTT, MPLT, SOVH, variability,
leverage, critical-operation flag), score how strongly the theory recommends
turning that item into a decoupling point (DDMRP-buffered) vs leaving it in
standard MRP.

Output:
  - per-factor 0-10 scores
  - total 0-70 score
  - recommended MRP type ("DDMRP" or "MRP") given a threshold
  - benefit comparison: cumulative chain LT vs decoupled LT, inventory €
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from database.db import get_session, Item, Buffer, BomLine


# ── Scoring constants ─────────────────────────────────────────────────────

VARIABILITY_SCORES = {"low": 0, "medium": 5, "high": 10}

# Default threshold to recommend DDMRP (out of 70 max). User-tunable in UI.
DEFAULT_THRESHOLD = 30

# Each of the 6 factors has a max of 10; the demand+supply variability pair
# counts as a single "external variability" criterion in the DDMRP book but
# we expose them separately so the user sees both contributions.
MAX_SCORE = 70  # 7 sub-factors × 10


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class PositioningResult:
    """Per-item output of `score_positioning()`."""
    item_id:           int
    part_number:       str
    description:       str
    cumulative_lt:     float            # full upstream chain LT (days)
    decoupled_lt:      float            # item.dlt (decoupled LT)
    scores:            dict             # {factor_name: 0-10}
    total_score:       int
    max_score:         int = MAX_SCORE
    incomplete_fields: list = field(default_factory=list)
    recommended_type:  str = "MRP"      # 'DDMRP' | 'MRP' | 'INCOMPLETE'
    override_type:     str = "auto"     # 'auto' | 'DDMRP' | 'MRP'
    effective_type:    str = "MRP"      # final type after applying override
    reasons:           list = field(default_factory=list)
    # Benefits (vs standard MRP)
    lt_reduction_days: float = 0.0
    lt_reduction_pct:  float = 0.0
    mrp_inv_value:     float = 0.0
    ddmrp_inv_value:   float = 0.0
    value_saving:      float = 0.0


# ── Cumulative LT (full chain, ignoring buffers) ──────────────────────────

def compute_cumulative_lt(
    item: Item,
    bom_map: Optional[dict] = None,
    _visited: Optional[set] = None,
) -> float:
    """
    Cumulative LT = longest path from `item` all the way to the raw materials
    (leaf nodes), WITHOUT respecting buffers. This is what an item's effective
    lead time would be under standard MRP (no decoupling).

    Compare with `bom_engine.compute_dlt()`, which stops at the first buffered
    child upstream and represents the DDMRP-Decoupled Lead Time.
    """
    if bom_map is None:
        session = get_session()
        try:
            lines = session.query(BomLine).all()
        finally:
            session.close()
        bom_map = {}
        for line in lines:
            bom_map.setdefault(line.parent_item_id, []).append(line)

    if _visited is None:
        _visited = set()
    if item.id in _visited:
        return float(item.dlt or 0.0)
    _visited = _visited | {item.id}

    own_lt = float(item.dlt or 0.0)
    children = bom_map.get(item.id, [])
    if not children:
        return own_lt

    # Load the actual child items once for this level
    session = get_session()
    try:
        child_items = {
            it.id: it for it in session.query(Item).filter(
                Item.id.in_([l.child_item_id for l in children])
            ).all()
        }
    finally:
        session.close()

    best_child = 0.0
    for line in children:
        child = child_items.get(line.child_item_id)
        if child is None:
            continue
        c = compute_cumulative_lt(child, bom_map, _visited)
        if c > best_child:
            best_child = c
    return own_lt + best_child


# ── Per-factor scoring ────────────────────────────────────────────────────

def _ratio_score(target: Optional[float], cumulative_lt: float) -> int:
    """
    For factors expressed as "should decouple if `target` is shorter than the
    full chain LT", return 10/5/0:
      ratio < 0.5  → 10   (strong reason — chain is more than 2× target)
      ratio < 1.0  → 5    (moderate — chain exceeds target)
      ratio >= 1.0 → 0    (no pressure to decouple)
    """
    if target is None or cumulative_lt <= 0:
        return 0
    ratio = target / cumulative_lt
    if ratio < 0.5:
        return 10
    if ratio < 1.0:
        return 5
    return 0


def score_positioning(
    item: Item,
    cumulative_lt: float,
    threshold: int = DEFAULT_THRESHOLD,
) -> PositioningResult:
    """
    Compute per-factor scores and the recommended MRP type for one item.
    `cumulative_lt` must be computed upstream via `compute_cumulative_lt()`.
    """
    scores: dict = {}
    incomplete: list = []

    # 1. Customer Tolerance Time — decouple if CTT < cumulative LT
    if item.customer_tolerance_time is None:
        incomplete.append("Customer Tolerance Time")
        scores["Customer Tolerance Time"] = 0
    else:
        scores["Customer Tolerance Time"] = _ratio_score(
            float(item.customer_tolerance_time), cumulative_lt)

    # 2. Market Potential Lead Time — decouple if a shorter quoted LT wins business
    if item.market_potential_lt is None:
        incomplete.append("Market Potential LT")
        scores["Market Potential LT"] = 0
    else:
        scores["Market Potential LT"] = _ratio_score(
            float(item.market_potential_lt), cumulative_lt)

    # 3. Sales Order Visibility Horizon — decouple if SOVH is short vs chain LT
    if item.order_visibility_horizon is None:
        incomplete.append("Sales Order Visibility")
        scores["Sales Order Visibility"] = 0
    else:
        scores["Sales Order Visibility"] = _ratio_score(
            float(item.order_visibility_horizon), cumulative_lt)

    # 4a. Demand Variability — high variability favours decoupling
    if not item.demand_variability_score:
        incomplete.append("Demand Variability")
        scores["Demand Variability"] = 0
    else:
        scores["Demand Variability"] = VARIABILITY_SCORES.get(
            item.demand_variability_score.lower(), 0)

    # 4b. Supply Variability
    if not item.supply_variability_score:
        incomplete.append("Supply Variability")
        scores["Supply Variability"] = 0
    else:
        scores["Supply Variability"] = VARIABILITY_SCORES.get(
            item.supply_variability_score.lower(), 0)

    # 5. Inventory Leverage & Flexibility — common components → buffer here
    if not item.inventory_leverage_score:
        incomplete.append("Inventory Leverage")
        scores["Inventory Leverage"] = 0
    else:
        scores["Inventory Leverage"] = VARIABILITY_SCORES.get(
            item.inventory_leverage_score.lower(), 0)

    # 6. Critical Operation Protection
    if item.critical_operation is None:
        incomplete.append("Critical Operation")
        scores["Critical Operation"] = 0
    else:
        scores["Critical Operation"] = 10 if item.critical_operation else 0

    total = sum(scores.values())

    # Determine recommended type
    if incomplete:
        recommended = "INCOMPLETE"
    elif total >= threshold:
        recommended = "DDMRP"
    else:
        recommended = "MRP"

    # Apply manual override (None/'auto' = follow recommendation)
    override = item.mrp_type_override or "auto"
    if override in ("DDMRP", "MRP"):
        effective = override
    else:
        effective = recommended if recommended != "INCOMPLETE" else "MRP"

    # Identify the high-scoring factors as "reasons"
    reasons = [factor for factor, sc in scores.items() if sc >= 5]

    decoupled_lt = float(item.dlt or 0.0)

    # ── Benefits vs standard MRP ──────────────────────────────────────────
    lt_reduction = max(0.0, cumulative_lt - decoupled_lt)
    lt_reduction_pct = (lt_reduction / cumulative_lt) if cumulative_lt > 0 else 0.0

    adu       = float(item.adu or 0.0)
    unit_cost = float(item.unit_cost or 0.0)

    # Standard-MRP pipeline-stock proxy: adu × cumulative_lt × unit_cost
    # (rough order-of-magnitude — assumes one full chain's worth of WIP/stock
    # is held without any decoupling buffer to absorb variability).
    mrp_inv_value = adu * cumulative_lt * unit_cost

    # DDMRP inventory held = Avg Inv Target × unit_cost
    # Compute Avg Inv Target inline (Red + Green/2) using current params:
    #   Red    = adu × dlt × ltf × (1 + vf)
    #   Green  = max(adu × order_cycle, MOQ, Red base)
    ltf = float(item.lead_time_factor or 0.5)
    vf  = float(item.variability_factor or 0.5)
    moq = float(item.min_order_qty or 0.0)
    oc  = float(item.order_cycle or 0.0)
    red_base   = adu * decoupled_lt * ltf
    red_safety = red_base * vf
    red_zone   = red_base + red_safety
    green_zone = max(adu * oc, moq, red_base) if any(v > 0 for v in (adu*oc, moq, red_base)) else 0.0
    avg_inv_qty = red_zone + (green_zone / 2.0)
    ddmrp_inv_value = avg_inv_qty * unit_cost

    value_saving = max(0.0, mrp_inv_value - ddmrp_inv_value)

    return PositioningResult(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description,
        cumulative_lt=cumulative_lt,
        decoupled_lt=decoupled_lt,
        scores=scores,
        total_score=int(total),
        incomplete_fields=incomplete,
        recommended_type=recommended,
        override_type=override,
        effective_type=effective,
        reasons=reasons,
        lt_reduction_days=lt_reduction,
        lt_reduction_pct=lt_reduction_pct,
        mrp_inv_value=mrp_inv_value,
        ddmrp_inv_value=ddmrp_inv_value,
        value_saving=value_saving,
    )


# ── Portfolio-level analysis ──────────────────────────────────────────────

def analyze_all_items(
    company_id: int,
    threshold: int = DEFAULT_THRESHOLD,
) -> list[PositioningResult]:
    """Score every item belonging to `company_id`."""
    session = get_session()
    try:
        items = session.query(Item).filter(
            Item.company_id == company_id
        ).order_by(Item.part_number).all()

        # Pre-load BOM once for all items
        lines = session.query(BomLine).all()
        bom_map: dict = {}
        for line in lines:
            bom_map.setdefault(line.parent_item_id, []).append(line)
    finally:
        session.close()

    results: list[PositioningResult] = []
    for item in items:
        cum_lt = compute_cumulative_lt(item, bom_map=bom_map)
        res = score_positioning(item, cum_lt, threshold=threshold)
        results.append(res)
    return results


def apply_recommendation(item_id: int, mrp_type: str) -> None:
    """
    Persist the recommendation back to the item:
      mrp_type='DDMRP'  → mrp_type_override = 'DDMRP'
      mrp_type='MRP'    → mrp_type_override = 'MRP'
      mrp_type='auto'   → clear the override
    """
    if mrp_type not in ("DDMRP", "MRP", "auto"):
        raise ValueError(f"Invalid mrp_type: {mrp_type!r}")
    session = get_session()
    try:
        item = session.query(Item).get(item_id)
        if item is None:
            return
        item.mrp_type_override = mrp_type
        session.commit()
    finally:
        session.close()


_ASSIGNABLE_METHODS = {"ddmrp", "rop_q", "rop_ss", "kanban", "periodic_rs"}


def assign_methodologies(assignments: dict) -> int:
    """
    Persist accepted Methodology-Simulation recommendations back to the items.

    `assignments` maps item_id -> policy key (one of _ASSIGNABLE_METHODS, or ""
    to clear the assignment). Setting a policy also aligns the binary
    `mrp_type_override` so the rest of the app (positioning, buffers) sees the
    part flagged as DDMRP / MRP. Returns the number of items updated.
    """
    if not assignments:
        return 0
    session = get_session()
    updated = 0
    try:
        for item_id, policy in assignments.items():
            policy = (policy or "").lower()
            if policy and policy not in _ASSIGNABLE_METHODS:
                continue
            item = session.query(Item).get(int(item_id))
            if item is None:
                continue
            item.assigned_methodology = policy
            if policy:
                item.mrp_type_override = "DDMRP" if policy == "ddmrp" else "MRP"
            updated += 1
        session.commit()
    finally:
        session.close()
    return updated
