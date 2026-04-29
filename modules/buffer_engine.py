"""
DDMRP Buffer Calculation Engine
---------------------------------
Implements the full DDMRP buffer calculation methodology:
  - Red Zone (base + safety)
  - Yellow Zone
  - Green Zone
  - Net Flow Position (NFP)
  - Buffer status (green / yellow / red)
  - Suggested replenishment quantity
  - Dynamic Buffer Adjustment (DBA)
  - Forward projection: day-by-day NFP forecast with replenishment signal dates
"""

from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from database.db import (
    get_session, Buffer, Item, DemandEntry, SupplyEntry, Settings,
    BufferAdjustment,
)


# ---------------------------------------------------------------------------
# Data classes for calculation results (decoupled from ORM)
# ---------------------------------------------------------------------------

@dataclass
class BufferZones:
    """Calculated DDMRP buffer zones for one item."""
    item_id: int
    part_number: str

    # Zone sizes
    red_zone_base: float = 0.0
    red_zone_safety: float = 0.0
    red_zone: float = 0.0       # TOR (Top of Red)
    yellow_zone: float = 0.0
    green_zone: float = 0.0

    # Tops
    top_of_red: float = 0.0     # = red_zone
    top_of_yellow: float = 0.0  # = red + yellow
    top_of_green: float = 0.0   # = red + yellow + green

    # Inputs used
    adu: float = 0.0
    dlt: float = 0.0
    ltf: float = 0.5
    vf: float = 0.5
    min_order_qty: float = 0.0
    order_cycle: float = 0.0

    # Canonical DDMRP KPIs (deck slide 59 / 92)
    avg_inventory_target: float = 0.0    # Red + Green/2
    avg_order_frequency_days: float = 0.0  # Green / ADU
    safety_days: float = 0.0             # Red / ADU
    avg_active_orders: float = 0.0       # Yellow / Green


@dataclass
class BufferStatus:
    """Runtime status of a buffer (NFP vs zones)."""
    item_id: int
    part_number: str
    on_hand: float = 0.0
    on_order: float = 0.0
    qualified_demand: float = 0.0   # demand spikes within lead time
    net_flow_position: float = 0.0

    zones: Optional[BufferZones] = None

    status: str = "green"           # green | yellow | red — PLANNING colour (NFP-based)
    suggested_order_qty: float = 0.0
    reorder_needed: bool = False

    # Execution-side metrics (deck slides 109-118)
    buffer_status_pct: float = 0.0  # on_hand / TOR  (1.0 = 100%)
    execution_color: str = "green"  # over_tog | green | yellow | red | dark_red


# ---------------------------------------------------------------------------
# Execution colour bands (deck slide 109)
# ---------------------------------------------------------------------------

def execution_color_from_pct(pct: float) -> str:
    """
    Map Buffer Status % (on_hand / TOR) to one of 5 execution colour bands.

    Per the deck (slide 109):
      pct < 0       → dark_red  (negative on-hand / stockout)
      pct < 0.50    → red       (below half of TOR)
      pct < 1.00    → yellow    (between half-TOR and TOR)
      pct ≤ 1 + (Y+G)/TOR (i.e. pct equates OH ≤ TOG) → green
      otherwise     → over_tog  (excess inventory above TOG)
    """
    if pct < 0:
        return "dark_red"
    if pct < 0.50:
        return "red"
    if pct < 1.00:
        return "yellow"
    return "green"


def execution_color(on_hand: float, zones: BufferZones) -> tuple[str, float]:
    """
    Return (execution_color_band, buffer_status_pct) using the deck's 5-band logic.

    The over_tog band kicks in when on_hand exceeds TOG (excess inventory).
    """
    if zones is None or zones.top_of_red <= 0:
        return ("green", 0.0)
    pct = on_hand / zones.top_of_red

    if zones.top_of_green > 0 and on_hand > zones.top_of_green:
        return ("over_tog", pct)
    return (execution_color_from_pct(pct), pct)


# ---------------------------------------------------------------------------
# Core calculation functions
# ---------------------------------------------------------------------------

ADU_WINDOW_DAYS = 7   # weekly rolling window for dynamic ADU


# ---------------------------------------------------------------------------
# ASOH (Adjusted Spike Horizon) parameter resolution
# ---------------------------------------------------------------------------

