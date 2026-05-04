"""
Database schema and session management for the DDMRP application.
Supports PostgreSQL (Supabase) via DATABASE_URL secret, with SQLite fallback.
"""

from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, ForeignKey, Text, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_database_url() -> str:
    """
    Resolve the database URL in priority order:
      1. DATABASE_URL environment variable (Docker / CI / Streamlit Cloud env)
      2. Streamlit secrets (local .streamlit/secrets.toml or Cloud secrets panel)
      3. Local SQLite fallback (offline dev without any secrets file)
    """
    # 1. Environment variable
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url

    # 2. Streamlit secrets
    try:
        import streamlit as st
        url = st.secrets.get("DATABASE_URL", "")
        if url:
            return url
    except Exception:
        pass

    # 3. SQLite fallback
    db_path = os.path.join(BASE_DIR, "ddmrp.db")
    return f"sqlite:///{db_path}"


DATABASE_URL = _get_database_url()

# PostgreSQL needs pool_pre_ping to handle idle connection drops
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# Material Master
# ---------------------------------------------------------------------------

class Supplier(Base):
    """Supplier master data."""
    __tablename__ = "suppliers"

    id   = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)

    # Location
    country = Column(String, default="")
    city    = Column(String, default="")
    address = Column(Text,   default="")

    # General contact
    website = Column(String, default="")
    phone   = Column(String, default="")
    email   = Column(String, default="")

    # Primary material contact
    material_contact_name  = Column(String, default="")
    material_contact_email = Column(String, default="")
    material_contact_phone = Column(String, default="")

    # Procurement contact
    procurement_contact_name  = Column(String, default="")
    procurement_contact_email = Column(String, default="")
    procurement_contact_phone = Column(String, default="")

    # Manager / escalation contact
    manager_contact_name  = Column(String, default="")
    manager_contact_email = Column(String, default="")
    manager_contact_phone = Column(String, default="")

    # Procurement / DDMRP relevant
    lead_time_days  = Column(Integer, default=0)    # typical supplier lead time
    reliability_pct = Column(Float,   default=100.0) # on-time delivery rate 0-100
    payment_terms   = Column(String,  default="")    # e.g. "Net 30"
    currency        = Column(String,  default="EUR")
    incoterms       = Column(String,  default="")    # EXW, FOB, DDP …

    # Classification
    status          = Column(String,  default="active")   # active | inactive | preferred
    certifications  = Column(Text,    default="")         # ISO 9001, ISO 14001 …
    notes           = Column(Text,    default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = relationship("Item", back_populates="supplier")


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    part_number = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=False)
    category = Column(String, default="")
    unit_of_measure = Column(String, default="EA")

    # DDMRP item type (slide 50): M=Manufactured, I=Intermediate, P=Purchased, D=Distributed
    item_type = Column(String, default="P")
    # Buffer Profile linkage (slides 50-54)
    buffer_profile_id = Column(Integer, ForeignKey("buffer_profiles.id"), nullable=True)

    # ASOH (Adjusted Spike Horizon) parameters (slide 83) - per-item overrides
    # nullable -> falls back to global Settings defaults
    spike_horizon_days = Column(Integer, nullable=True)
    spike_threshold_factor = Column(Float, nullable=True)

    # DDMRP parameters
    adu = Column(Float, default=0.0)
    dlt = Column(Float, default=0.0)
    lead_time_factor = Column(Float, default=0.5)
    variability_factor = Column(Float, default=0.5)
    min_order_qty = Column(Float, default=0.0)
    order_cycle = Column(Float, default=0.0)

    # Current stock
    on_hand = Column(Float, default=0.0)

    # Cost fields — used by Safety Stock & EOQ module
    unit_cost        = Column(Float, default=0.0)   # € per unit
    ordering_cost    = Column(Float, default=0.0)   # € per order (0 → use global default)
    holding_cost_pct = Column(Float, default=0.0)   # annual holding cost % as fraction, e.g. 0.25 (0 → use global default)

    # Default supplier
    default_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)

    # Relationships
    demand_entries = relationship("DemandEntry", back_populates="item", cascade="all, delete")
    supply_entries = relationship("SupplyEntry", back_populates="item", cascade="all, delete")
    buffer = relationship("Buffer", back_populates="item", uselist=False, cascade="all, delete")
    process_nodes    = relationship("ProcessNode", back_populates="item",
                                    foreign_keys="ProcessNode.item_id")
    node_memberships = relationship("ProcessNodeItem", back_populates="item")
    buffer_profile   = relationship("BufferProfile", back_populates="items")
    supplier         = relationship("Supplier", back_populates="items", foreign_keys=[default_supplier_id])

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Buffer Profile (slides 50-54)
# ---------------------------------------------------------------------------

