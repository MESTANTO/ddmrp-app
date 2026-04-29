"""
Global Settings page — Streamlit page.

Manages the singleton Settings row:
  • Default Spike Horizon (days) — fallback when item.spike_horizon_days is NULL
  • Default Spike Threshold Factor (× ADU) — fallback when item.spike_threshold_factor is NULL
  • Default ADU Window (days) — rolling demand window for Dynamic ADU
  • Default Ordering Cost (€/order) — used by Safety Stock & EOQ when item.ordering_cost = 0
  • Default Holding Cost % (annual %) — used by Safety Stock & EOQ when item.holding_cost_pct = 0
"""

from __future__ import annotations

import streamlit as st
from database.db import get_session, Settings
from modules.buffer_engine import ADU_WINDOW_DAYS


# ---------------------------------------------------------------------------
# Settings schema extended columns (migrate gracefully)
# ---------------------------------------------------------------------------

_EXTRA_DEFAULTS = {
    "default_adu_window_days":  ADU_WINDOW_DAYS,
    "default_ordering_cost":    0.0,
    "default_holding_cost_pct": 0.25,
}


def _ensure_settings_columns():
    """
    Add extra columns to the settings table if they don't exist yet.
    Called once on page load — idempotent.
    """
    from database.db import _add_columns_safely
    _add_columns_safely("settings", [
        ("default_adu_window_days",  "INTEGER DEFAULT 7",      "INTEGER DEFAULT 7"),
        ("default_ordering_cost",    "REAL DEFAULT 0.0",       "DOUBLE PRECISION DEFAULT 0.0"),
        ("default_holding_cost_pct", "REAL DEFAULT 0.25",      "DOUBLE PRECISION DEFAULT 0.25"),
    ])


def _load_settings() -> Settings:
    session = get_session()
    try:
        s = session.query(Settings).first()
        if s is None:
            s = Settings(
                default_spike_horizon_days=0,
                default_spike_threshold_factor=2.0,
            )
            session.add(s)
            session.commit()
        return s
    finally:
        session.close()


def _get(s: Settings, attr: str, default):
    return getattr(s, attr, None) or default


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def show():
    st.header("⚙️ Global Settings")
    st.caption(
        "Default parameters used app-wide. Item-level overrides in Material Master "
        "take precedence over these values."
    )

    _ensure_settings_columns()
    s = _load_settings()

    st.divider()

    # ── ASOH defaults (slide 83) ─────────────────────────────────────────────
    st.subheader("ASOH — Spike Detection (slide 83)")
    st.caption(
        "Applied when an item's Spike Horizon / Spike Threshold Factor is blank."
    )
    col1, col2 = st.columns(2)
    with col1:
        spike_horizon = st.number_input(
            "Default Spike Horizon (days)",
            min_value=0, max_value=365,
            value=int(_get(s, "default_spike_horizon_days", 0)),
            step=1,
            help="0 = use item's DLT as the horizon.",
        )
    with col2:
        spike_factor = st.number_input(
            "Default Spike Threshold Factor (× ADU)",
            min_value=0.1, max_value=20.0,
            value=float(_get(s, "default_spike_threshold_factor", 2.0)),
            step=0.1,
            help="A demand entry exceeding ADU × Factor is classified as a spike.",
        )

    st.divider()

    # ── ADU window ──────────────────────────────────────────────────────────
    st.subheader("Dynamic ADU Window")
    st.caption("Rolling window used by the buffer engine to compute dynamic ADU.")
    adu_window = st.number_input(
        "Default ADU Window (days)",
        min_value=1, max_value=365,
        value=int(_get(s, "default_adu_window_days", ADU_WINDOW_DAYS)),
        step=1,
        help=f"Default is {ADU_WINDOW_DAYS} days (1 week). Increase for slower-moving items.",
    )

    st.divider()

    # ── Cost defaults (Safety Stock & EOQ) ──────────────────────────────────
    st.subheader("Cost Defaults — Safety Stock & EOQ")
    st.caption(
        "Used when an item's ordering or holding cost fields are left at 0. "
        "Set here once to avoid re-entering on every item."
    )
    col3, col4 = st.columns(2)
    with col3:
        ordering_cost = st.number_input(
            "Default Ordering Cost (€ per order)",
            min_value=0.0, max_value=100_000.0,
            value=float(_get(s, "default_ordering_cost", 0.0)),
            step=10.0,
        )
    with col4:
        holding_pct = st.number_input(
            "Default Holding Cost (% annual, e.g. 25)",
            min_value=0.0, max_value=100.0,
            value=float(_get(s, "default_holding_cost_pct", 0.25)) * 100.0,
            step=1.0,
            help="Enter as percentage, e.g. 25 for 25 % annual holding cost.",
        )

    st.divider()

    if st.button("💾 Save Settings", type="primary"):
        session = get_session()
        try:
            obj = session.query(Settings).first()
            if obj is None:
                obj = Settings()
                session.add(obj)

            obj.default_spike_horizon_days    = int(spike_horizon)
            obj.default_spike_threshold_factor = float(spike_factor)

            # Extra columns — set via setattr to survive missing column gracefully
            try:
                obj.default_adu_window_days  = int(adu_window)
                obj.default_ordering_cost    = float(ordering_cost)
                obj.default_holding_cost_pct = float(holding_pct) / 100.0
            except Exception:
                pass

            session.commit()
            st.success("✅ Settings saved.")
        except Exception as e:
            session.rollback()
            st.error(f"Error saving settings: {e}")
        finally:
            session.close()

    # ── Current effective values summary ────────────────────────────────────
    st.divider()
    st.subheader("Current Effective Values")
    st.caption("What the engine will use today:")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spike Horizon",   f"{int(_get(s, 'default_spike_horizon_days', 0))} d"
                                  if _get(s, 'default_spike_horizon_days', 0) else "= DLT")
    c2.metric("Spike Threshold", f"× {_get(s, 'default_spike_threshold_factor', 2.0):.1f} ADU")
    c3.metric("ADU Window",      f"{int(_get(s, 'default_adu_window_days', ADU_WINDOW_DAYS))} d")
    c4.metric("Ordering Cost",   f"€ {_get(s, 'default_ordering_cost', 0.0):,.0f}")
    c5.metric("Holding Cost",    f"{_get(s, 'default_holding_cost_pct', 0.25) * 100:.0f} %")