def _get_global_settings() -> Tuple[int, float]:
    """Return (default_spike_horizon_days, default_spike_threshold_factor) from Settings."""
    session = get_session()
    try:
        s = session.query(Settings).first()
        if s is None:
            return (0, 2.0)
        return (
            int(s.default_spike_horizon_days or 0),
            float(s.default_spike_threshold_factor or 2.0),
        )
    finally:
        session.close()


def resolve_spike_params(item: Item) -> Tuple[int, float]:
    """
    Resolve effective (spike_horizon_days, spike_threshold_factor) for an item.

    Per the deck (slide 83), Spike Horizon and Spike Threshold are two distinct
    user-configurable parameters. The app uses item-level overrides if set,
    otherwise falls back to the global Settings defaults. If horizon is 0/None,
    it defaults to the item's DLT.
    """
    g_horizon, g_factor = _get_global_settings()

    horizon = item.spike_horizon_days if getattr(item, "spike_horizon_days", None) else g_horizon
    if not horizon or horizon <= 0:
        horizon = int(round(item.dlt)) if item.dlt else 0

    factor = item.spike_threshold_factor if getattr(item, "spike_threshold_factor", None) else g_factor
    if factor is None or factor <= 0:
        factor = g_factor

    return (int(horizon), float(factor))


# ---------------------------------------------------------------------------
# Planned Adjustments — DAF / LTAF / ZAF (deck slides 73-80)
# ---------------------------------------------------------------------------

@dataclass
class AdjustmentFactors:
    """Composite adjustment factors active on a given date for one item."""
    daf: float = 1.0
    ltaf: float = 1.0
    red_zaf: float = 1.0
    yellow_zaf: float = 1.0
    green_zaf: float = 1.0


def _adjustment_active(adj: BufferAdjustment, on_date: date) -> bool:
    start = adj.start_date.date() if adj.start_date else None
    end   = adj.end_date.date()   if adj.end_date   else None
    if start and on_date < start:
        return False
    if end and on_date > end:
        return False
    return True


def get_active_factors(item: Item, on_date: date,
                       adjustments: Optional[List[BufferAdjustment]] = None) -> AdjustmentFactors:
    """
    Return the composite adjustment factors active for `item` on `on_date`.
    Multiple overlapping adjustments multiply together (per the deck — stacking).
    Pass `adjustments` to avoid re-querying inside per-day loops.
    """
    if adjustments is None:
        session = get_session()
        try:
            adjustments = session.query(BufferAdjustment).filter_by(item_id=item.id).all()
        finally:
            session.close()

    f = AdjustmentFactors()
    for adj in adjustments:
        if not _adjustment_active(adj, on_date):
            continue
        f.daf        *= (adj.daf        if adj.daf        is not None else 1.0)
        f.ltaf       *= (adj.ltaf       if adj.ltaf       is not None else 1.0)
        f.red_zaf    *= (adj.red_zaf    if adj.red_zaf    is not None else 1.0)
        f.yellow_zaf *= (adj.yellow_zaf if adj.yellow_zaf is not None else 1.0)
        f.green_zaf  *= (adj.green_zaf  if adj.green_zaf  is not None else 1.0)
    return f


def load_item_adjustments(item_id: int) -> List[BufferAdjustment]:
    """Pre-load all adjustments for one item — call this once before per-day loops."""
    session = get_session()
    try:
        return session.query(BufferAdjustment).filter_by(item_id=item_id).all()
    finally:
        session.close()


def calculate_dynamic_adu(item: Item, window_days: int = ADU_WINDOW_DAYS) -> float:
    """
    Calculate ADU from actual demand entries in the past `window_days`.
    Formula: Σ(actual demand in last window_days) / window_days
    Falls back to item.adu if no demand data is available.
    """
    today_dt  = datetime.utcnow()
    start_dt  = today_dt - timedelta(days=window_days)

    session = get_session()
    try:
        entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_type == "actual",
                DemandEntry.demand_date >= start_dt,
                DemandEntry.demand_date <= today_dt,
            ).all()
        )
    finally:
        session.close()

    if not entries:
        return item.adu if item.adu > 0 else 0.0

    total_demand = sum(e.quantity for e in entries)
    dynamic_adu  = total_demand / window_days
    # If dynamic ADU is 0 (no demand this week), fall back to static to avoid
    # collapsing the buffer to zero
    return dynamic_adu if dynamic_adu > 0 else item.adu