class BufferProfile(Base):
    """
    DDMRP Buffer Profile = Item Type x Lead Time Category x Variability Category.
    Each profile carries default LTF and VF inside the canonical bands:
      LTF: L (long LT)   = 0.2-0.4
           M (medium LT) = 0.41-0.6
           S (short LT)  = 0.61-1.0
      VF:  L (low var)   = 0.0-0.4
           M (medium)    = 0.41-0.6
           H (high)      = 0.61-1.0
    """
    __tablename__ = "buffer_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, unique=True, nullable=False)
    item_type = Column(String, nullable=False)        # M | I | P | D
    lt_category = Column(String, nullable=False)      # S | M | L
    var_category = Column(String, nullable=False)     # L | M | H
    default_ltf = Column(Float, nullable=False)
    default_vf = Column(Float, nullable=False)

    items = relationship("Item", back_populates="buffer_profile")


# ---------------------------------------------------------------------------
# Buffer Adjustments — Planned Adjustments (DAF / ZAF / LTAF, slides 73-80)
# ---------------------------------------------------------------------------

class BufferAdjustment(Base):
    """
    A time-bounded planned adjustment for an item's buffer math.

    Three families of factors (deck slides 73-80):
      - DAF  (Demand Adjustment Factor)   — multiplier on ADU
      - LTAF (Lead Time Adjustment Factor)— multiplier on DLT
      - ZAF  (Zone Adjustment Factors)    — per-zone multipliers (Red, Yellow, Green)

    A factor of 1.0 = neutral / no change. End_date NULL = open-ended.
    Multiple overlapping adjustments on the same item are multiplied together.
    """
    __tablename__ = "buffer_adjustments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)

    daf = Column(Float, default=1.0)         # Demand Adjustment Factor (× ADU)
    ltaf = Column(Float, default=1.0)        # Lead Time Adjustment Factor (× DLT)
    red_zaf = Column(Float, default=1.0)     # Red Zone Adjustment Factor
    yellow_zaf = Column(Float, default=1.0)  # Yellow Zone Adjustment Factor
    green_zaf = Column(Float, default=1.0)   # Green Zone Adjustment Factor

    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item")


# ---------------------------------------------------------------------------
# Settings (singleton with global defaults)
# ---------------------------------------------------------------------------

