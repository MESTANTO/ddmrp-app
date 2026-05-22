"""
Total Cost of Ownership (TCO) — Supplier evaluation engine.

Reference:
  Hosseinzadeh Lotfi, F., et al. (2023).
  *Supply Chain Performance Evaluation: Application of Data Envelopment
  Analysis.* Springer. Ch. 4 — supplier selection considers price plus
  quality, delivery, service, technology and risk costs.

The book's central claim: selecting a supplier on unit price alone hides
the real cost of doing business with them. TCO surrounds the price with
five surrounding cost buckets and reports cost-per-unit so suppliers can
be ranked on the same basis.

Cost components
---------------
  purchase_cost     = Σ units × unit_price (from supply_entries + items.unit_cost)
  quality_cost      = (1 − reliability_pct/100) × purchase_cost × QUALITY_FACTOR
                      defects → returns, rework, scrap; proxied by reliability deficit
  delivery_cost     = (1 − reliability_pct/100) × purchase_cost × DELIVERY_FACTOR
                      late deliveries → expediting, freight upgrades
  service_cost      = purchase_cost × SERVICE_FACTOR    (admin / after-sales overhead)
  technology_cost   = 0 by default (user-entered: tooling, integration spend)
  risk_cost         = open risks for the supplier — sum(inherent_score × IMPACT_€)
                      uses supplier_risks rows; 0 if none

  total_cost        = purchase_cost + quality + delivery + service + technology + risk
  tco_per_unit      = total_cost / units_delivered

All factors are configurable; defaults are conservative (small percentages)
so a perfectly reliable supplier with no risks essentially has TCO ≈ purchase.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional

from database.db import (
    SessionLocal,
    Supplier,
    Item,
    SupplyEntry,
    SupplierTCO,
    SupplierRisk,
)


# Default factors — tunable; conservative so honest suppliers don't get punished.
QUALITY_FACTOR  = 0.5    # every 1% reliability deficit → 0.5% of purchase as quality cost
DELIVERY_FACTOR = 0.3    # every 1% reliability deficit → 0.3% of purchase as expediting
SERVICE_FACTOR  = 0.02   # 2% of purchase across the board (admin/after-sales)
RISK_IMPACT_EUR = 1_000  # € per 1 point of inherent_score (1-25); rough.


@dataclass
class TCOResult:
    supplier_id: int
    supplier_code: str
    supplier_name: str
    period_start: str
    period_end: str
    units_delivered: float
    unit_price_avg: float
    purchase_cost: float
    quality_cost: float
    delivery_cost: float
    service_cost: float
    technology_cost: float
    risk_cost: float
    total_cost: float
    tco_per_unit: float
    reliability_pct: float
    open_risks: int
    notes: str = ""


def _round(x, n=2):
    try:
        return round(float(x), n)
    except Exception:
        return 0.0


def compute_tco(
    supplier_id: int,
    company_id: int,
    period_start: Optional[datetime] = None,
    period_end:   Optional[datetime] = None,
    quality_factor:  float = QUALITY_FACTOR,
    delivery_factor: float = DELIVERY_FACTOR,
    service_factor:  float = SERVICE_FACTOR,
    risk_impact_eur: float = RISK_IMPACT_EUR,
    technology_cost_override: Optional[float] = None,
) -> TCOResult:
    """
    Auto-compute a TCO snapshot for one supplier over a period.

    Pulls units_delivered + purchase_cost from supply_entries × item.unit_cost,
    derives quality/delivery costs from supplier.reliability_pct, sums open
    supplier_risks for the risk_cost. Does NOT persist; call `save_tco()` to
    write to supplier_tco.
    """
    if period_end is None:
        period_end = datetime.utcnow()
    if period_start is None:
        period_start = period_end - timedelta(days=365)

    session = SessionLocal()
    try:
        sup = session.query(Supplier).get(int(supplier_id))
        if sup is None or sup.company_id != company_id:
            raise ValueError(f"Supplier id={supplier_id} not found for this company")

        # Aggregate supply volume × unit_cost across all items sourced from this supplier
        # in the period. We use Item.default_supplier_id as the supplier link.
        items = (session.query(Item)
                 .filter(Item.default_supplier_id == sup.id,
                         Item.company_id == company_id)
                 .all())
        item_ids = [i.id for i in items]
        unit_cost_by_item = {i.id: float(i.unit_cost or 0.0) for i in items}

        units = 0.0
        purchase = 0.0
        if item_ids:
            entries = (session.query(SupplyEntry)
                       .filter(SupplyEntry.item_id.in_(item_ids),
                               SupplyEntry.due_date >= period_start,
                               SupplyEntry.due_date <= period_end)
                       .all())
            for e in entries:
                qty = float(e.quantity or 0.0)
                price = unit_cost_by_item.get(e.item_id, 0.0)
                units    += qty
                purchase += qty * price

        unit_price_avg = (purchase / units) if units > 0 else 0.0
        reliability = float(sup.reliability_pct or 100.0)
        deficit_pct = max(0.0, 100.0 - reliability)   # 0-100

        # Cost components
        quality   = purchase * (deficit_pct / 100.0) * quality_factor
        delivery  = purchase * (deficit_pct / 100.0) * delivery_factor
        service   = purchase * service_factor
        technology = (float(technology_cost_override)
                      if technology_cost_override is not None else 0.0)

        # Risk cost — sum inherent scores of open/mitigating risks tagged to this supplier
        open_risks = (session.query(SupplierRisk)
                      .filter(SupplierRisk.supplier_id == sup.id,
                              SupplierRisk.company_id == company_id,
                              SupplierRisk.status.in_(["open", "mitigating"]))
                      .all())
        risk_score_sum = sum(int(r.inherent_score or 0) for r in open_risks)
        risk_cost = risk_score_sum * float(risk_impact_eur)

        total = purchase + quality + delivery + service + technology + risk_cost
        per_unit = (total / units) if units > 0 else 0.0

        return TCOResult(
            supplier_id    = sup.id,
            supplier_code  = sup.code,
            supplier_name  = sup.name,
            period_start   = period_start.date().isoformat(),
            period_end     = period_end.date().isoformat(),
            units_delivered= _round(units, 2),
            unit_price_avg = _round(unit_price_avg, 4),
            purchase_cost  = _round(purchase, 2),
            quality_cost   = _round(quality, 2),
            delivery_cost  = _round(delivery, 2),
            service_cost   = _round(service, 2),
            technology_cost= _round(technology, 2),
            risk_cost      = _round(risk_cost, 2),
            total_cost     = _round(total, 2),
            tco_per_unit   = _round(per_unit, 4),
            reliability_pct= _round(reliability, 2),
            open_risks     = len(open_risks),
            notes          = "",
        )
    finally:
        session.close()


def compute_tco_all(
    company_id: int,
    period_start: Optional[datetime] = None,
    period_end:   Optional[datetime] = None,
    **kwargs,
) -> list[TCOResult]:
    """Compute TCO for every active supplier in the company."""
    session = SessionLocal()
    try:
        suppliers = (session.query(Supplier)
                     .filter(Supplier.company_id == company_id)
                     .all())
        sup_ids = [s.id for s in suppliers]
    finally:
        session.close()

    out: list[TCOResult] = []
    for sid in sup_ids:
        try:
            out.append(compute_tco(sid, company_id,
                                   period_start=period_start,
                                   period_end=period_end, **kwargs))
        except Exception as exc:
            print(f"TCO error for supplier id={sid}: {exc}")
    # Rank: lowest tco_per_unit first (ties → lowest total_cost)
    out.sort(key=lambda r: (r.tco_per_unit if r.tco_per_unit > 0 else float("inf"),
                            r.total_cost))
    return out


def save_tco(
    company_id: int,
    result: TCOResult,
    auto_computed: bool = True,
    overrides: Optional[dict] = None,
) -> int:
    """
    Persist a TCO snapshot to supplier_tco. If `overrides` is given, each
    listed component replaces the auto-computed value and the row is
    flagged `auto_computed=False`.
    Returns the new row id.
    """
    overrides = overrides or {}
    session = SessionLocal()
    try:
        # Re-fetch the dataclass values, then layer overrides
        payload = {
            "units_delivered": result.units_delivered,
            "unit_price_avg":  result.unit_price_avg,
            "purchase_cost":   result.purchase_cost,
            "quality_cost":    result.quality_cost,
            "delivery_cost":   result.delivery_cost,
            "service_cost":    result.service_cost,
            "technology_cost": result.technology_cost,
            "risk_cost":       result.risk_cost,
        }
        for k, v in overrides.items():
            if k in payload and v is not None:
                payload[k] = float(v)

        total = (payload["purchase_cost"] + payload["quality_cost"]
                 + payload["delivery_cost"] + payload["service_cost"]
                 + payload["technology_cost"] + payload["risk_cost"])
        per_unit = (total / payload["units_delivered"]
                    if payload["units_delivered"] > 0 else 0.0)

        row = SupplierTCO(
            company_id   = company_id,
            supplier_id  = result.supplier_id,
            period_start = datetime.fromisoformat(result.period_start),
            period_end   = datetime.fromisoformat(result.period_end),
            units_delivered = payload["units_delivered"],
            unit_price_avg  = payload["unit_price_avg"],
            purchase_cost   = payload["purchase_cost"],
            quality_cost    = payload["quality_cost"],
            delivery_cost   = payload["delivery_cost"],
            service_cost    = payload["service_cost"],
            technology_cost = payload["technology_cost"],
            risk_cost       = payload["risk_cost"],
            total_cost      = _round(total, 2),
            tco_per_unit    = _round(per_unit, 4),
            auto_computed   = bool(auto_computed and not overrides),
            notes           = result.notes or "",
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def list_tco_snapshots(company_id: int, supplier_id: Optional[int] = None,
                       limit: int = 50) -> list[dict]:
    """Return saved TCO snapshots, newest first."""
    session = SessionLocal()
    try:
        q = (session.query(SupplierTCO, Supplier)
             .join(Supplier, SupplierTCO.supplier_id == Supplier.id)
             .filter(SupplierTCO.company_id == company_id))
        if supplier_id:
            q = q.filter(SupplierTCO.supplier_id == int(supplier_id))
        q = q.order_by(SupplierTCO.created_at.desc()).limit(int(limit))
        out = []
        for tco, sup in q.all():
            out.append({
                "id":             tco.id,
                "supplier_id":    sup.id,
                "supplier_code":  sup.code,
                "supplier_name":  sup.name,
                "period_start":   tco.period_start.date().isoformat() if tco.period_start else None,
                "period_end":     tco.period_end.date().isoformat() if tco.period_end else None,
                "units_delivered":tco.units_delivered,
                "purchase_cost":  tco.purchase_cost,
                "quality_cost":   tco.quality_cost,
                "delivery_cost":  tco.delivery_cost,
                "service_cost":   tco.service_cost,
                "technology_cost":tco.technology_cost,
                "risk_cost":      tco.risk_cost,
                "total_cost":     tco.total_cost,
                "tco_per_unit":   tco.tco_per_unit,
                "auto_computed":  bool(tco.auto_computed),
                "notes":          tco.notes or "",
                "created_at":     tco.created_at.isoformat() if tco.created_at else None,
            })
        return out
    finally:
        session.close()


def delete_tco_snapshot(snapshot_id: int, company_id: int) -> bool:
    session = SessionLocal()
    try:
        row = session.query(SupplierTCO).get(int(snapshot_id))
        if row is None or row.company_id != company_id:
            return False
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def to_dict(r: TCOResult) -> dict:
    return asdict(r)
