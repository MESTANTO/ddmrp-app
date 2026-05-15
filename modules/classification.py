"""
Shared ABC/XYZ classification logic.

Extracted from views/abc_xyz.py so both the Streamlit page and the
Inventory Manager Agent can call it without importing a Streamlit view.
"""

import statistics
from collections import defaultdict

import pandas as pd


# Default thresholds (can be overridden by callers)
ABC_A_THR  = 0.70   # top 70 % of annual value → A
ABC_AB_THR = 0.90   # top 90 % of annual value → A + B
XYZ_X_THR  = 0.50   # CV < 0.50 → X
XYZ_Y_THR  = 1.00   # CV < 1.00 → Y  (else Z)


def compute_abc_xyz(
    items,
    demands,
    abc_a_thr:  float = ABC_A_THR,
    abc_ab_thr: float = ABC_AB_THR,
    xyz_x_thr:  float = XYZ_X_THR,
    xyz_y_thr:  float = XYZ_Y_THR,
) -> pd.DataFrame:
    """
    Classify a list of SQLAlchemy Item objects against their DemandEntry rows.

    Parameters
    ----------
    items   : list of Item ORM objects (already loaded, session may be closed)
    demands : list of DemandEntry ORM objects for those items
    abc_a_thr  : cumulative-value threshold for A class  (default 0.70)
    abc_ab_thr : cumulative-value threshold for A+B class (default 0.90)
    xyz_x_thr  : CV upper bound for X class (default 0.50)
    xyz_y_thr  : CV upper bound for Y class (default 1.00)

    Returns
    -------
    pd.DataFrame with columns:
        id, part_number, description, category, item_type,
        unit_cost, annual_usage, annual_value, cv,
        abc, xyz, acvs
    Returns empty DataFrame if items list is empty.
    """
    if not items:
        return pd.DataFrame()

    demand_by_item: dict = defaultdict(list)
    for d in demands:
        demand_by_item[d.item_id].append(d)

    rows = []
    for item in items:
        item_demands = demand_by_item.get(item.id, [])

        # Annual usage
        if item_demands:
            total_qty  = sum(d.quantity for d in item_demands)
            dates      = [d.demand_date for d in item_demands]
            span_days  = max(1, (max(dates) - min(dates)).days)
            annual_usage = total_qty * 365.0 / span_days
        else:
            annual_usage = (item.adu or 0.0) * 365.0

        annual_value = annual_usage * (item.unit_cost or 0.0)
        cv = _compute_cv(item, item_demands)

        rows.append({
            "id":           item.id,
            "part_number":  item.part_number,
            "description":  item.description,
            "category":     item.category or "",
            "item_type":    item.item_type or "P",
            "unit_cost":    item.unit_cost or 0.0,
            "annual_usage": annual_usage,
            "annual_value": annual_value,
            "cv":           cv,
        })

    df = pd.DataFrame(rows)

    # ABC classification
    df_val  = df[df["annual_value"] > 0].copy()
    df_zero = df[df["annual_value"] <= 0].copy()

    if not df_val.empty:
        df_val = df_val.sort_values("annual_value", ascending=False).reset_index(drop=True)
        total_v = df_val["annual_value"].sum()
        df_val["cum_pct"]      = df_val["annual_value"].cumsum() / total_v
        df_val["cum_pct_prev"] = df_val["cum_pct"].shift(1, fill_value=0.0)
        df_val["abc"] = df_val["cum_pct_prev"].apply(
            lambda c: "A" if c < abc_a_thr else ("B" if c < abc_ab_thr else "C")
        )
        df_zero["abc"] = "C"
        df = pd.concat([df_val, df_zero], ignore_index=True)
    else:
        df["abc"]     = "C"
        df["cum_pct"] = 0.0

    # XYZ classification
    df["xyz"]  = df["cv"].apply(lambda cv: "X" if cv < xyz_x_thr else ("Y" if cv < xyz_y_thr else "Z"))
    df["acvs"] = df["abc"] + "-" + df["xyz"]

    return df


def _compute_cv(item, demands) -> float:
    """Coefficient of variation of weekly demand buckets for one item."""
    if len(demands) >= 4:
        weekly: dict = defaultdict(float)
        for d in demands:
            week_key = d.demand_date.isocalendar()[:2]
            weekly[week_key] += d.quantity

        vals = list(weekly.values())
        if len(vals) >= 2:
            mean_v = statistics.mean(vals)
            if mean_v > 0:
                return statistics.stdev(vals) / mean_v

    return item.variability_factor or 0.0