class Settings(Base):
    """Singleton row holding global app defaults (id=1)."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Global ASOH defaults (slide 83) - used when item-level overrides are NULL
    default_spike_horizon_days = Column(Integer, default=0)         # 0 -> fall back to DLT
    default_spike_threshold_factor = Column(Float, default=2.0)     # multiple of ADU


# ---------------------------------------------------------------------------
# Demand & Supply
# ---------------------------------------------------------------------------

class DemandEntry(Base):
    __tablename__ = "demand_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    demand_type = Column(String, default="actual")   # actual | forecast
    quantity = Column(Float, nullable=False)
    demand_date = Column(DateTime, nullable=False)
    order_reference = Column(String, default="")
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="demand_entries")


class SupplyEntry(Base):
    __tablename__ = "supply_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    supply_type = Column(String, default="purchase_order")
    quantity = Column(Float, nullable=False)
    due_date = Column(DateTime, nullable=False)
    order_reference = Column(String, default="")
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="supply_entries")


# ---------------------------------------------------------------------------
# Buffer (calculated DDMRP zones per item)
# ---------------------------------------------------------------------------

class Buffer(Base):
    __tablename__ = "buffers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("items.id"), unique=True, nullable=False)

    red_zone = Column(Float, default=0.0)
    yellow_zone = Column(Float, default=0.0)
    green_zone = Column(Float, default=0.0)

    top_of_red = Column(Float, default=0.0)
    top_of_yellow = Column(Float, default=0.0)
    top_of_green = Column(Float, default=0.0)

    net_flow_position = Column(Float, default=0.0)
    status = Column(String, default="green")
    suggested_order_qty = Column(Float, default=0.0)

    dynamic_adu = Column(Float, default=0.0)
    static_adu = Column(Float, default=0.0)
    adu_window_days = Column(Integer, default=7)

    # Execution view (deck slides 109-118)
    buffer_status_pct = Column(Float, default=0.0)         # on_hand / TOR
    execution_color = Column(String, default="green")      # over_tog | green | yellow | red | dark_red

    last_calculated = Column(DateTime, default=datetime.utcnow)
    next_recalc_due = Column(DateTime, nullable=True)

    item = relationship("Item", back_populates="buffer")


# ---------------------------------------------------------------------------
# BOM (Bill of Materials) — used by compute_dlt() in bom_engine.py
# ---------------------------------------------------------------------------

class BomLine(Base):
    """
    One BOM row: parent item (assembly) needs `qty` units of child item per assembly.
    Used by compute_dlt() to walk upstream paths and find the longest
    unprotected (non-buffered) lead-time chain (deck slide 26).
    """
    __tablename__ = "bom_lines"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    parent_item_id  = Column(Integer, ForeignKey("items.id"), nullable=False)
    child_item_id   = Column(Integer, ForeignKey("items.id"), nullable=False)
    qty             = Column(Float, default=1.0)
    note            = Column(Text, default="")
    created_at      = Column(DateTime, default=datetime.utcnow)

    parent = relationship("Item", foreign_keys=[parent_item_id])
    child  = relationship("Item", foreign_keys=[child_item_id])


# ---------------------------------------------------------------------------
# Manufacturing Process Designer
# ---------------------------------------------------------------------------

class ProcessNode(Base):
    __tablename__ = "process_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    process_id = Column(Integer, ForeignKey("processes.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)   # legacy — kept for migration
    label = Column(String, nullable=False)
    node_type = Column(String, default="operation")
    has_buffer = Column(Boolean, default=False)
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)
    sequence = Column(Integer, default=0)

    process = relationship("Process", back_populates="nodes")
    item = relationship("Item", back_populates="process_nodes", foreign_keys=[item_id])
    node_items = relationship("ProcessNodeItem", back_populates="node",
                              cascade="all, delete-orphan")
    outgoing_edges = relationship("ProcessEdge", foreign_keys="ProcessEdge.source_id",
                                  back_populates="source", cascade="all, delete")
    incoming_edges = relationship("ProcessEdge", foreign_keys="ProcessEdge.target_id",
                                  back_populates="target", cascade="all, delete")


class ProcessNodeItem(Base):
    """Many-to-many: one ProcessNode can reference multiple Items."""
    __tablename__ = "process_node_items"

    id      = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Integer, ForeignKey("process_nodes.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)

    node = relationship("ProcessNode", back_populates="node_items")
    item = relationship("Item", back_populates="node_memberships")


class ProcessEdge(Base):
    __tablename__ = "process_edges"

    id = Column(Integer, primary_key=True, autoincrement=True)
    process_id = Column(Integer, ForeignKey("processes.id"), nullable=False)
    source_id = Column(Integer, ForeignKey("process_nodes.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("process_nodes.id"), nullable=False)

    process = relationship("Process", back_populates="edges")
    source = relationship("ProcessNode", foreign_keys=[source_id], back_populates="outgoing_edges")
    target = relationship("ProcessNode", foreign_keys=[target_id], back_populates="incoming_edges")


class Process(Base):
    __tablename__ = "processes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    nodes = relationship("ProcessNode", back_populates="process", cascade="all, delete")
    edges = relationship("ProcessEdge", back_populates="process", cascade="all, delete")


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_db():
    """Create all tables if they don't exist. Works on both PostgreSQL and SQLite."""
    Base.metadata.create_all(engine)
    # Add any new columns to existing tables (safe, idempotent, cross-dialect)
    _migrate_buffer_columns()
    _migrate_item_columns()
    _migrate_bom_columns()
    _migrate_process_node_items()
    _migrate_supplier_columns()
    # Seed reference data
    _seed_buffer_profiles()
    _seed_settings()


def _migrate_buffer_columns():
    """Add new Buffer columns to existing databases (idempotent)."""
    _add_columns_safely("buffers", [
        ("dynamic_adu",       "REAL DEFAULT 0.0",        "DOUBLE PRECISION DEFAULT 0.0"),
        ("static_adu",        "REAL DEFAULT 0.0",        "DOUBLE PRECISION DEFAULT 0.0"),
        ("adu_window_days",   "INTEGER DEFAULT 7",       "INTEGER DEFAULT 7"),
        ("next_recalc_due",   "DATETIME",                "TIMESTAMP"),
        ("buffer_status_pct", "REAL DEFAULT 0.0",        "DOUBLE PRECISION DEFAULT 0.0"),
        ("execution_color",  "TEXT DEFAULT 'green'",     "VARCHAR DEFAULT 'green'"),
    ])


