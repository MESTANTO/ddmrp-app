"""
Shared persistence + lifecycle helpers for all optimization sessions.

Each session implements `solve(company_id, params) -> SolveResult` and the
view layer calls `record_run(...)` to persist inputs + outputs against an
`OptimizationRun` row. Snapshots auto-expire 10 days after creation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from database.db import (
    OptimizationRun,
    OptimizationResult,
    SessionLocal,
)

SNAPSHOT_TTL_DAYS = 10


@dataclass
class SolveResult:
    """Container returned by every session's solve() function."""
    status: str                          # solved | infeasible | unbounded | failed
    objective_value: float | None
    solver_used: str
    runtime_ms: int
    artefacts: list[tuple[str, dict]] = field(default_factory=list)
    # list of (kind, payload_dict) — kind ∈ {allocation, schedule, route,
    # sensitivity, summary, kpi, chart_data}
    error_message: str | None = None


def record_run(
    *,
    company_id: int,
    user_id: int | None,
    session_key: str,
    scenario_name: str,
    params: dict[str, Any],
    result: SolveResult,
) -> int:
    """
    Persist an OptimizationRun row + one OptimizationResult per artefact.
    Returns the new run id.
    """
    now = datetime.utcnow()
    expires = now + timedelta(days=SNAPSHOT_TTL_DAYS)
    sess = SessionLocal()
    try:
        run = OptimizationRun(
            company_id=company_id,
            user_id=user_id,
            session_key=session_key,
            scenario_name=scenario_name or "",
            params_json=json.dumps(params, default=str),
            status=result.status,
            objective_value=result.objective_value,
            solver_used=result.solver_used,
            runtime_ms=result.runtime_ms,
            error_message=result.error_message,
            created_at=now,
            expires_at=expires,
        )
        sess.add(run)
        sess.flush()
        for kind, payload in result.artefacts:
            sess.add(OptimizationResult(
                run_id=run.id,
                kind=kind,
                payload_json=json.dumps(payload, default=str),
            ))
        sess.commit()
        return run.id
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def list_runs(company_id: int, session_key: str | None = None, limit: int = 25):
    """Return recent runs for the company, optionally filtered by session."""
    sess = SessionLocal()
    try:
        q = sess.query(OptimizationRun).filter(OptimizationRun.company_id == company_id)
        if session_key:
            q = q.filter(OptimizationRun.session_key == session_key)
        return q.order_by(OptimizationRun.created_at.desc()).limit(limit).all()
    finally:
        sess.close()


def load_run(run_id: int, company_id: int) -> tuple[OptimizationRun, list[OptimizationResult]] | None:
    """Fetch one run + all its artefacts (company-scoped)."""
    sess = SessionLocal()
    try:
        run = sess.query(OptimizationRun).filter(
            OptimizationRun.id == run_id,
            OptimizationRun.company_id == company_id,
        ).first()
        if run is None:
            return None
        artefacts = sess.query(OptimizationResult).filter(
            OptimizationResult.run_id == run.id
        ).all()
        return run, artefacts
    finally:
        sess.close()


def timed(func):
    """Decorator that wraps a solve() function and returns elapsed ms."""
    def _wrap(*a, **kw):
        t0 = time.perf_counter()
        out = func(*a, **kw)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return out, elapsed_ms
    return _wrap
