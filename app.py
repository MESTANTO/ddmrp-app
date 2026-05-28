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

# Top-level pages (rendered as standalone buttons, no group)
TOP_LEVEL: list[tuple[str, str]] = [
    ("📊  Dashboard",             "dashboard"),
]

# Grouped pages — each group is an expander with buttons inside
GROUPS: dict[str, list[tuple[str, str]]] = {
    "📁  Master Data": [
        ("📋  Material Master",       "material_master"),
        ("🏭  Supplier Master",       "supplier_master"),
        ("🌐  Network Master",        "network_master"),
        ("📈  Demand & Supply",       "demand_supply"),
        ("🔗  Process Designer",      "process_designer"),
        ("🧬  BOM & Auto DLT",        "bom_engine"),
    ],
    "🎯  DDMRP": [
        ("🎯  MRP Type",              "mrp_type"),
        ("🚦  Replenishment Signals", "signal_engine"),
        ("🚨  Execution Alarms",      "alarms"),
        ("📐  Prioritized Share",     "share_allocator"),
        ("🎛️  Buffer Adjustments",    "buffer_adjustments"),
        ("🛡️  Safety Stock & EOQ",    "safety_stock"),
    ],
    "📈  Analysis": [
        ("🔠  ABC / XYZ / ACV²",      "abc_xyz"),
        ("📉  Model Velocity",        "model_velocity"),
        ("💰  Total Cost of Ownership","tco_analysis"),
        ("⚠️  Risk Register",          "risk_register"),
        ("📤  Export to Excel",       "export"),
    ],
    "🧮  Optimization": [
        ("🧮  Optimization Sessions", "optimization_home"),
    ],
}

# Bottom-level standalone pages
BOTTOM_LEVEL: list[tuple[str, str]] = [
    ("🤖  AI Inventory Manager", "ai_inventory_manager"),
    ("⚙️  Settings",              "settings"),
]

# Flat lookup: slug → label (used to label the active page later if needed)
ALL_PAGES: dict[str, str] = {}
for _lbl, _slug in TOP_LEVEL + BOTTOM_LEVEL:
    ALL_PAGES[_slug] = _lbl
for _items in GROUPS.values():
    for _lbl, _slug in _items:
        ALL_PAGES[_slug] = _lbl


# Active-page state lives in session_state so clicks survive reruns
if "active_page" not in st.session_state:
    st.session_state.active_page = "dashboard"


def _nav_button(label: str, slug: str) -> None:
    """Render a sidebar nav button; highlights the active page."""
    is_active = (st.session_state.active_page == slug)
    if st.sidebar.button(
        label,
        key=f"nav_{slug}",
        use_container_width=True,
        type="primary" if is_active else "secondary",
    ):
        st.session_state.active_page = slug
        st.rerun()


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

# Top-level entries
for _lbl, _slug in TOP_LEVEL:
    _nav_button(_lbl, _slug)

# Grouped entries — expander auto-opens if the active page is inside
for _group_label, _items in GROUPS.items():
    _slugs_in_group = {s for _, s in _items}
    _expanded = st.session_state.active_page in _slugs_in_group
    with st.sidebar.expander(_group_label, expanded=_expanded):
        for _lbl, _slug in _items:
            _is_active = (st.session_state.active_page == _slug)
            if st.button(
                _lbl,
                key=f"nav_{_slug}",
                use_container_width=True,
                type="primary" if _is_active else "secondary",
            ):
                st.session_state.active_page = _slug
                st.rerun()

# Bottom-level entries
for _lbl, _slug in BOTTOM_LEVEL:
    _nav_button(_lbl, _slug)

with st.sidebar:
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

page_key = st.session_state.active_page

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

elif page_key == "mrp_type":
    from views.mrp_type import show
    show()

elif page_key == "share_allocator":
    from modules.share_allocator import show
    show()

elif page_key == "model_velocity":
    from views.model_velocity import show
    show()

elif page_key == "ai_inventory_manager":
    from views.ai_inventory_manager import show
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

elif page_key == "tco_analysis":
    from views.tco_analysis import show
    show()

elif page_key == "risk_register":
    from views.risk_register import show
    show()

elif page_key == "export":
    from modules.export import show
    show()

elif page_key == "network_master":
    from views.network_master import show
    show()

elif page_key == "optimization_home":
    from views.optimization.home import show
    show()

elif page_key == "settings":
    from views.settings import show
    show()