def calculate_zones(item: Item, adu_override: float = None,
                    on_date: Optional[date] = None,
                    adjustments: Optional[List[BufferAdjustment]] = None) -> BufferZones:
    """
    Calculate Red / Yellow / Green buffer zones for an item.

    By default uses the DYNAMIC ADU (7-day rolling window).
    Pass adu_override to force a specific ADU value (e.g. for previews).

    Planned Adjustments (deck slides 73-80) are applied on `on_date`
    (defaults to today):
      ADU_eff = ADU * DAF
      DLT_eff = DLT * LTAF
      Red    *= red_ZAF
      Yellow *= yellow_ZAF
      Green  *= green_ZAF

    Formulas (standard DDMRP):
      Red Zone Base   = ADU_eff × DLT_eff × LTF
      Red Zone Safety = Red Zone Base × VF
      Red Zone        = (Red Zone Base + Red Zone Safety) × red_ZAF
      Yellow Zone     = ADU_eff × DLT_eff × yellow_ZAF
      Green Zone      = MAX(ADU_eff × OC, MOQ, Red Zone Base) × green_ZAF
      Top of Red      = Red Zone
      Top of Yellow   = Red + Yellow
      Top of Green    = Red + Yellow + Green
    """
    adu = adu_override if adu_override is not None else calculate_dynamic_adu(item)
    dlt = item.dlt
    ltf = item.lead_time_factor
    vf  = item.variability_factor
    moq = item.min_order_qty
    oc  = item.order_cycle

    # Apply planned adjustments active on `on_date`
    eval_date = on_date or date.today()
    factors   = get_active_factors(item, eval_date, adjustments)
    adu_eff   = adu * factors.daf
    dlt_eff   = dlt * factors.ltaf

    red_base   = adu_eff * dlt_eff * ltf
    red_safety = red_base * vf
    red_zone   = (red_base + red_safety) * factors.red_zaf

    yellow_zone = adu_eff * dlt_eff * factors.yellow_zaf

    green_candidates = [adu_eff * oc, moq, red_base]
    green_zone = max(green_candidates) if any(g > 0 for g in green_candidates) else 0.0
    green_zone *= factors.green_zaf

    tor = red_zone
    toy = red_zone + yellow_zone
    tog = red_zone + yellow_zone + green_zone

    # Canonical DDMRP KPIs (slides 59 & 92) — based on the EFFECTIVE ADU
    # (so DAF-driven demand changes correctly shorten/lengthen days-of-coverage)
    avg_inventory_target     = red_zone + green_zone / 2.0
    avg_order_frequency_days = (green_zone / adu_eff) if adu_eff > 0 else 0.0
    safety_days              = (red_zone / adu_eff)   if adu_eff > 0 else 0.0
    avg_active_orders        = (yellow_zone / green_zone) if green_zone > 0 else 0.0

    return BufferZones(
        item_id=item.id,
        part_number=item.part_number,
        red_zone_base=red_base,
        red_zone_safety=red_safety,
        red_zone=red_zone,
        yellow_zone=yellow_zone,
        green_zone=green_zone,
        top_of_red=tor,
        top_of_yellow=toy,
        top_of_green=tog,
        adu=adu_eff,
        dlt=dlt_eff,
        ltf=ltf,
        vf=vf,
        min_order_qty=moq,
        order_cycle=oc,
        avg_inventory_target=avg_inventory_target,
        avg_order_frequency_days=avg_order_frequency_days,
        safety_days=safety_days,
        avg_active_orders=avg_active_orders,
    )


def calculate_on_order(item: Item, as_of: datetime = None) -> float:
    """Sum of all open supply orders not yet received."""
    if as_of is None:
        as_of = datetime.utcnow()
    session = get_session()
    try:
        entries = (
            session.query(SupplyEntry)
            .filter(
                SupplyEntry.item_id == item.id,
                SupplyEntry.due_date >= as_of,
            )
            .all()
        )
        return sum(e.quantity for e in entries)
    finally:
        session.close()


def calculate_qualified_demand(item: Item, as_of: datetime = None) -> float:
    """
    Qualified demand = demand spikes (actual orders) due within the item's
    Spike Horizon.  Forecast entries are excluded from spikes.
    A spike is any single demand entry that exceeds (Spike Threshold Factor x ADU).

    Spike Horizon and Spike Threshold are configurable per item (deck slide 83);
    fallbacks: global Settings defaults, then DLT for the horizon and 2x for the factor.
    """
    if as_of is None:
        as_of = datetime.utcnow()

    spike_horizon_days, spike_factor = resolve_spike_params(item)
    horizon = as_of + timedelta(days=spike_horizon_days)
    spike_threshold = item.adu * spike_factor

    session = get_session()
    try:
        entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_type == "actual",
                DemandEntry.demand_date >= as_of,
                DemandEntry.demand_date <= horizon,
            )
            .all()
        )
        qualified = sum(e.quantity for e in entries if e.quantity > spike_threshold)
        return qualified
    finally:
        session.close()


