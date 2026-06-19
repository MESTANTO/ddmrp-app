"""
DDMRP-vs-MRP methodology simulation.

A time-stepped (daily) Monte-Carlo engine that replays each eligible part under
several replenishment policies over a horizon of N days × R replications, then
reports the inventory-rotation index ("indice di rotazione di stock"), average
stock value and service level so the user can pick the policy that maximises
service while minimising stock.

Policies compared (all driven by the part's own master data — nothing is
duplicated):
  • ddmrp        — DDMRP buffer (Red/Yellow/Green); order TOG−NFP when NFP≤TOY
  • rop_q        — Reorder Point (s,Q): s = ADU×LT,            Q = EOQ/MOQ
  • rop_ss       — ROP + Safety Stock: s = ADU×LT + SS(Z),     Q = EOQ/MOQ
  • kanban       — two-bin: order one container when IP ≤ one container
  • periodic_rs  — Periodic Review (R,S): every R days top up to S

The engine is pure numpy (no DB, no Streamlit) so its `solve(params)` result can
be persisted via `solver_base.record_run` exactly like the other sessions.

Contract: `solve(params: dict) -> SolveResult`.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from modules.optimization.solver_base import SolveResult

# ── Service-level → Z (mirror of safety_stock._Z_TABLE, kept inline so the
#    engine stays import-light / Streamlit-free) ───────────────────────────────
_Z_TABLE = [
    (50.0, 0.00), (75.0, 0.67), (80.0, 0.84), (85.0, 1.04),
    (90.0, 1.28), (92.5, 1.44), (95.0, 1.65), (97.5, 1.96),
    (98.0, 2.05), (99.0, 2.33), (99.5, 2.58), (99.9, 3.09),
]

POLICY_LABELS = {
    "ddmrp":       "DDMRP buffer",
    "rop_q":       "Reorder Point (s,Q)",
    "rop_ss":      "ROP + Safety Stock",
    "kanban":      "Kanban (two-bin)",
    "periodic_rs": "Periodic Review (R,S)",
}

_DEFAULT_ORDERING_COST = 50.0
_DEFAULT_HOLDING_PCT = 0.25


def _z_from_service(sl_pct: float) -> float:
    sl_pct = max(50.0, min(99.9, sl_pct))
    for (s1, z1), (s2, z2) in zip(_Z_TABLE[:-1], _Z_TABLE[1:]):
        if s1 <= sl_pct <= s2:
            if s2 == s1:
                return z1
            return z1 + (z2 - z1) * (sl_pct - s1) / (s2 - s1)
    return 1.65


def _eoq(annual_demand: float, ordering_cost: float,
         holding_pct: float, unit_cost: float) -> float:
    denom = holding_pct * unit_cost
    if denom <= 0 or annual_demand <= 0 or ordering_cost <= 0:
        return 0.0
    return math.sqrt(2 * annual_demand * ordering_cost / denom)


# ── Demand generation ────────────────────────────────────────────────────────

def _gen_demand(rng: np.random.Generator, item: dict, horizon: int,
                mode: str, profile: str) -> np.ndarray:
    """Return a length-`horizon` non-negative daily-demand vector."""
    adu = float(item["adu"])
    hist = item.get("hist_series") or []

    if mode == "bootstrap" and len(hist) > 0 and any(h > 0 for h in hist):
        idx = rng.integers(0, len(hist), size=horizon)
        return np.maximum(np.asarray(hist, dtype=float)[idx], 0.0)

    # Synthetic. Base mean shaped by the chosen profile, noise scaled by CV.
    base = adu if adu > 0 else (float(item.get("hist_mean", 0.0)) or 1.0)
    # CV: prefer historical, else map variability_factor (0..1) to a CV band.
    cv = float(item.get("hist_cv", 0.0))
    if cv <= 0:
        cv = 0.25 + 0.75 * float(item.get("variability_factor", 0.5))

    t = np.arange(horizon)
    if profile == "seasonal":
        period = 30.0
        mean = base * (1.0 + 0.5 * np.sin(2 * np.pi * t / period))
    elif profile == "trending":
        mean = base * (1.0 + 0.6 * (t / max(horizon - 1, 1)))
    elif profile == "lumpy":
        # Intermittent: demand occurs on a fraction of days, larger when it does.
        occur_p = 0.35
        occurs = rng.random(horizon) < occur_p
        sizes = rng.gamma(shape=2.0, scale=base / occur_p / 2.0, size=horizon)
        return np.where(occurs, np.maximum(sizes, 0.0), 0.0)
    else:  # stable
        mean = np.full(horizon, base, dtype=float)

    sigma = np.maximum(mean * cv, 1e-9)
    draw = rng.normal(mean, sigma)
    return np.maximum(draw, 0.0)


# ── Single (item, policy) Monte-Carlo run ────────────────────────────────────

def _simulate(item: dict, policy: str, *, horizon: int, n_reps: int,
              demand_mode: str, demand_profile: str, service_target: float,
              seed: int) -> dict[str, float]:
    def _f(v, default=0.0):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v if math.isfinite(v) else default

    adu = max(_f(item["adu"]), 0.0)
    dlt = max(_f(item["dlt"]), 0.0)
    ltf = _f(item["lead_time_factor"], 0.5)
    vf = _f(item["variability_factor"], 0.5)
    moq = max(_f(item["min_order_qty"]), 0.0)
    oc = max(_f(item["order_cycle"]), 0.0)
    unit_cost = max(_f(item["unit_cost"]), 0.0)
    ordering_cost = item["ordering_cost"] if item["ordering_cost"] > 0 else _DEFAULT_ORDERING_COST
    holding_pct = item["holding_cost_pct"] if item["holding_cost_pct"] > 0 else _DEFAULT_HOLDING_PCT

    # Replenishment lead time: MRP policies use supplier LT (fallback DLT);
    # DDMRP uses the decoupled lead time.
    sup_lt = int(item.get("supplier_lead_time_days") or 0)
    mrp_lt = sup_lt if sup_lt > 0 else dlt
    lead = dlt if policy == "ddmrp" else mrp_lt
    lead_i = max(int(round(lead)), 0)

    z = _z_from_service(service_target * 100.0)
    annual_demand = adu * 365.0
    eoq = _eoq(annual_demand, ordering_cost, holding_pct, unit_cost)
    order_q = max(eoq, moq, adu)  # never order a non-positive lot

    # Demand σ proxy (daily) for safety stock — variability_factor banded.
    cv = float(item.get("hist_cv", 0.0)) or (0.25 + 0.75 * vf)
    sigma_d = adu * cv
    ss = z * sigma_d * math.sqrt(max(lead, 1.0))

    # ── DDMRP zones from master data (standard formulas) ─────────────────────
    red_base = adu * dlt * ltf
    red_zone = red_base * (1.0 + vf)
    yellow_zone = adu * dlt
    green_zone = max(adu * oc, moq, red_base)
    tor = red_zone
    toy = red_zone + yellow_zone
    tog = red_zone + yellow_zone + green_zone

    # Review period for periodic policy.
    review_R = int(round(oc)) if oc >= 1 else 7

    # Policy reorder point / order-up-to for initialisation.
    if policy == "rop_q":
        s_point = adu * lead
        order_up = s_point + order_q
    elif policy == "rop_ss":
        s_point = adu * lead + ss
        order_up = s_point + order_q
    elif policy == "kanban":
        container = max(adu * lead, moq, adu)
        s_point = container
        order_q = container
        order_up = 2 * container
    elif policy == "periodic_rs":
        s_point = 0.0
        order_up = adu * (lead + review_R) + ss
    else:  # ddmrp
        s_point = toy
        order_up = tog

    rng = np.random.default_rng(seed)

    rep_fill, rep_csl, rep_avg_oh = [], [], []
    rep_orders, rep_hold, rep_order_cost = [], [], []
    rep_mean_demand = []

    warmup = min(horizon // 3, max(lead_i * 2, 10))

    for _ in range(n_reps):
        demand = _gen_demand(rng, item, horizon, demand_mode, demand_profile)
        on_hand = float(order_up)               # warm start near steady state
        arrivals = np.zeros(horizon + lead_i + 1, dtype=float)
        on_order = 0.0

        oh_samples = []
        demanded = filled = stockout_days = 0.0
        n_orders = 0
        days_since_review = 0

        for tt in range(horizon):
            # 1. receive
            recv = arrivals[tt]
            if recv:
                on_hand += recv
                on_order -= recv
            # 2. demand (lost sales)
            d = demand[tt]
            sat = min(on_hand, d)
            on_hand -= sat
            ip = on_hand + on_order
            # 3. order decision
            q = 0.0
            if policy == "periodic_rs":
                if days_since_review >= review_R:
                    days_since_review = 0
                    if ip < order_up:
                        q = order_up - ip
                else:
                    days_since_review += 1
            else:
                if ip <= s_point:
                    if policy in ("rop_q", "kanban"):
                        q = order_q
                    else:  # ddmrp, rop_ss top-up to order_up
                        q = order_up - ip
            if q > 0:
                if moq > 0:
                    q = max(q, moq)
                arrivals[tt + lead_i] += q
                on_order += q
                n_orders += 1
            # 4. metrics (post-warmup)
            if tt >= warmup:
                demanded += d
                filled += sat
                if d - sat > 1e-9:
                    stockout_days += 1
                oh_samples.append(on_hand)

        meas_days = max(horizon - warmup, 1)
        avg_oh = float(np.mean(oh_samples)) if oh_samples else 0.0
        fill = (filled / demanded) if demanded > 0 else 1.0
        csl = 1.0 - stockout_days / meas_days
        mean_d = demanded / meas_days

        rep_fill.append(fill)
        rep_csl.append(csl)
        rep_avg_oh.append(avg_oh)
        rep_orders.append(n_orders)
        rep_hold.append(avg_oh * unit_cost * holding_pct)
        # ordering cost over the measured window, annualised later if needed
        rep_order_cost.append(n_orders * ordering_cost)
        rep_mean_demand.append(mean_d)

    avg_oh_units = float(np.mean(rep_avg_oh))
    avg_oh_value = avg_oh_units * unit_cost
    mean_daily_demand = float(np.mean(rep_mean_demand))
    annual_demand_value = mean_daily_demand * 365.0 * unit_cost
    turnover = (annual_demand_value / avg_oh_value) if avg_oh_value > 0 else 0.0

    return {
        "policy":              policy,
        "unit_fill_rate":      round(float(np.mean(rep_fill)), 4),
        "cycle_service_level": round(float(np.mean(rep_csl)), 4),
        "avg_on_hand_units":   round(avg_oh_units, 2),
        "avg_on_hand_value":   round(avg_oh_value, 2),
        "turnover_index":      round(turnover, 2),
        "n_orders":            round(float(np.mean(rep_orders)), 1),
        "annual_holding_cost": round(float(np.mean(rep_hold)), 2),
        "ordering_cost_window": round(float(np.mean(rep_order_cost)), 2),
        "meets_target":        bool(np.mean(rep_fill) >= service_target),
    }


def solve(params: dict[str, Any]) -> SolveResult:
    """
    params:
        items:          list of per-item dicts from load_simulation_inputs
        horizon_days:   simulation length (default 180)
        n_replications: Monte-Carlo reps (default 30)
        demand_mode:    'synthetic' | 'bootstrap'
        demand_profile: 'stable' | 'seasonal' | 'lumpy' | 'trending'
        policies:       list of policy keys (ddmrp always included)
        service_target: target unit fill rate (0..1), default 0.95
        seed:           RNG seed (default 42)
    """
    t0 = time.perf_counter()

    items = params.get("items") or []
    horizon = int(params.get("horizon_days", 180))
    n_reps = int(params.get("n_replications", 30))
    demand_mode = params.get("demand_mode", "synthetic")
    demand_profile = params.get("demand_profile", "stable")
    service_target = float(params.get("service_target", 0.95))
    seed = int(params.get("seed", 42))
    policies = params.get("policies") or list(POLICY_LABELS.keys())
    if "ddmrp" not in policies:
        policies = ["ddmrp", *policies]

    if not items:
        return SolveResult(
            status="failed", objective_value=None,
            solver_used="monte-carlo-sim",
            runtime_ms=int((time.perf_counter() - t0) * 1000),
            error_message="No items with demand to simulate. Set ADU / demand "
                          "history in Material Master first.",
        )

    rows: list[dict] = []
    recommendation: list[dict] = []

    for idx, it in enumerate(items):
        per_policy: dict[str, dict] = {}
        for pol in policies:
            res = _simulate(
                it, pol, horizon=horizon, n_reps=n_reps,
                demand_mode=demand_mode, demand_profile=demand_profile,
                service_target=service_target, seed=seed + idx * 97,
            )
            res.update({
                "item_id":     it["id"],
                "part_number": it["part_number"],
                "description": it["description"],
                "unit_cost":   it["unit_cost"],
                "is_ddmrp_eligible": it["is_ddmrp_eligible"],
            })
            rows.append(res)
            per_policy[pol] = res

        # Recommendation: cheapest stock among policies meeting the target;
        # if none meet it, the one with the highest fill rate.
        meeting = [p for p in per_policy.values() if p["meets_target"]]
        if meeting:
            best = min(meeting, key=lambda p: p["avg_on_hand_value"])
        else:
            best = max(per_policy.values(), key=lambda p: p["unit_fill_rate"])

        ddmrp = per_policy["ddmrp"]
        mrp_only = {k: v for k, v in per_policy.items() if k != "ddmrp"}
        best_mrp = (min(mrp_only.values(), key=lambda p: p["avg_on_hand_value"])
                    if mrp_only else None)

        recommendation.append({
            "item_id":           it["id"],
            "part_number":       it["part_number"],
            "description":       it["description"],
            "is_ddmrp_eligible": it["is_ddmrp_eligible"],
            "best_policy":       best["policy"],
            "best_policy_label": POLICY_LABELS.get(best["policy"], best["policy"]),
            "best_fill":         best["unit_fill_rate"],
            "best_stock_value":  best["avg_on_hand_value"],
            "best_turnover":     best["turnover_index"],
            "ddmrp_fill":        ddmrp["unit_fill_rate"],
            "ddmrp_stock_value": ddmrp["avg_on_hand_value"],
            "ddmrp_turnover":    ddmrp["turnover_index"],
            "best_mrp_policy":   best_mrp["policy"] if best_mrp else None,
            "best_mrp_stock_value": best_mrp["avg_on_hand_value"] if best_mrp else None,
            "ddmrp_stock_delta_vs_mrp": (
                round(ddmrp["avg_on_hand_value"] - best_mrp["avg_on_hand_value"], 2)
                if best_mrp else None
            ),
            "ddmrp_wins": bool(best["policy"] == "ddmrp"),
        })

    # ── Aggregate summary per policy ─────────────────────────────────────────
    policy_summary = []
    for pol in policies:
        prows = [r for r in rows if r["policy"] == pol]
        if not prows:
            continue
        policy_summary.append({
            "policy":            pol,
            "policy_label":      POLICY_LABELS.get(pol, pol),
            "avg_fill_rate":     round(float(np.mean([r["unit_fill_rate"] for r in prows])), 4),
            "avg_csl":           round(float(np.mean([r["cycle_service_level"] for r in prows])), 4),
            "total_stock_value": round(float(np.sum([r["avg_on_hand_value"] for r in prows])), 2),
            "avg_turnover":      round(float(np.mean([r["turnover_index"] for r in prows])), 2),
            "total_holding_cost": round(float(np.sum([r["annual_holding_cost"] for r in prows])), 2),
            "n_items":           len(prows),
        })

    ddmrp_total = next((s["total_stock_value"] for s in policy_summary if s["policy"] == "ddmrp"), 0.0)
    n_ddmrp_wins = sum(1 for r in recommendation if r["ddmrp_wins"])

    summary = {
        "n_items":         len(items),
        "n_policies":      len(policies),
        "horizon_days":    horizon,
        "n_replications":  n_reps,
        "demand_mode":     demand_mode,
        "demand_profile":  demand_profile,
        "service_target":  service_target,
        "ddmrp_total_stock_value": ddmrp_total,
        "n_ddmrp_recommended": n_ddmrp_wins,
        "policy_summary":  policy_summary,
    }

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return SolveResult(
        status="solved",
        objective_value=float(ddmrp_total),
        solver_used="monte-carlo-sim",
        runtime_ms=elapsed_ms,
        artefacts=[
            ("summary",        summary),
            ("by_item_policy", {"rows": rows}),
            ("recommendation", {"rows": recommendation}),
        ],
    )
