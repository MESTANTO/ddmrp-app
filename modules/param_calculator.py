"""
Dynamic DDMRP Parameter Calculator
------------------------------------
Calculates ADU, DLT, Lead Time Factor (LTF) and Variability Factor (VF)
dynamically from actual demand history and supply data.

Formulas (as per DDMRP standard):

  ADU (Average Daily Usage)
  ─────────────────────────
    Past ADU    = Σ(actual demand in past N days) / N
    Forward ADU = Σ(forecast demand in next M days) / M
    Blended ADU = (w_past × Past ADU + w_fwd × Forward ADU) / (w_past + w_fwd)

  VF  (Variability Factor)   — demand variability
  ─────────────────────────
    CV_demand = StdDev(daily demand) / Mean(daily demand)
    CV < 0.20  → VF = 0.20   (very low variability)
    CV < 0.40  → VF = 0.40   (low)
    CV < 0.60  → VF = 0.60   (medium)
    CV < 0.80  → VF = 0.70   (high)
    CV ≥ 0.80  → VF = 0.80   (very high)

  LTF (Lead Time Factor)     — supply lead-time variability
  ─────────────────────────
    CV_lt = StdDev(supply lead times) / Mean(supply lead times)
    CV_lt < 0.20 → LTF = 0.30
    CV_lt < 0.40 → LTF = 0.50
    CV_lt < 0.60 → LTF = 0.60
    CV_lt ≥ 0.60 → LTF = 0.80
    (if only one supply entry available → LTF = 0.50 default)

  DLT (Decoupled Lead Time)  — estimated from open supply orders
  ─────────────────────────
    DLT = average days until receipt across all open supply entries
    (if no supply data → keeps existing value)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from statistics import mean, stdev
from typing import Optional
from database.db import get_session, Item, DemandEntry, SupplyEntry
from database.auth import get_company_id


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CalcParams:
    """Dynamically calculated DDMRP parameters for one item."""
    item_id:    int
    part_number: str
    description: str

    # Calculated values
    adu:              float
    dlt:              float
    lead_time_factor: float
    variability_factor: float

    # Current (existing) values for comparison
    current_adu:  float
    current_dlt:  float
    current_ltf:  float
    current_vf:   float

    # Diagnostics
    past_adu:     float
    forward_adu:  float
    cv_demand:    float   # coefficient of variation of daily demand
    cv_lt:        float   # coefficient of variation of lead times
    n_demand_days: int    # how many demand data-points used
    n_supply_entries: int # how many supply entries used for DLT/LTF
    adu_method:   str     # "past" | "forward" | "blended"

    # Flags
    demand_data_sufficient: bool   # False → not enough history, ADU kept/defaulted
    supply_data_sufficient: bool   # False → not enough supply data, DLT/LTF kept


# ─────────────────────────────────────────────────────────────────────────────
# Core calculation
# ─────────────────────────────────────────────────────────────────────────────

def calculate_params(
    item: Item,
    lookback_days: int   = 60,
    forward_days: int    = 30,
    adu_method: str      = "blended",   # "past" | "forward" | "blended"
    past_weight: float   = 0.6,
    forward_weight: float = 0.4,
) -> CalcParams:
    """
    Compute ADU, DLT, LTF, VF dynamically for one item.
    Falls back to existing item values when data is insufficient.
    """
    today    = datetime.utcnow().date()
    today_dt = datetime.utcnow()

    # ── Load demand data ──────────────────────────────────────────────────────
    past_start  = today_dt - timedelta(days=lookback_days)
    future_end  = today_dt + timedelta(days=forward_days)

    session = get_session()
    try:
        past_entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_type == "actual",
                DemandEntry.demand_date >= past_start,
                DemandEntry.demand_date <= today_dt,
            ).all()
        )
        future_entries = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item.id,
                DemandEntry.demand_type.in_(["forecast", "actual"]),
                DemandEntry.demand_date > today_dt,
                DemandEntry.demand_date <= future_end,
            ).all()
        )
        supply_entries = (
            session.query(SupplyEntry)
            .filter(
                SupplyEntry.item_id == item.id,
                SupplyEntry.due_date >= today_dt,
            ).all()
        )
    finally:
        session.close()

    # ── ADU — Past ───────────────────────────────────────────────────────────
    # Aggregate to daily buckets
    past_daily: dict = {}
    for e in past_entries:
        d = e.demand_date.date()
        past_daily[d] = past_daily.get(d, 0.0) + e.quantity

    # Fill missing days in the lookback window with 0
    all_past_days = [
        (today - timedelta(days=i)) for i in range(lookback_days)
    ]
    past_values = [past_daily.get(d, 0.0) for d in all_past_days]
    past_adu = sum(past_values) / lookback_days if lookback_days > 0 else 0.0

    # ── ADU — Forward ─────────────────────────────────────────────────────────
    future_daily: dict = {}
    for e in future_entries:
        d = e.demand_date.date()
        future_daily[d] = future_daily.get(d, 0.0) + e.quantity

    all_future_days = [
        (today + timedelta(days=i + 1)) for i in range(forward_days)
    ]
    future_values = [future_daily.get(d, 0.0) for d in all_future_days]
    forward_adu = sum(future_values) / forward_days if forward_days > 0 else 0.0

    # ── ADU — Blended ─────────────────────────────────────────────────────────
    if adu_method == "past":
        adu = past_adu
    elif adu_method == "forward":
        adu = forward_adu
    else:  # blended
        total_w = past_weight + forward_weight
        adu = (past_weight * past_adu + forward_weight * forward_adu) / total_w

    demand_sufficient = sum(1 for v in past_values if v > 0) >= max(3, lookback_days // 10)

    # If we couldn't compute a meaningful ADU, keep the existing value
    if adu <= 0:
        adu = item.adu if item.adu > 0 else 1.0

    # ── Variability Factor — demand CV ───────────────────────────────────────
    non_zero = [v for v in past_values if v > 0]
    if len(non_zero) >= 5 and mean(past_values) > 0:
        try:
            cv_demand = stdev(past_values) / mean(past_values)
        except Exception:
            cv_demand = 0.5
    else:
        cv_demand = 0.5  # default: medium variability

    vf = _cv_to_vf(cv_demand)

    # ── Lead Time Factor & DLT — from supply entries ──────────────────────────
    lead_times = [
        (e.due_date.date() - today).days
        for e in supply_entries
        if (e.due_date.date() - today).days > 0
    ]

    supply_sufficient = len(lead_times) >= 2

    if lead_times:
        dlt = round(mean(lead_times), 1)
        if len(lead_times) >= 2 and mean(lead_times) > 0:
            try:
                cv_lt = stdev(lead_times) / mean(lead_times)
            except Exception:
                cv_lt = 0.5
        else:
            cv_lt = 0.5
        ltf = _cv_to_ltf(cv_lt)
    else:
        # Not enough supply data — keep existing values
        dlt   = item.dlt   if item.dlt   > 0 else 5.0
        ltf   = item.lead_time_factor if item.lead_time_factor > 0 else 0.5
        cv_lt = 0.5

    return CalcParams(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description,
        adu=round(adu, 2),
        dlt=round(dlt, 1),
        lead_time_factor=ltf,
        variability_factor=vf,
        current_adu=item.adu,
        current_dlt=item.dlt,
        current_ltf=item.lead_time_factor,
        current_vf=item.variability_factor,
        past_adu=round(past_adu, 2),
        forward_adu=round(forward_adu, 2),
        cv_demand=round(cv_demand, 3),
        cv_lt=round(cv_lt, 3),
        n_demand_days=sum(1 for v in past_values if v > 0),
        n_supply_entries=len(lead_times),
        adu_method=adu_method,
        demand_data_sufficient=demand_sufficient,
        supply_data_sufficient=supply_sufficient,
    )


def calculate_all_params(
    lookback_days: int   = 60,
    forward_days: int    = 30,
    adu_method: str      = "blended",
    past_weight: float   = 0.6,
    forward_weight: float = 0.4,
    company_id: int      = None,
) -> list:
    """Run dynamic parameter calculation for every item."""
    session = get_session()
    try:
        q = session.query(Item)
        if company_id is not None:
            q = q.filter(Item.company_id == company_id)
        items = q.all()
    finally:
        session.close()

    results = []
    for item in items:
        try:
            results.append(calculate_params(
                item, lookback_days, forward_days,
                adu_method, past_weight, forward_weight,
            ))
        except Exception as e:
            print(f"Param calc error for {item.part_number}: {e}")
    return results


def apply_params(calc: CalcParams):
    """Persist dynamically calculated parameters back to the Item record."""
    session = get_session()
    try:
        item = session.query(Item).get(calc.item_id)
        if item:
            item.adu               = calc.adu
            item.dlt               = calc.dlt
            item.lead_time_factor  = calc.lead_time_factor
            item.variability_factor = calc.variability_factor
            session.commit()
    finally:
        session.close()


def apply_all_params(results: list):
    """Apply a list of CalcParams to the database in one transaction."""
    session = get_session()
    try:
        for calc in results:
            item = session.query(Item).get(calc.item_id)
            if item:
                item.adu                = calc.adu
                item.dlt                = calc.dlt
                item.lead_time_factor   = calc.lead_time_factor
                item.variability_factor = calc.variability_factor
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# CV → Factor mapping helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cv_to_vf(cv: float) -> float:
    """Map demand coefficient of variation to Variability Factor."""
    if cv < 0.20: return 0.20
    if cv < 0.40: return 0.40
    if cv < 0.60: return 0.60
    if cv < 0.80: return 0.70
    return 0.80


def _cv_to_ltf(cv: float) -> float:
    """Map lead-time coefficient of variation to Lead Time Factor."""
    if cv < 0.20: return 0.30
    if cv < 0.40: return 0.50
    if cv < 0.60: return 0.60
    return 0.80