def calculate_net_flow_position(item: Item, as_of: datetime = None) -> float:
    """
    Net Flow Position = On-Hand + On-Order - Qualified Demand
    """
    on_order = calculate_on_order(item, as_of)
    qualified_demand = calculate_qualified_demand(item, as_of)
    return item.on_hand + on_order - qualified_demand


def determine_status(nfp: float, zones: BufferZones) -> str:
    """
    Determine buffer status colour based on NFP vs zones.
      NFP <= Top of Red    → red    (critical)
      NFP <= Top of Yellow → yellow (plan replenishment)
      NFP >  Top of Yellow → green  (OK)
    """
    if nfp <= zones.top_of_red:
        return "red"
    elif nfp <= zones.top_of_yellow:
        return "yellow"
    else:
        return "green"


def calculate_suggested_order(nfp: float, zones: BufferZones) -> float:
    """
    Replenishment quantity = Top of Green - NFP  (if NFP is in yellow or red)
    Rounded up to nearest unit; 0 if already green.
    """
    if nfp <= zones.top_of_yellow:
        qty = zones.top_of_green - nfp
        return max(0.0, round(qty, 2))
    return 0.0


def dynamic_buffer_adjustment(zones: BufferZones, recent_adu: float) -> BufferZones:
    """
    Dynamic Buffer Adjustment (DBA):
    Recalculate zones using a recently observed ADU instead of the planned one.
    Useful for demand-driven recalibration (e.g. monthly review).
    """
    # Create a temporary item-like object with updated ADU
    class _Proxy:
        pass

    proxy = _Proxy()
    proxy.id = zones.item_id
    proxy.part_number = zones.part_number
    proxy.adu = recent_adu
    proxy.dlt = zones.dlt
    proxy.lead_time_factor = zones.ltf
    proxy.variability_factor = zones.vf
    proxy.min_order_qty = zones.min_order_qty
    proxy.order_cycle = zones.order_cycle

    return calculate_zones(proxy)


# ---------------------------------------------------------------------------
# High-level: calculate and persist buffer for one item
# ---------------------------------------------------------------------------

def recalculate_buffer(item: Item, as_of: datetime = None,
                       window_days: int = ADU_WINDOW_DAYS) -> BufferStatus:
    """
    Full DDMRP buffer recalculation for a single item.

    Buffer limits are calculated using a DYNAMIC ADU computed from the
    actual demand of the past `window_days` (default = 7 days / 1 week).
    This means buffer zones automatically tighten or widen based on the
    most recent week of actual consumption.

    Persists results to the buffers table and returns a BufferStatus.
    Next recalculation is due in 7 days (stored as next_recalc_due).
    """
    if as_of is None:
        as_of = datetime.utcnow()

    # Calculate dynamic ADU from last window_days of actual demand
    dyn_adu = calculate_dynamic_adu(item, window_days)

    # Compute zones using the dynamic ADU
    zones = calculate_zones(item, adu_override=dyn_adu)

    nfp           = calculate_net_flow_position(item, as_of)
    status        = determine_status(nfp, zones)
    suggested_qty = calculate_suggested_order(nfp, zones)
    next_due      = as_of + timedelta(days=7)   # recalculate again in 1 week

    exec_color, status_pct = execution_color(item.on_hand, zones)

    # Persist to DB
    session = get_session()
    try:
        buf = session.query(Buffer).filter_by(item_id=item.id).first()
        if buf is None:
            buf = Buffer(item_id=item.id)
            session.add(buf)

        buf.red_zone            = zones.red_zone
        buf.yellow_zone         = zones.yellow_zone
        buf.green_zone          = zones.green_zone
        buf.top_of_red          = zones.top_of_red
        buf.top_of_yellow       = zones.top_of_yellow
        buf.top_of_green        = zones.top_of_green
        buf.net_flow_position   = nfp
        buf.status              = status
        buf.suggested_order_qty = suggested_qty
        buf.dynamic_adu         = round(dyn_adu, 4)
        buf.static_adu          = item.adu
        buf.adu_window_days     = window_days
        buf.buffer_status_pct   = round(status_pct, 4)
        buf.execution_color     = exec_color
        buf.last_calculated     = as_of
        buf.next_recalc_due     = next_due

        session.commit()
    finally:
        session.close()

    return BufferStatus(
        item_id=item.id,
        part_number=item.part_number,
        on_hand=item.on_hand,
        on_order=calculate_on_order(item, as_of),
        qualified_demand=calculate_qualified_demand(item, as_of),
        net_flow_position=nfp,
        zones=zones,
        status=status,
        suggested_order_qty=suggested_qty,
        reorder_needed=(status in ("red", "yellow")),
        buffer_status_pct=round(status_pct, 4),
        execution_color=exec_color,
    )


