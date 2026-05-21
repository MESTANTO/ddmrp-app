"""
modules/forecasting.py — Demand forecasting methods for inventory optimization.

Implements:
- Simple Moving Average (SMA 3-month, SMA 6-month)
- Weighted Moving Average (WMA)
- Seasonal index calculation + seasonal-adjusted forecast
- Linear trend projection
- Walk-forward forecast accuracy (MAE, MAPE, bias)
- Company-wide forecast summary for the LLM skill context

All functions are READ-ONLY — no DB writes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, stdev
from typing import List, Optional

from database.db import DemandEntry, Item, SessionLocal

# ---------------------------------------------------------------------------
# Month helpers
# ---------------------------------------------------------------------------

_MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MonthPoint:
    year: int
    month: int
    demand: float

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"


@dataclass
class ForecastResult:
    item_id: int
    part_number: str
    description: str
    method: str
    lookback_months: int
    history: List[dict]          # [{label, demand}, ...]
    forecast: List[dict]         # [{label, demand, lower, upper}, ...]
    mean_monthly: float
    std_monthly: float
    cv: float
    has_enough_data: bool
    seasonal_indices: Optional[List[dict]] = None  # [{month_name, index, pattern}, ...]
    trend_slope: float = 0.0
    notes: str = ""


@dataclass
class SeasonalityResult:
    item_id: int
    part_number: str
    description: str
    annual_avg: float
    monthly_indices: List[dict]
    peak_months: List[str]
    trough_months: List[str]
    has_seasonality: bool
    seasonality_strength: float
    data_years: float
    notes: str = ""


@dataclass
class AccuracyResult:
    item_id: int
    part_number: str
    description: str
    method: str
    periods: List[dict]
    mae: float
    mape: float
    bias: float
    accuracy_status: str   # "good" | "acceptable" | "poor" | "unknown"
    has_enough_data: bool
    notes: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_monthly_demand(item_id: int, lookback_months: int = 36) -> List[MonthPoint]:
    """Aggregate actual DemandEntry rows into monthly totals (Python-side)."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_months * 30)
    session = SessionLocal()
    try:
        rows = (
            session.query(DemandEntry)
            .filter(
                DemandEntry.item_id == item_id,
                DemandEntry.demand_date >= cutoff,
                DemandEntry.demand_type == "actual",
            )
            .order_by(DemandEntry.demand_date)
            .all()
        )
        buckets: dict[tuple[int, int], float] = {}
        for r in rows:
            d = r.demand_date
            key = (d.year, d.month)
            buckets[key] = buckets.get(key, 0.0) + float(r.quantity or 0)
        return [
            MonthPoint(year=y, month=m, demand=v)
            for (y, m), v in sorted(buckets.items())
        ]
    finally:
        session.close()


def _sma(values: list, window: int) -> float:
    tail = values[-window:] if len(values) >= window else values
    return mean(tail) if tail else 0.0


def _wma(values: list, weights: list | None = None) -> float:
    """Weighted moving average; default weights [0.20, 0.35, 0.45] for 3 months."""
    if weights is None:
        weights = [0.20, 0.35, 0.45]
    n = len(weights)
    tail = values[-n:]
    if not tail:
        return 0.0
    if len(tail) < n:
        used_w = weights[-len(tail):]
        total_w = sum(used_w)
        return sum(v * w for v, w in zip(tail, used_w)) / total_w if total_w else 0.0
    return sum(v * w for v, w in zip(tail, weights))


def _seasonal_indices(monthly: list) -> list:
    """Return 12 monthly seasonal indices (index 0=Jan … 11=Dec). Requires ≥12 months."""
    if len(monthly) < 12:
        return [1.0] * 12
    by_month: dict[int, list] = {m: [] for m in range(1, 13)}
    for p in monthly:
        by_month[p.month].append(p.demand)
    month_avgs = {m: mean(vs) for m, vs in by_month.items() if vs}
    overall = mean(list(month_avgs.values())) if month_avgs else 1.0
    if overall == 0:
        return [1.0] * 12
    return [month_avgs.get(m, overall) / overall for m in range(1, 13)]


