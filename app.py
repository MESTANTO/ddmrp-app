"""
DDMRP Application — Main Entry Point
Run with: streamlit run app.py
"""

import streamlit as st
from database.db import init_db

# Page config
st.set_page_config(
    page_title="DDMRP Application",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialise database on first run
init_db()

# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------

PAGES = {
    "📊 Dashboard":                  "dashboard",
    "📋 Material Master":            "material_master",
    "📈 Demand & Supply":            "demand_supply",
    "🏭 Process Designer":           "process_designer",
    "🚦 Replenishment Signals":      "signal_engine",
    "📤 Export to Excel":            "export",
}

with st.sidebar:
    st.image(
        "https://img.icons8.com/fluency/96/delivery-time.png",
        width=60,
    )
    st.title("DDMRP App")
    st.caption("Demand Driven MRP")
    st.divider()

    selection = st.radio(
        "Navigate to",
        list(PAGES.keys()),
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("v1.0 — Built with Streamlit")

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

elif page_key == "demand_supply":
    from modules.demand_supply import show
    show()

elif page_key == "process_designer":
    from modules.process_designer import show
    show()

elif page_key == "signal_engine":
    from modules.signal_engine import show
    show()

elif page_key == "export":
    from modules.export import show
    show()