def is_buffer_stale(buf: Buffer, window_days: int = ADU_WINDOW_DAYS) -> bool:
    """Return True if the buffer has not been recalculated within window_days."""
    if buf is None or buf.last_calculated is None:
        return True
    age = (datetime.utcnow() - buf.last_calculated).days
    return age >= window_days


def recalculate_all_buffers(window_days: int = ADU_WINDOW_DAYS) -> List[BufferStatus]:
    """Recalculate buffers for every item in the database.

    Args:
        window_days: Rolling window (days) used to compute dynamic ADU.
                     Defaults to ADU_WINDOW_DAYS (7 days / 1 week).
    """
    session = get_session()
    try:
        items = session.query(Item).all()
        results = []
        for item in items:
            try:
                result = recalculate_buffer(item, window_days=window_days)
                results.append(result)
            except Exception as e:
                print(f"Error calculating buffer for {item.part_number}: {e}")
        return results
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Forward projection — day-by-day NFP forecast
# ---------------------------------------------------------------------------

@dataclass
class DailyProjection:
    """Projected buffer state for a single future day."""
    day_index: int
    date: date
    demand_consumed: float          # demand expected on this day
    supply_received: float          # supply arriving on this day
    projected_on_hand: float        # running on-hand after receipts and consumption
    on_order_remaining: float       # open supply orders due after this day
    qualified_demand_spike: float   # demand spikes within DLT window from this day
    nfp: float                      # Net Flow Position
    status: str                     # green | yellow | red
    is_trigger: bool = False        # True on the first day NFP enters yellow or red


@dataclass
class ReplenishmentSignal:
    """A forward-looking replenishment signal for one item."""
    item_id: int
    part_number: str
    description: str

    # Today's snapshot
    today_nfp: float
    today_status: str
    today_on_hand: float
    today_on_order: float

    # Projected trigger
    trigger_date: Optional[date]        # first future day NFP drops into yellow/red
    trigger_nfp: float                  # NFP at the trigger point
    trigger_status: str                 # yellow or red

    # Action to take
    order_by_date: Optional[date]       # when the order must be placed
    receipt_date: Optional[date]        # when the order is expected to arrive
    order_quantity: float               # TOG - trigger_NFP

    # Full daily timeline for chart
    daily: List[DailyProjection] = field(default_factory=list)

    zones: Optional[BufferZones] = None


