"""
Authentication and session helpers for the DDMRP multi-tenant application.

Passwords use PBKDF2-HMAC-SHA256 with a per-user random salt (built-in
hashlib — no external dependencies required).
"""

import hashlib
import os
import streamlit as st
from database.db import get_session, Company, User, seed_company_data


# ─────────────────────────────────────────────────────────────────────────────
# Password hashing
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(32)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return salt.hex() + ":" + key.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return key.hex() == key_hex
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# User CRUD
# ─────────────────────────────────────────────────────────────────────────────

def register_user(username: str, password: str, email: str = "") -> tuple:
    """
    Create a new user (no company yet).
    Returns (True, user_dict) on success or (False, error_message) on failure.
    """
    session = get_session()
    try:
        if session.query(User).filter_by(username=username.lower().strip()).first():
            return False, "Username already taken."
        user = User(
            username=username.lower().strip(),
            email=email.strip(),
            password_hash=hash_password(password),
        )
        session.add(user)
        session.commit()
        return True, _user_to_dict(user)
    except Exception as e:
        session.rollback()
        return False, str(e)
    finally:
        session.close()


def authenticate(username: str, password: str):
    """
    Verify credentials.  Returns user dict on success, None on failure.
    Also updates last_login timestamp.
    """
    from datetime import datetime
    session = get_session()
    try:
        user = session.query(User).filter_by(
            username=username.lower().strip()
        ).first()
        if user and user.is_active and verify_password(password, user.password_hash):
            user.last_login = datetime.utcnow()
            session.commit()
            return _user_to_dict(user)
        return None
    finally:
        session.close()


def _user_to_dict(user: User) -> dict:
    return {
        "id":         user.id,
        "username":   user.username,
        "email":      user.email,
        "company_id": user.company_id,
        "role":       user.role,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Company CRUD
# ─────────────────────────────────────────────────────────────────────────────

def create_company(
    user_id: int,
    name: str,
    industry: str = "",
    country: str = "",
    city: str = "",
    currency: str = "EUR",
    website: str = "",
    notes: str = "",
) -> tuple:
    """
    Create a Company record, link it to user_id, seed reference data.
    Returns (True, company_id) or (False, error_message).
    """
    session = get_session()
    try:
        company = Company(
            name=name.strip(),
            industry=industry.strip(),
            country=country.strip(),
            city=city.strip(),
            currency=currency.strip() or "EUR",
            website=website.strip(),
            notes=notes.strip(),
        )
        session.add(company)
        session.flush()   # get company.id

        user = session.query(User).get(user_id)
        if user:
            user.company_id = company.id
        session.commit()
        cid = company.id
    except Exception as e:
        session.rollback()
        return False, str(e)
    finally:
        session.close()

    # Seed buffer profiles + settings for the new company (outside the session)
    seed_company_data(cid)
    return True, cid


def get_company_info(company_id: int) -> dict:
    """Return company fields as a plain dict (safe after session close)."""
    session = get_session()
    try:
        co = session.query(Company).get(company_id)
        if not co:
            return {}
        return {
            "id": co.id, "name": co.name, "industry": co.industry,
            "country": co.country, "city": co.city, "currency": co.currency,
            "website": co.website, "notes": co.notes,
        }
    finally:
        session.close()


def update_company(company_id: int, **fields) -> bool:
    session = get_session()
    try:
        co = session.query(Company).get(company_id)
        if not co:
            return False
        for k, v in fields.items():
            if hasattr(co, k):
                setattr(co, k, v)
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit session state helpers
# ─────────────────────────────────────────────────────────────────────────────

_KEY = "_ddmrp_user"


def login(user_dict: dict):
    """Store user in Streamlit session state."""
    st.session_state[_KEY] = user_dict


def logout():
    st.session_state.pop(_KEY, None)


def get_current_user() -> dict | None:
    return st.session_state.get(_KEY)


def get_company_id() -> int | None:
    u = get_current_user()
    return u.get("company_id") if u else None


def is_authenticated() -> bool:
    return get_current_user() is not None


def has_company() -> bool:
    return bool(get_company_id())


def refresh_session_company(user_id: int):
    """Re-read company_id from DB and update session (call after company setup)."""
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if user and _KEY in st.session_state:
            st.session_state[_KEY]["company_id"] = user.company_id
    finally:
        session.close()