def _migrate_bom_columns():
    """bom_lines is created by create_all; no extra columns to migrate yet."""
    pass  # placeholder — keeps the pattern consistent if columns are added later


def _migrate_supplier_columns():
    """Add default_supplier_id to existing items tables (idempotent)."""
    _add_columns_safely("items", [
        ("default_supplier_id", "INTEGER", "INTEGER"),
    ])


def _migrate_process_node_items():
    """
    process_node_items is created by create_all.
    This function migrates any existing ProcessNode.item_id (legacy single FK)
    into the new junction table (idempotent).
    """
    session = SessionLocal()
    try:
        nodes = session.query(ProcessNode).filter(ProcessNode.item_id.isnot(None)).all()
        for node in nodes:
            exists = session.query(ProcessNodeItem).filter_by(
                node_id=node.id, item_id=node.item_id).first()
            if not exists:
                session.add(ProcessNodeItem(node_id=node.id, item_id=node.item_id))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _migrate_item_columns():
    """Add cost / DDMRP-profile / ASOH fields to existing `items` tables (idempotent)."""
    _add_columns_safely("items", [
        ("unit_cost",              "REAL DEFAULT 0.0",   "DOUBLE PRECISION DEFAULT 0.0"),
        ("ordering_cost",          "REAL DEFAULT 0.0",   "DOUBLE PRECISION DEFAULT 0.0"),
        ("holding_cost_pct",       "REAL DEFAULT 0.0",   "DOUBLE PRECISION DEFAULT 0.0"),
        ("item_type",              "TEXT DEFAULT 'P'",   "VARCHAR DEFAULT 'P'"),
        ("buffer_profile_id",      "INTEGER",            "INTEGER"),
        ("spike_horizon_days",     "INTEGER",            "INTEGER"),
        ("spike_threshold_factor", "REAL",               "DOUBLE PRECISION"),
    ])


# ---------------------------------------------------------------------------
# Reference data seeding
# ---------------------------------------------------------------------------

# Canonical 9-cell band midpoints per the deck (slides 51-54).
# LTF bands: L 0.2-0.4 (mid 0.30) | M 0.41-0.6 (mid 0.50) | S 0.61-1.0 (mid 0.80)
# VF  bands: L 0.0-0.4 (mid 0.20) | M 0.41-0.6 (mid 0.50) | H 0.61-1.0 (mid 0.80)
_LTF_MID = {"L": 0.30, "M": 0.50, "S": 0.80}
_VF_MID  = {"L": 0.20, "M": 0.50, "H": 0.80}
_ITEM_TYPES = ("M", "I", "P", "D")


def _seed_buffer_profiles():
    """Pre-populate the 36-cell profile matrix (4 item types x 3 LT cats x 3 VF cats)."""
    session = SessionLocal()
    try:
        existing = {p.name for p in session.query(BufferProfile).all()}
        for it in _ITEM_TYPES:
            for ltc in ("S", "M", "L"):
                for vc in ("L", "M", "H"):
                    name = f"{it}-{ltc}-{vc}"
                    if name in existing:
                        continue
                    session.add(BufferProfile(
                        name=name,
                        item_type=it,
                        lt_category=ltc,
                        var_category=vc,
                        default_ltf=_LTF_MID[ltc],
                        default_vf=_VF_MID[vc],
                    ))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _seed_settings():
    """Ensure the Settings singleton row exists."""
    session = SessionLocal()
    try:
        s = session.query(Settings).first()
        if s is None:
            session.add(Settings(
                default_spike_horizon_days=0,        # 0 -> use DLT as horizon
                default_spike_threshold_factor=2.0,  # 2x ADU per the deck
            ))
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _add_columns_safely(table: str, cols):
    """
    cols: list of tuples (name, sqlite_def, pg_def).
    Uses the right ALTER TABLE syntax for each dialect and silently skips if
    the column already exists.
    """
    import sqlalchemy
    is_pg = DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres://")
    with engine.connect() as conn:
        for name, sqlite_def, pg_def in cols:
            if is_pg:
                stmt = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{name}" {pg_def}'
            else:
                stmt = f"ALTER TABLE {table} ADD COLUMN {name} {sqlite_def}"
            try:
                conn.execute(sqlalchemy.text(stmt))
                conn.commit()
            except Exception:
                pass  # column already exists — ignore


def get_session():
    return SessionLocal()