def project_buffer_forward(item: Item, horizon_days: int = 60) -> ReplenishmentSignal:
    """
    Simulate day-by-day NFP for an item over the next `horizon_days`.

    Logic per day d:
      supply_received(d)  = supply orders with due_date == d
      demand_today(d)     = sum of logged demand entries on day d,
                            OR item.adu if no entries exist for that day
      on_hand(d)          = on_hand(d-1) + supply_received(d) - demand_today(d)  [≥ 0]
      on_order(d)         = sum of supply due STRICTLY after day d
      spike_demand(d)     = sum of actual demand entries > 2×ADU
                            with demand_date in [d, d + DLT]
      NFP(d)              = on_hand(d) + on_order(d) - spike_demand(d)
      status(d)           = red | yellow | green  via determine_status()

    Replenishment signal:
      trigger_date  = first day where status transitions INTO yellow or red
      order_by_date = today if already yellow/red, else trigger_date
      receipt_date  = order_by_date + DLT days
      order_qty     = TOG - NFP at trigger (using current zones)
    """
    today_dt = datetime.utcnow()
    today = today_dt.date()
    dlt_days = int(round(item.dlt)) if item.dlt else 0
    spike_horizon_days, spike_factor = resolve_spike_params(item)
    adjustments = load_item_adjustments(item.id)

    zones = calculate_zones(item, on_date=today, adjustments=adjustments)

    # ---- Pre-load all demand and supply for this item in one query each ----
    session = get_session()
    try:
        horizon_end = today + timedelta(days=horizon_days + max(dlt_days, spike_horizon_days) + 1)

        demand_entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_date >= today_dt,
                DemandEntry.demand_date <= datetime.combine(horizon_end, datetime.min.time()),
            )
            .all()
        )
        supply_entries = (
            session.query(SupplyEntry)
            .filter(
                SupplyEntry.item_id == item.id,
                SupplyEntry.due_date >= today_dt,
                SupplyEntry.due_date <= datetime.combine(horizon_end, datetime.min.time()),
            )
            .all()
        )
    finally:
        session.close()

    # Index by date for O(1) lookups
    demand_by_date: dict = {}
    for e in demand_entries:
        d = e.demand_date.date()
        demand_by_date.setdefault(d, [])
        demand_by_date[d].append(e)

    supply_by_date: dict = {}
    for e in supply_entries:
        d = e.due_date.date()
        supply_by_date.setdefault(d, [])
        supply_by_date[d].append(e)

    spike_threshold = item.adu * spike_factor

    # Today's actual NFP (used for day-0 snapshot)
    today_on_order = sum(e.quantity for e in supply_entries)  # all future supply
    today_spike = sum(
        e.quantity for e in demand_entries
        if e.demand_type == "actual"
        and e.demand_date.date() <= today + timedelta(days=spike_horizon_days)
        and e.quantity > spike_threshold
    )
    today_nfp = item.on_hand + today_on_order - today_spike
    today_status = determine_status(today_nfp, zones)

    # ---- Day-by-day simulation ----
    running_oh = item.on_hand
    daily: List[DailyProjection] = []
    trigger_day: Optional[DailyProjection] = None
    previous_status = today_status

    for d in range(horizon_days + 1):
        day_date = today + timedelta(days=d)

        # Per-day adjustment factors (DAF lifts ADU; ZAF/LTAF re-shape zones)
        day_factors = get_active_factors(item, day_date, adjustments)
        day_zones   = (calculate_zones(item, on_date=day_date, adjustments=adjustments)
                       if day_factors != AdjustmentFactors()
                       else zones)

        # Supply arriving today
        supply_today = sum(e.quantity for e in supply_by_date.get(day_date, []))

        # Demand today: use logged entries if present, else fall back to ADU * DAF
        day_demand_entries = demand_by_date.get(day_date, [])
        if day_demand_entries:
            demand_today = sum(e.quantity for e in day_demand_entries)
        else:
            demand_today = item.adu * day_factors.daf

        # Update running on-hand (day 0 = current snapshot, no movement yet)
        if d > 0:
            running_oh = max(0.0, running_oh + supply_today - demand_today)

        # On-order = supply due AFTER this day
        on_order = sum(
            e.quantity for e in supply_entries
            if e.due_date.date() > day_date
        )

        # Qualified demand spikes within Spike Horizon from this day
        spike_window_end = day_date + timedelta(days=spike_horizon_days)
        spike_demand = sum(
            e.quantity for e in demand_entries
            if e.demand_type == "actual"
            and day_date <= e.demand_date.date() <= spike_window_end
            and e.quantity > spike_threshold
        )

        nfp = running_oh + on_order - spike_demand
        status = determine_status(nfp, day_zones)

        # Detect first trigger: transition from green into yellow or red
        is_trigger = False
        if (
            trigger_day is None
            and d > 0                          # never flag day 0
            and status in ("yellow", "red")
            and previous_status == "green"
        ):
            is_trigger = True

        proj = DailyProjection(
            day_index=d,
            date=day_date,
            demand_consumed=demand_today,
            supply_received=supply_today,
            projected_on_hand=round(running_oh, 2),
            on_order_remaining=round(on_order, 2),
            qualified_demand_spike=round(spike_demand, 2),
            nfp=round(nfp, 2),
            status=status,
            is_trigger=is_trigger,
        )
        daily.append(proj)

        if is_trigger and trigger_day is None:
            trigger_day = proj

        previous_status = status

    # ---- If already in yellow/red today, trigger is today ----
    if trigger_day is None and today_status in ("yellow", "red"):
        trigger_day = daily[0]
        daily[0].is_trigger = True

    # ---- Build replenishment signal ----
    if trigger_day is not None:
        # Order must be placed today so it arrives before the trigger date
        # If trigger is in the future: order_by = trigger_date - DLT
        order_by = trigger_day.date - timedelta(days=dlt_days)
        # If that's in the past or is today, order today
        order_by = max(order_by, today)
        receipt = order_by + timedelta(days=dlt_days)
        order_qty = max(0.0, round(zones.top_of_green - trigger_day.nfp, 2))
        # Respect MOQ
        if item.min_order_qty and order_qty < item.min_order_qty:
            order_qty = item.min_order_qty
    else:
        order_by = None
        receipt = None
        order_qty = 0.0

    return ReplenishmentSignal(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description,
        today_nfp=round(today_nfp, 2),
        today_status=today_status,
        today_on_hand=item.on_hand,
        today_on_order=round(today_on_order, 2),
        trigger_date=trigger_day.date if trigger_day else None,
        trigger_nfp=round(trigger_day.nfp, 2) if trigger_day else today_nfp,
        trigger_status=trigger_day.status if trigger_day else today_status,
        order_by_date=order_by,
        receipt_date=receipt,
        order_quantity=order_qty,
        daily=daily,
        zones=zones,
    )