def _trend_slope(values: list) -> float:
    """Ordinary least-squares slope (units/month)."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = mean(values)
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den else 0.0


def _next_month(year: int, month: int, offset: int) -> tuple[int, int]:
    m = month + offset
    y = year + (m - 1) // 12
    m = ((m - 1) % 12) + 1
    return y, m


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def forecast_item(
    item: Item,
    method: str = "sma_3",
    periods: int = 3,
    lookback_months: int = 24,
) -> ForecastResult:
    """
    Compute demand forecast for one item.

    method: "sma_3" | "sma_6" | "wma" | "seasonal" | "trend"
    periods: number of future months to forecast
    """
    monthly = _get_monthly_demand(item.id, lookback_months=lookback_months)
    demands = [p.demand for p in monthly]
    has_data = len(monthly) >= 3

    m_val = mean(demands) if demands else 0.0
    s_val = stdev(demands) if len(demands) >= 2 else 0.0
    cv    = s_val / m_val if m_val > 0 else 0.0

    history = [{"label": p.label, "demand": round(p.demand, 1)} for p in monthly]
    seasonal = _seasonal_indices(monthly) if len(monthly) >= 12 else [1.0] * 12
    slope    = _trend_slope(demands)

    last_p = monthly[-1] if monthly else None
    roll   = list(demands)  # rolling list extended with each forecast step

    forecast_pts = []
    for i in range(1, periods + 1):
        if last_p:
            fy, fm = _next_month(last_p.year, last_p.month, i)
            label = f"{fy}-{fm:02d}"
            si = seasonal[fm - 1]
        else:
            label = f"Month+{i}"
            si = 1.0

        if method == "sma_3":
            base = _sma(roll, 3)
        elif method == "sma_6":
            base = _sma(roll, 6)
        elif method == "wma":
            base = _wma(roll)
        elif method == "seasonal":
            base = _sma(roll, 3) * si
        elif method == "trend":
            base = max(0.0, m_val + slope * (len(roll) - len(demands) / 2 + i))
        else:
            base = _sma(roll, 3)

        base = max(0.0, base)
        forecast_pts.append({
            "label":  label,
            "demand": round(base, 1),
            "lower":  round(max(0.0, base - s_val), 1),
            "upper":  round(base + s_val, 1),
        })
        roll.append(base)

    season_dicts = None
    if len(monthly) >= 12:
        season_dicts = []
        for mi, idx in enumerate(seasonal):
            pattern = "peak" if idx >= 1.2 else ("trough" if idx <= 0.8 else "normal")
            season_dicts.append({
                "month_name": _MONTH_NAMES[mi],
                "month_num":  mi + 1,
                "index":      round(idx, 3),
                "pattern":    pattern,
            })

    notes = ""
    if not has_data:
        notes = "Insufficient demand history (< 3 months of actual data)."

    return ForecastResult(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description or "",
        method=method,
        lookback_months=lookback_months,
        history=history,
        forecast=forecast_pts,
        mean_monthly=round(m_val, 2),
        std_monthly=round(s_val, 2),
        cv=round(cv, 3),
        has_enough_data=has_data,
        seasonal_indices=season_dicts,
        trend_slope=round(slope, 3),
        notes=notes,
    )


def seasonal_analysis_item(item: Item, lookback_months: int = 36) -> SeasonalityResult:
    """Compute monthly seasonal indices and identify peak/trough months."""
    monthly  = _get_monthly_demand(item.id, lookback_months=lookback_months)
    demands  = [p.demand for p in monthly]
    annual_avg = mean(demands) if demands else 0.0
    seasonal = _seasonal_indices(monthly)
    data_years = len(monthly) / 12.0

    indices_dicts, peak_months, trough_months = [], [], []
    for mi, idx in enumerate(seasonal):
        if idx >= 1.2:
            pattern = "peak"
            peak_months.append(_MONTH_NAMES[mi])
        elif idx <= 0.8:
            pattern = "trough"
            trough_months.append(_MONTH_NAMES[mi])
        else:
            pattern = "normal"
        indices_dicts.append({
            "month_name": _MONTH_NAMES[mi],
            "month_num":  mi + 1,
            "index":      round(idx, 3),
            "pattern":    pattern,
        })

    strength = max(seasonal) - min(seasonal)
    has_seasonality = strength >= 0.4 and len(monthly) >= 12

    notes = ""
    if len(monthly) < 12:
        notes = "Need ≥12 months of data for reliable seasonal analysis."

    return SeasonalityResult(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description or "",
        annual_avg=round(annual_avg, 2),
        monthly_indices=indices_dicts,
        peak_months=peak_months,
        trough_months=trough_months,
        has_seasonality=has_seasonality,
        seasonality_strength=round(strength, 3),
        data_years=round(data_years, 1),
        notes=notes,
    )


def accuracy_report_item(
    item: Item,
    method: str = "sma_3",
    holdout_months: int = 3,
    lookback_months: int = 24,
) -> AccuracyResult:
    """
    Walk-forward accuracy: for each of the last holdout_months compute what the
    forecast would have been vs actual demand. Returns MAE, MAPE, and bias.
    """
    monthly  = _get_monthly_demand(item.id, lookback_months=lookback_months + holdout_months)
    has_data = len(monthly) >= holdout_months + 3

    if not has_data:
        return AccuracyResult(
            item_id=item.id,
            part_number=item.part_number,
            description=item.description or "",
            method=method,
            periods=[],
            mae=0.0, mape=0.0, bias=0.0,
            accuracy_status="unknown",
            has_enough_data=False,
            notes="Insufficient data for accuracy analysis (need holdout + 3 months).",
        )

    train = list(monthly[:-holdout_months])
    test  = monthly[-holdout_months:]
    seasonal_base = _seasonal_indices(train) if len(train) >= 12 else [1.0] * 12

    periods = []
    for actual_pt in test:
        train_demands = [p.demand for p in train]
        if method == "sma_3":
            fv = _sma(train_demands, 3)
        elif method == "sma_6":
            fv = _sma(train_demands, 6)
        elif method == "wma":
            fv = _wma(train_demands)
        elif method == "seasonal":
            fv = _sma(train_demands, 3) * seasonal_base[actual_pt.month - 1]
        else:
            fv = _sma(train_demands, 3)

        err     = fv - actual_pt.demand
        abs_err = abs(err)
        pct_err = (abs_err / actual_pt.demand * 100) if actual_pt.demand > 0 else 0.0

        periods.append({
            "label":     actual_pt.label,
            "actual":    round(actual_pt.demand, 1),
            "forecast":  round(fv, 1),
            "error":     round(err, 1),
            "abs_error": round(abs_err, 1),
            "pct_error": round(pct_err, 1),
        })
        train.append(actual_pt)

    mae  = mean([p["abs_error"] for p in periods]) if periods else 0.0
    mape = mean([p["pct_error"] for p in periods]) if periods else 0.0
    bias = mean([p["error"]     for p in periods]) if periods else 0.0

    status = "good" if mape < 10 else ("acceptable" if mape < 20 else "poor")

    return AccuracyResult(
        item_id=item.id,
        part_number=item.part_number,
        description=item.description or "",
        method=method,
        periods=periods,
        mae=round(mae, 2),
        mape=round(mape, 2),
        bias=round(bias, 2),
        accuracy_status=status,
        has_enough_data=True,
    )


# ---------------------------------------------------------------------------
# Company-wide summary for LLM skill context
# ---------------------------------------------------------------------------

def get_demand_forecast_data(company_id: int, lookback_months: int = 24) -> list:
    """
    Compute forecast summary for all company items.
    Returns list of dicts sorted by |adu_divergence_pct| descending.
    Used by the demand_forecasting LLM skill.
    """
    session = SessionLocal()
    try:
        items = session.query(Item).filter(Item.company_id == company_id).all()
    finally:
        session.close()

    results = []
    for item in items:
        try:
            monthly = _get_monthly_demand(item.id, lookback_months=lookback_months)
            if len(monthly) < 3:
                continue
            demands = [p.demand for p in monthly]
            m_val   = mean(demands)
            s_val   = stdev(demands) if len(demands) >= 2 else 0.0
            cv      = s_val / m_val if m_val > 0 else 0.0
            slope   = _trend_slope(demands)
            sma3    = _sma(demands, 3)
            seasonal = _seasonal_indices(monthly)

            # ADU divergence: compare SMA-implied daily rate vs stored ADU
            adu_daily    = m_val / 30.0  # monthly → daily
            adu_div_pct  = (
                (adu_daily - item.adu) / item.adu * 100
                if item.adu and item.adu > 0 else 0.0
            )

            peak_months   = [_MONTH_NAMES[i] for i, idx in enumerate(seasonal) if idx >= 1.2]
            trough_months = [_MONTH_NAMES[i] for i, idx in enumerate(seasonal) if idx <= 0.8]

            results.append({
                "id":               item.id,
                "part_number":      item.part_number,
                "description":      item.description,
                "months_of_data":   len(monthly),
                "mean_monthly":     round(m_val, 1),
                "std_monthly":      round(s_val, 1),
                "cv":               round(cv, 3),
                "trend_slope":      round(slope, 2),
                "sma3_forecast":    round(sma3, 1),
                "stored_adu":       item.adu,
                "implied_adu":      round(adu_daily, 4),
                "adu_divergence_pct": round(adu_div_pct, 1),
                "has_seasonality":  len(monthly) >= 12 and (max(seasonal) - min(seasonal)) >= 0.4,
                "peak_months":      peak_months,
                "trough_months":    trough_months,
            })
        except Exception:
            continue

    return sorted(results, key=lambda x: abs(x["adu_divergence_pct"]), reverse=True)
