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
    "🧬 BOM & Auto DLT":             "bom_engine",
    "🚦 Replenishment Signals":      "signal_engine",
    "🚨 Execution Alarms":           "alarms",
    "📐 Prioritized Share":          "share_allocator",
    "📉 Model Velocity":             "model_velocity",
    "🛡️ Safety Stock & EOQ":         "safety_stock",
    "🎛️ Buffer Adjustments":         "buffer_adjustments",
    "📤 Export to Excel":            "export",
    "⚙️ Settings":                   "settings",
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
