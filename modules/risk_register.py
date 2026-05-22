"""
Risk Register — Supply chain risk catalog with Tang's 9 mitigation strategies.

Reference:
  Hosseinzadeh Lotfi, F., et al. (2023).
  *Supply Chain Performance Evaluation: Application of Data Envelopment Analysis.*
  Springer.  Ch. 5 — risk taxonomy (operational vs disruption, internal vs
  external, per-node) plus Tang's 9 disruption mitigation strategies.

A risk row binds together:
  - a target (whole company, a supplier, or one item)
  - a taxonomy (category + node)
  - inherent risk score = likelihood × impact   (1-25)
  - a mitigation strategy from Tang's nine + residual score after mitigation
  - workflow status:  open → mitigating → (accepted | closed)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from database.db import (
    SessionLocal,
    SupplierRisk,
    Supplier,
    Item,
)


# Allowed enums — referenced by the UI and agent allowlists
RISK_CATEGORIES = [
    "operational",      # day-to-day supply variability, quality slips
    "disruption",       # rare, high-impact events (factory fire, port closure)
    "financial",        # supplier insolvency, FX, payment terms
    "quality",          # defects, rework, recalls
    "compliance",       # regulatory, ESG, audit failure
    "geopolitical",     # tariffs, sanctions, export controls
    "environmental",    # natural disasters, climate, water scarcity
]

RISK_NODES = [
    "supplier", "producer", "distributor", "customer", "internal", "network",
]

# Tang's 9 mitigation strategies (Lotfi Ch. 5, citing Tang 2006) + 'other'
TANG_STRATEGIES = [
    "postponement",          # delay differentiation until demand is known
    "strategic_stock",       # buffer inventory at key points
    "flexible_supply",       # multi-source, dual-source
    "make_and_buy",          # split between in-house and outsource
    "economic_incentives",   # reward suppliers for performance/diversification
    "flexible_transportation",  # multi-modal, multi-carrier
    "revenue_management",    # dynamic pricing to steer demand
    "dynamic_assortment",    # shift portfolio toward available SKUs
    "silent_rollover",       # phase products gradually, no hard cutovers
    "other",
]

RISK_STATUSES = ["open", "mitigating", "accepted", "closed"]

# Fields the chat agent may set on a risk
RISK_FIELDS = {
    "supplier_id", "item_id", "title", "description", "category", "node",
    "likelihood", "impact", "mitigation_strategy", "mitigation_notes",
    "residual_likelihood", "residual_impact", "status", "owner", "due_date",
}


@dataclass
class RiskSummary:
    open_count: int
    mitigating_count: int
    closed_count: int
    accepted_count: int
    total_inherent_exposure: int      # sum of inherent_score for open+mitigating
    total_residual_exposure: int      # sum of residual_score (fallback to inherent) for open+mitigating
    top_risks: list                   # top 5 by inherent_score, status in (open, mitigating)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _clip(n, lo=1, hi=5):
    try:
        v = int(n)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def compute_score(likelihood, impact) -> int:
    """Inherent or residual risk score = L × I, both clipped to 1-5."""
    return _clip(likelihood) * _clip(impact)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_risk(company_id: int, fields: dict) -> int:
    """Insert a new SupplierRisk row. Returns the new id."""
    cleaned = _clean_fields(fields)
    if not cleaned.get("title"):
        raise ValueError("title is required")

    likelihood = _clip(cleaned.get("likelihood", 3))
    impact     = _clip(cleaned.get("impact",     3))
    inherent   = likelihood * impact

    r_l = cleaned.get("residual_likelihood")
    r_i = cleaned.get("residual_impact")
    residual = (_clip(r_l) * _clip(r_i)) if (r_l is not None and r_i is not None) else None

    session = SessionLocal()
    try:
        row = SupplierRisk(
            company_id   = company_id,
            supplier_id  = cleaned.get("supplier_id"),
            item_id      = cleaned.get("item_id"),
            title        = cleaned["title"].strip(),
            description  = cleaned.get("description", "").strip(),
            category     = cleaned.get("category", "operational"),
            node         = cleaned.get("node", "supplier"),
            likelihood   = likelihood,
            impact       = impact,
            inherent_score = inherent,
            mitigation_strategy = cleaned.get("mitigation_strategy", "other"),
            mitigation_notes    = cleaned.get("mitigation_notes", "").strip(),
            residual_likelihood = _clip(r_l) if r_l is not None else None,
            residual_impact     = _clip(r_i) if r_i is not None else None,
            residual_score      = residual,
            status       = cleaned.get("status", "open"),
            owner        = cleaned.get("owner", "").strip(),
            due_date     = _parse_date(cleaned.get("due_date")),
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def update_risk(company_id: int, risk_id: int, fields: dict) -> bool:
    """Apply field updates to an existing risk. Returns True on success."""
    cleaned = _clean_fields(fields)
    session = SessionLocal()
    try:
        row = session.query(SupplierRisk).get(int(risk_id))
        if row is None or row.company_id != company_id:
            return False
        for k, v in cleaned.items():
            if k == "due_date":
                v = _parse_date(v)
            elif k in ("likelihood", "impact"):
                v = _clip(v)
            elif k in ("residual_likelihood", "residual_impact"):
                v = _clip(v) if v is not None else None
            setattr(row, k, v)
        # Recompute scores
        row.inherent_score = _clip(row.likelihood) * _clip(row.impact)
        if row.residual_likelihood is not None and row.residual_impact is not None:
            row.residual_score = _clip(row.residual_likelihood) * _clip(row.residual_impact)
        else:
            row.residual_score = None
        row.updated_at = datetime.utcnow()
        session.commit()
        return True
    finally:
        session.close()


def delete_risk(company_id: int, risk_id: int) -> bool:
    session = SessionLocal()
    try:
        row = session.query(SupplierRisk).get(int(risk_id))
        if row is None or row.company_id != company_id:
            return False
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def list_risks(
    company_id: int,
    status: Optional[str] = None,
    supplier_id: Optional[int] = None,
    item_id: Optional[int] = None,
    category: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Return risks (newest first), optionally filtered."""
    session = SessionLocal()
    try:
        q = session.query(SupplierRisk).filter(SupplierRisk.company_id == company_id)
        if status:      q = q.filter(SupplierRisk.status == status)
        if supplier_id: q = q.filter(SupplierRisk.supplier_id == int(supplier_id))
        if item_id:     q = q.filter(SupplierRisk.item_id == int(item_id))
        if category:    q = q.filter(SupplierRisk.category == category)
        q = q.order_by(SupplierRisk.inherent_score.desc(),
                       SupplierRisk.created_at.desc()).limit(int(limit))
        rows = q.all()

        # Resolve supplier code + item part_number for display
        sup_ids = {r.supplier_id for r in rows if r.supplier_id}
        itm_ids = {r.item_id for r in rows if r.item_id}
        sup_map = {s.id: s for s in
                   session.query(Supplier).filter(Supplier.id.in_(sup_ids)).all()} if sup_ids else {}
        itm_map = {i.id: i for i in
                   session.query(Item).filter(Item.id.in_(itm_ids)).all()} if itm_ids else {}

        out = []
        for r in rows:
            sup = sup_map.get(r.supplier_id) if r.supplier_id else None
            it  = itm_map.get(r.item_id) if r.item_id else None
            out.append({
                "id":                  r.id,
                "title":               r.title,
                "description":         r.description or "",
                "category":            r.category,
                "node":                r.node,
                "supplier_id":         r.supplier_id,
                "supplier_code":       sup.code if sup else None,
                "supplier_name":       sup.name if sup else None,
                "item_id":             r.item_id,
                "part_number":         it.part_number if it else None,
                "likelihood":          r.likelihood,
                "impact":              r.impact,
                "inherent_score":      r.inherent_score,
                "mitigation_strategy": r.mitigation_strategy,
                "mitigation_notes":    r.mitigation_notes or "",
                "residual_likelihood": r.residual_likelihood,
                "residual_impact":     r.residual_impact,
                "residual_score":      r.residual_score,
                "status":              r.status,
                "owner":               r.owner or "",
                "due_date":            r.due_date.date().isoformat() if r.due_date else None,
                "created_at":          r.created_at.isoformat() if r.created_at else None,
                "updated_at":          r.updated_at.isoformat() if r.updated_at else None,
            })
        return out
    finally:
        session.close()