def project_all_buffers(horizon_days: int = 60) -> List[ReplenishmentSignal]:
    """Run forward projection for every item."""
    session = get_session()
    try:
        items = session.query(Item).all()
    finally:
        session.close()

    results = []
    for item in items:
        try:
            results.append(project_buffer_forward(item, horizon_days))
        except Exception as e:
            print(f"Projection error for {item.part_number}: {e}")
    return results


# ---------------------------------------------------------------------------
# Full-horizon planned orders — keep buffer in green zone throughout
# ---------------------------------------------------------------------------

@dataclass
class PlannedOrder:
    """A single DDMRP-suggested replenishment order."""
    item_id: int
    part_number: str
    description: str
    order_date: date          # date the order must be PLACED
    receipt_date: date        # date the supply arrives (order_date + DLT)
    order_quantity: float     # suggested qty (TOG - NFP at trigger, respects MOQ)
    nfp_before: float         # NFP that triggered the order
    nfp_after: float          # projected NFP immediately after order is added to on-order
    trigger_status: str       # "yellow" or "red"
    days_until_order: int     # calendar days from today until order must be placed
    is_urgent: bool           # True if order_date is today or already past


@dataclass
class PlanningResult:
    """Output of the full-horizon planning run for one item."""
    item_id: int
    part_number: str
    description: str
    zones: BufferZones
    planned_orders: List[PlannedOrder]       # all orders needed across the horizon
    daily_planned: List[DailyProjection]     # day-by-day NFP WITH planned orders applied
    daily_unplanned: List[DailyProjection]   # day-by-day NFP WITHOUT any new orders (raw forecast)


