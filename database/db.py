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

class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    part_number = Column(String, unique=True, nullable=False)
    description = Column(String, nullable=False)
    category = Column(String, default="")
    unit_of_measure = Column(String, default="EA")

    # DDMRP parameters
    adu = Column(Float, default=0.0)
    dlt = Column(Float, default=0.0)
    lead_time_factor = Column(Float, default=0.5)
    variability_factor = Column(Float, default=0.5)
    min_order_qty = Column(Float, default=0.0)
    order_cycle = Column(Float, default=0.0)

    # Current stock
    on_hand = Column(Float, default=0.0)

    # Relationships
    demand_entries = relationship("DemandEntry", back_populates="item", cascade="all, delete")
    supply_entries = relationship("SupplyEntry", back_populates="item", cascade="all, delete")
    buffer = relationship("Buffer", back_populates="item", uselist=False, cascade="all, delete")
    process_nodes = relationship("ProcessNode", back_populates="item")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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

    last_calculated = Column(DateTime, default=datetime.utcnow)
    next_recalc_due = Column(DateTime, nullable=True)

    item = relationship("Item", back_populates="buffer")


# ---------------------------------------------------------------------------
# Manufacturing Process Designer
# ---------------------------------------------------------------------------

class ProcessNode(Base):
    __tablename__ = "process_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    process_id = Column(Integer, ForeignKey("processes.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    label = Column(String, nullable=False)
    node_type = Column(String, default="operation")
    has_buffer = Column(Boolean, default=False)
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)
    sequence = Column(Integer, default=0)

    process = relationship("Process", back_populates="nodes")
    item = relationship("Item", back_populates="process_nodes")
    outgoing_edges = relationship("ProcessEdge", foreign_keys="ProcessEdge.source_id",
                                  back_populates="source", cascade="all, delete")
    incoming_edges = relationship("ProcessEdge", foreign_keys="ProcessEdge.target_id",
                                  back_populates="target", cascade="all, delete")


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
    # For SQLite only: add new columns to existing databases (safe migration)
    if DATABASE_URL.startswith("sqlite"):
        _migrate_sqlite_buffer_columns()


def _migrate_sqlite_buffer_columns():
    """Add new Buffer columns to existing SQLite databases (idempotent)."""
    import sqlalchemy
    new_columns = [
        ("dynamic_adu",     "REAL DEFAULT 0.0"),
        ("static_adu",      "REAL DEFAULT 0.0"),
        ("adu_window_days", "INTEGER DEFAULT 7"),
        ("next_recalc_due", "DATETIME"),
    ]
    with engine.connect() as conn:
        for col_name, col_def in new_columns:
            try:
                conn.execute(sqlalchemy.text(
                    f"ALTER TABLE buffers ADD COLUMN {col_name} {col_def}"
                ))
                conn.commit()
            except Exception:
                pass


def get_session():
    return SessionLocal()
