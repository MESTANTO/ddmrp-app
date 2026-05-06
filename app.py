"""
DDMRP Application — Main Entry Point
Run with: streamlit run app.py
"""

import streamlit as st
from database.db import init_db
from database.auth import is_authenticated, has_company, get_current_user, logout
from styles import inject_css

# Page config
st.set_page_config(
    page_title="DDMRP · Demand Driven MRP",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inject enterprise stylesheet (runs on every rerun — it's just HTML/CSS, fast)
inject_css()

# Run DB migrations and seeding only once per server process (not on every rerun)
@st.cache_resource
def _init_db_once():
    init_db()
    return True


_init_db_once()

# ---------------------------------------------------------------------------
# Auth gate — must be authenticated before anything else is shown
# ---------------------------------------------------------------------------

if not is_authenticated():
    from views.login import show as _show_login
    _show_login()
    st.stop()

if not has_company():
    from views.company_setup import show_setup as _show_company_setup
    _show_company_setup()
    st.stop()

# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

PAGES = {
    "📊  Dashboard":                 "dashboard",
    "📋  Material Master":           "material_master",
    "🏭  Supplier Master":           "supplier_master",
    "📈  Demand & Supply":           "demand_supply",
    "🔗  Process Designer":          "process_designer",
    "🧬  BOM & Auto DLT":            "bom_engine",
    "🚦  Replenishment Signals":     "signal_engine",
    "🚨  Execution Alarms":          "alarms",
    "📐  Prioritized Share":         "share_allocator",
    "📉  Model Velocity":            "model_velocity",
    "🤖  AI Advisor":                "ai_advisor",
    "🔠  ABC / XYZ / ACV²":          "abc_xyz",
    "🛡️  Safety Stock & EOQ":        "safety_stock",
    "🎛️  Buffer Adjustments":        "buffer_adjustments",
    "📤  Export to Excel":           "export",
    "⚙️  Settings":                  "settings",
}

_user = get_current_user()
_company_id = _user.get("company_id")

with st.sidebar:
    st.markdown(
        "<div style='margin-bottom:0.15rem'>"
        "<span style='font-size:1.35rem;font-weight:800;letter-spacing:0.08em;"
        "text-transform:uppercase;color:#E8F0FF;font-family:IBM Plex Sans,sans-serif'>"
        "DDMRP</span>"
        "</div>"
        "<div style='font-size:0.62rem;font-weight:700;letter-spacing:0.15em;"
        "text-transform:uppercase;color:#3D5577;font-family:IBM Plex Sans,sans-serif;"
        "margin-bottom:0.25rem'>Demand Driven MRP</div>",
        unsafe_allow_html=True,
    )
    st.divider()
    st.caption("MODULES")

    selection = st.radio(
        "Navigate to",
        list(PAGES.keys()),
        label_visibility="collapsed",
    )
    st.divider()

    # User / company info + logout
    from database.auth import get_company_info
    _co = get_company_info(_company_id) if _company_id else {}
    st.markdown(
        f"<div style='font-size:0.7rem;color:#7A92BB;margin-bottom:0.15rem'>"
        f"<span style='color:#3D5577'>COMPANY</span><br>"
        f"<strong style='color:#E8F0FF'>{_co.get('name', '—')}</strong>"
        f"</div>"
        f"<div style='font-size:0.68rem;color:#3D5577;margin-bottom:0.75rem'>"
        f"{_user['username']}</div>",
        unsafe_allow_html=True,
    )
    if st.button("Sign Out", use_container_width=True, type="secondary"):
        logout()
        st.rerun()
    st.caption("v1.0  ·  Streamlit")

# ---------------------------------------------------------------------------
# Page routing
# ---------------------------------------------------------------------------

page_key = PAGES[selection]

if page_key == "dashboard":
    from views.dashboard import show
    show()

elif page_key == "material_master":
    from modules.material_master import show
    show()

elif page_key == "supplier_master":
    from modules.supplier_master import show
    show()

elif page_key == "demand_supply":
    from modules.demand_supply import show
    show()

elif page_key == "process_designer":
    from modules.process_designer import show
    show()

elif page_key == "signal_engine":
    from modules.signal_engine import show
    show()

elif page_key == "alarms":
    from views.alarms import show
    show()

elif page_key == "bom_engine":
    from modules.bom_engine import show
    show()

elif page_key == "share_allocator":
    from modules.share_allocator import show
    show()

elif page_key == "model_velocity":
    from views.model_velocity import show
    show()

elif page_key == "ai_advisor":
    from views.ai_advisor import show
    show()

elif page_key == "abc_xyz":
    from views.abc_xyz import show
    show()

elif page_key == "safety_stock":
    from modules.safety_stock import show
    show()

elif page_key == "buffer_adjustments":
    from modules.buffer_adjustments import show
    show()

elif page_key == "export":
    from modules.export import show
    show()

elif page_key == "settings":
    from views.settings import show
    show()