def plan_replenishment_orders(item: Item, horizon_days: int = 60) -> PlanningResult:
    """
    Full-horizon DDMRP replenishment planning for one item.

    Algorithm:
      For each day d = 0 … horizon:
        1. Add supply receipts due on day d (existing + previously planned orders)
        2. Consume demand (actual logged entries, or ADU as fallback)
        3. Compute NFP = running_on_hand + open_on_order_after_d - qualified_spikes
        4. If NFP ≤ TOY:
             - Generate planned order qty = max(TOG − NFP, MOQ)
             - Place it TODAY (d=0) if already yellow/red, else on the trigger day
             - Receipt = order_date + DLT  →  add to open_orders immediately
             - NFP recalculated; this should push it back into green
        5. Record every day into daily_planned

    Also builds daily_unplanned (same simulation without generating any orders)
    so the chart can show both lines for comparison.
    """
    today_dt = datetime.utcnow()
    today    = today_dt.date()
    dlt_days = max(1, int(round(item.dlt))) if item.dlt else 1
    spike_horizon_days, spike_factor = resolve_spike_params(item)
    adjustments = load_item_adjustments(item.id)

    zones = calculate_zones(item, on_date=today, adjustments=adjustments)

    # ---- Load all demand and supply once ----
    session = get_session()
    try:
        horizon_end = today + timedelta(days=horizon_days + max(dlt_days, spike_horizon_days) + 1)
        demand_entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_date >= today_dt,
                DemandEntry.demand_date <= datetime.combine(horizon_end, datetime.min.time()),
            ).all()
        )
        supply_entries = (
            session.query(SupplyEntry)
            .filter(
                SupplyEntry.item_id == item.id,
                SupplyEntry.due_date >= today_dt,
                SupplyEntry.due_date <= datetime.combine(horizon_end, datetime.min.time()),
            ).all()
        )
    finally:
        session.close()

    # Index demand by date
    demand_by_date: dict = {}
    for e in demand_entries:
        d = e.demand_date.date()
        demand_by_date.setdefault(d, [])
        demand_by_date[d].append(e)

    spike_threshold = item.adu * spike_factor

    # ---- Helper: compute NFP for a given state ----
    def _nfp(oh, open_orders_list, day_date):
        on_order = sum(qty for rdate, qty in open_orders_list if rdate > day_date)
        spk_end  = day_date + timedelta(days=spike_horizon_days)
        spikes   = sum(
            e.quantity for e in demand_entries
            if e.demand_type == "actual"
            and day_date <= e.demand_date.date() <= spk_end
            and e.quantity > spike_threshold
        )
        return oh + on_order - spikes

    # ---- Run WITHOUT planning (unplanned baseline) ----
    def _run_simulation(generate_orders: bool):
        running_oh   = item.on_hand
        # open_orders: list of (receipt_date, qty)
        open_orders  = [(e.due_date.date(), e.quantity) for e in supply_entries]
        planned      = []
        daily        = []
        orders_placed_on: set = set()   # prevent >1 order per day per item

        for d in range(horizon_days + 1):
            day_date = today + timedelta(days=d)

            # Per-day adjustments
            day_factors = get_active_factors(item, day_date, adjustments)
            day_zones   = (calculate_zones(item, on_date=day_date, adjustments=adjustments)
                           if day_factors != AdjustmentFactors()
                           else zones)
            day_dlt = max(1, int(round(item.dlt * day_factors.ltaf))) if item.dlt else dlt_days

            # 1. Receive supply due today
            arrived = sum(qty for rdate, qty in open_orders if rdate == day_date)
            if d > 0:
                day_demand_entries = demand_by_date.get(day_date, [])
                demand_today = (sum(e.quantity for e in day_demand_entries)
                                if day_demand_entries
                                else item.adu * day_factors.daf)
                running_oh = max(0.0, running_oh + arrived - demand_today)
            else:
                demand_today = item.adu * day_factors.daf  # day 0: snapshot, no movement

            # 2. Compute NFP
            nfp    = _nfp(running_oh, open_orders, day_date)
            status = determine_status(nfp, day_zones)

            # 3. Generate order if NFP in yellow or red (planning mode only)
            if generate_orders and status in ("yellow", "red") and day_date not in orders_placed_on:
                raw_qty  = day_zones.top_of_green - nfp
                order_qty = max(raw_qty, item.min_order_qty if item.min_order_qty else 0)
                order_qty = round(order_qty, 2)

                receipt_d = day_date + timedelta(days=day_dlt)
                open_orders.append((receipt_d, order_qty))
                orders_placed_on.add(day_date)

                nfp_after = _nfp(running_oh, open_orders, day_date)
                days_until = (day_date - today).days

                planned.append(PlannedOrder(
                    item_id=item.id,
                    part_number=item.part_number,
                    description=item.description,
                    order_date=day_date,
                    receipt_date=receipt_d,
                    order_quantity=order_qty,
                    nfp_before=round(nfp, 2),
                    nfp_after=round(nfp_after, 2),
                    trigger_status=status,
                    days_until_order=days_until,
                    is_urgent=(days_until <= 0),
                ))

                # Recompute NFP and status after order
                nfp    = nfp_after
                status = determine_status(nfp, day_zones)

            daily.append(DailyProjection(
                day_index=d,
                date=day_date,
                demand_consumed=demand_today,
                supply_received=arrived,
                projected_on_hand=round(running_oh, 2),
                on_order_remaining=round(
                    sum(qty for rdate, qty in open_orders if rdate > day_date), 2),
                qualified_demand_spike=0.0,
                nfp=round(nfp, 2),
                status=status,
                is_trigger=False,
            ))

        return planned, daily

    planned_orders, daily_planned   = _run_simulation(generate_orders=True)
    _,               daily_unplanned = _run_simulation(generate_orders=False)

    return PlanningResult(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description,
        zones=zones,
        planned_orders=planned_orders,
        daily_planned=daily_planned,
        daily_unplanned=daily_unplanned,
    )


def plan_all_items(horizon_days: int = 60) -> List[PlanningResult]:
    """Run full-horizon planning for every item in the database."""
    session = get_session()
    try:
        items = session.query(Item).all()
    finally:
        session.close()

    results = []
    for item in items:
        try:
            results.append(plan_replenishment_orders(item, horizon_days))
        except Exception as e:
            print(f"Planning error for {item.part_number}: {e}")
    return results