def risk_summary(company_id: int) -> RiskSummary:
    """Aggregate counts + exposure across the register."""
    session = SessionLocal()
    try:
        rows = (session.query(SupplierRisk)
                .filter(SupplierRisk.company_id == company_id)
                .all())
    finally:
        session.close()

    counts = {s: 0 for s in RISK_STATUSES}
    inh = 0
    res = 0
    open_or_mit = []
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("open", "mitigating"):
            inh += int(r.inherent_score or 0)
            res += int(r.residual_score if r.residual_score is not None else (r.inherent_score or 0))
            open_or_mit.append(r)

    open_or_mit.sort(key=lambda r: int(r.inherent_score or 0), reverse=True)
    top = [
        {"id": r.id, "title": r.title, "score": r.inherent_score,
         "supplier_id": r.supplier_id, "item_id": r.item_id,
         "status": r.status, "strategy": r.mitigation_strategy}
        for r in open_or_mit[:5]
    ]

    return RiskSummary(
        open_count        = counts.get("open", 0),
        mitigating_count  = counts.get("mitigating", 0),
        accepted_count    = counts.get("accepted", 0),
        closed_count      = counts.get("closed", 0),
        total_inherent_exposure = inh,
        total_residual_exposure = res,
        top_risks               = top,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_fields(fields: dict | None) -> dict:
    if not isinstance(fields, dict):
        return {}
    return {k: v for k, v in fields.items() if k in RISK_FIELDS}


def _parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
    try:
        return datetime.combine(v, datetime.min.time())
    except Exception:
        return None
