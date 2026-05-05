"""
Enterprise UI stylesheet for the DDMRP application.
Inject once per page via inject_css() at the top of app.py.

Design: Operational Intelligence
  - Deep navy shell with electric-blue accent system
  - IBM Plex Sans (UI) + IBM Plex Mono (numbers / data)
  - Card surfaces, precision borders, subtle depth
"""

import streamlit as st

_CSS = """
<!-- Google Fonts: IBM Plex Sans + IBM Plex Mono -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">

<style>

/* ═══════════════════════════════════════════════════════════════
   VARIABLES
═══════════════════════════════════════════════════════════════ */
:root {
    --bg-base:      #07101F;
    --bg-surface:   #0C1A30;
    --bg-elevated:  #112240;
    --bg-border:    #1E3356;
    --bg-hover:     #163050;

    --accent:       #1565FF;
    --accent-dim:   rgba(21, 101, 255, 0.15);
    --accent-ring:  rgba(21, 101, 255, 0.30);
    --cyan:         #00C4E8;
    --success:      #00C896;
    --warning:      #FFB020;
    --danger:       #F03040;

    --text-1:    #E8F0FF;
    --text-2:    #7A92BB;
    --text-3:    #3D5577;
    --text-data: #A0D4FF;

    --r:   6px;
    --r-lg: 10px;
    --ease: 0.15s ease;

    --font-ui:   'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    --font-data: 'IBM Plex Mono', 'Courier New', monospace;
}

/* ═══════════════════════════════════════════════════════════════
   RESET & GLOBAL
═══════════════════════════════════════════════════════════════ */
*, *::before, *::after {
    font-family: var(--font-ui) !important;
    box-sizing: border-box;
}

/* Monospace for numbers in widgets */
input[type="number"],
[data-testid="stMetricValue"],
[data-testid="stMetricDelta"] {
    font-family: var(--font-data) !important;
}

/* ═══════════════════════════════════════════════════════════════
   APP SHELL
═══════════════════════════════════════════════════════════════ */
.stApp {
    background-color: var(--bg-base) !important;
    background-image:
        radial-gradient(
            ellipse 120% 55% at 50% -5%,
            rgba(21, 101, 255, 0.07) 0%,
            transparent 65%
        );
}

[data-testid="stAppViewContainer"] > .main {
    background: transparent !important;
}

.main .block-container {
    padding: 2.5rem 3rem 5rem !important;
    max-width: 1700px !important;
}

/* Top toolbar / deploy button bar */
[data-testid="stHeader"] {
    background: var(--bg-surface) !important;
    border-bottom: 1px solid var(--bg-border) !important;
}

[data-testid="stDecoration"] {
    background: linear-gradient(90deg, var(--accent), var(--cyan)) !important;
    height: 2px !important;
}

/* ═══════════════════════════════════════════════════════════════
   SIDEBAR
═══════════════════════════════════════════════════════════════ */
[data-testid="stSidebar"] {
    background: var(--bg-surface) !important;
    border-right: 1px solid var(--bg-border) !important;
}

[data-testid="stSidebarContent"] {
    padding: 1.75rem 1rem 1.5rem !important;
}

/* App title in sidebar */
[data-testid="stSidebar"] h1 {
    font-size: 0.95rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.12em !important;
    text-transform: uppercase !important;
    color: var(--text-1) !important;
    margin-bottom: 0.1rem !important;
}

[data-testid="stSidebar"] hr {
    margin: 1rem 0 !important;
}

/* Nav section label */
[data-testid="stSidebar"] .stCaption {
    font-size: 0.65rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    color: var(--text-3) !important;
}

/* Radio as nav — remove the dot */
[data-testid="stSidebar"] input[type="radio"] {
    display: none !important;
}
[data-testid="stSidebar"] [data-baseweb="radio"] > div:first-child {
    display: none !important;
}

/* Nav items */
[data-testid="stSidebar"] [data-testid="stRadio"] > div {
    gap: 2px !important;
    flex-direction: column !important;
}

[data-testid="stSidebar"] [data-testid="stRadio"] label {
    display: flex !important;
    align-items: center !important;
    padding: 0.5rem 0.75rem !important;
    border-radius: var(--r) !important;
    font-size: 0.8rem !important;
    font-weight: 500 !important;
    color: var(--text-2) !important;
    transition: all var(--ease) !important;
    border: 1px solid transparent !important;
    cursor: pointer !important;
    line-height: 1.4 !important;
}

[data-testid="stSidebar"] [data-testid="stRadio"] label:hover {
    background: var(--bg-hover) !important;
    color: var(--text-1) !important;
}

[data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {
    background: var(--accent-dim) !important;
    color: #5B9AFF !important;
    border-color: var(--accent-ring) !important;
}

/* ═══════════════════════════════════════════════════════════════
   TYPOGRAPHY
═══════════════════════════════════════════════════════════════ */
h1 {
    font-size: 1.5rem !important;
    font-weight: 700 !important;
    letter-spacing: -0.03em !important;
    color: var(--text-1) !important;
    line-height: 1.2 !important;
    margin-bottom: 0.2rem !important;
}

/* st.header renders inside a data-testid="stHeadingWithActionElements" */
[data-testid="stHeadingWithActionElements"] h1 {
    font-size: 1.45rem !important;
    border-bottom: 1px solid var(--bg-border) !important;
    padding-bottom: 0.6rem !important;
    margin-bottom: 0.5rem !important;
}

h2, h3 {
    font-size: 0.78rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-2) !important;
    margin-top: 1rem !important;
}

p, li {
    color: var(--text-1) !important;
    font-size: 0.875rem !important;
    line-height: 1.6 !important;
}

[data-testid="stCaptionContainer"],
.stCaption {
    color: var(--text-2) !important;
    font-size: 0.75rem !important;
    letter-spacing: 0.01em !important;
}

/* Code / inline mono */
code {
    font-family: var(--font-data) !important;
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    border-radius: 3px !important;
    padding: 0.1em 0.4em !important;
    font-size: 0.83em !important;
    color: var(--text-data) !important;
}

/* ═══════════════════════════════════════════════════════════════
   DIVIDERS
═══════════════════════════════════════════════════════════════ */
hr {
    border: none !important;
    border-top: 1px solid var(--bg-border) !important;
    margin: 1.5rem 0 !important;
    opacity: 1 !important;
}

/* ═══════════════════════════════════════════════════════════════
   METRICS / KPI CARDS
═══════════════════════════════════════════════════════════════ */
[data-testid="metric-container"] {
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    border-top: 2px solid var(--accent) !important;
    border-radius: var(--r) !important;
    padding: 1.1rem 1.25rem 1rem !important;
    transition: border-color var(--ease), box-shadow var(--ease) !important;
}

[data-testid="metric-container"]:hover {
    border-top-color: var(--cyan) !important;
    box-shadow: 0 0 0 1px var(--bg-border), 0 8px 24px rgba(0,0,0,0.35) !important;
}

[data-testid="stMetricLabel"] > div {
    font-size: 0.67rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-2) !important;
}

[data-testid="stMetricValue"] > div {
    font-family: var(--font-data) !important;
    font-size: 1.55rem !important;
    font-weight: 600 !important;
    letter-spacing: -0.03em !important;
    color: var(--text-1) !important;
    line-height: 1.2 !important;
}

[data-testid="stMetricDelta"] > div {
    font-family: var(--font-data) !important;
    font-size: 0.72rem !important;
}

/* ═══════════════════════════════════════════════════════════════
   BUTTONS
═══════════════════════════════════════════════════════════════ */
.stButton > button {
    background: var(--accent) !important;
    color: #fff !important;
    border: none !important;
    border-radius: var(--r) !important;
    font-weight: 600 !important;
    font-size: 0.75rem !important;
    letter-spacing: 0.07em !important;
    text-transform: uppercase !important;
    padding: 0.55rem 1.4rem !important;
    transition: background var(--ease), transform var(--ease), box-shadow var(--ease) !important;
    position: relative !important;
}

.stButton > button:hover:not(:disabled) {
    background: #0047D4 !important;
    transform: translateY(-1px) !important;
    box-shadow: 0 4px 18px rgba(21, 101, 255, 0.4) !important;
}

.stButton > button:active:not(:disabled) {
    transform: translateY(0) !important;
    box-shadow: none !important;
}

.stButton > button:disabled {
    background: var(--bg-border) !important;
    color: var(--text-3) !important;
    cursor: not-allowed !important;
}

/* Secondary / ghost */
.stButton > button[kind="secondary"],
.stButton > button[data-testid*="secondary"] {
    background: transparent !important;
    border: 1px solid var(--bg-border) !important;
    color: var(--text-2) !important;
}

.stButton > button[kind="secondary"]:hover:not(:disabled) {
    background: var(--accent-dim) !important;
    border-color: var(--accent-ring) !important;
    color: var(--text-1) !important;
    box-shadow: none !important;
}

/* Form submit button */
[data-testid="stFormSubmitButton"] > button {
    background: var(--accent) !important;
    color: #fff !important;
    border: none !important;
    border-radius: var(--r) !important;
    font-weight: 600 !important;
    font-size: 0.75rem !important;
    letter-spacing: 0.07em !important;
    text-transform: uppercase !important;
    padding: 0.55rem 1.4rem !important;
    transition: all var(--ease) !important;
}

[data-testid="stFormSubmitButton"] > button:hover {
    background: #0047D4 !important;
    box-shadow: 0 4px 18px rgba(21, 101, 255, 0.4) !important;
}

/* ═══════════════════════════════════════════════════════════════
   TABS
═══════════════════════════════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {
    background: transparent !important;
    border-bottom: 1px solid var(--bg-border) !important;
    gap: 0 !important;
    padding: 0 !important;
}

.stTabs [data-baseweb="tab"] {
    background: transparent !important;
    border: none !important;
    border-bottom: 2px solid transparent !important;
    border-radius: 0 !important;
    color: var(--text-3) !important;
    font-size: 0.7rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    padding: 0.8rem 1.4rem !important;
    transition: color var(--ease), background var(--ease) !important;
    margin-bottom: -1px !important;
}

.stTabs [data-baseweb="tab"]:hover {
    color: var(--text-1) !important;
    background: var(--bg-hover) !important;
}

.stTabs [aria-selected="true"] {
    color: var(--text-1) !important;
    border-bottom-color: var(--accent) !important;
    background: transparent !important;
}

.stTabs [data-baseweb="tab-highlight"] {
    background: var(--accent) !important;
    height: 2px !important;
}

.stTabs [data-baseweb="tab-panel"] {
    padding-top: 1.5rem !important;
    background: transparent !important;
}

/* ═══════════════════════════════════════════════════════════════
   FORM INPUTS
═══════════════════════════════════════════════════════════════ */
/* All input-like elements */
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stTextArea"] textarea {
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    border-radius: var(--r) !important;
    color: var(--text-1) !important;
    font-size: 0.875rem !important;
    transition: border-color var(--ease), box-shadow var(--ease) !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stNumberInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-dim) !important;
    outline: none !important;
}

/* Placeholder text */
input::placeholder, textarea::placeholder {
    color: var(--text-3) !important;
}

/* Selectbox */
[data-testid="stSelectbox"] > div > div,
[data-testid="stMultiSelect"] > div > div {
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    border-radius: var(--r) !important;
    transition: border-color var(--ease) !important;
}

[data-testid="stSelectbox"] > div > div:focus-within,
[data-testid="stMultiSelect"] > div > div:focus-within {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-dim) !important;
}

/* Widget labels */
[data-testid="stWidgetLabel"],
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label {
    font-size: 0.68rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.09em !important;
    text-transform: uppercase !important;
    color: var(--text-2) !important;
    margin-bottom: 0.3rem !important;
}

/* Slider accent */
[data-baseweb="slider"] [role="progressbar"] {
    background-color: var(--accent) !important;
}

[data-baseweb="slider"] [role="slider"] {
    background: var(--accent) !important;
    border-color: var(--accent) !important;
}

/* Form container card */
[data-testid="stForm"] {
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    border-radius: var(--r-lg) !important;
    padding: 1.75rem 1.5rem !important;
}

/* ═══════════════════════════════════════════════════════════════
   ALERTS / NOTIFICATION BOXES
═══════════════════════════════════════════════════════════════ */
[data-testid="stAlert"] {
    border-radius: var(--r) !important;
    font-size: 0.83rem !important;
    padding: 0.75rem 1rem !important;
}

/* Info */
[data-testid="stAlert"][data-baseweb="notification"] {
    background: rgba(21, 101, 255, 0.07) !important;
    border: 1px solid rgba(21, 101, 255, 0.22) !important;
    border-left: 3px solid var(--accent) !important;
}

/* Warning */
div[data-testid="stAlert"] > div:has([data-testid="stAlertContentWarning"]) {
    background: rgba(255, 176, 32, 0.07) !important;
    border-left: 3px solid var(--warning) !important;
}

/* Error */
div[data-testid="stAlert"] > div:has([data-testid="stAlertContentError"]) {
    background: rgba(240, 48, 64, 0.07) !important;
    border-left: 3px solid var(--danger) !important;
}

/* Success */
div[data-testid="stAlert"] > div:has([data-testid="stAlertContentSuccess"]) {
    background: rgba(0, 200, 150, 0.07) !important;
    border-left: 3px solid var(--success) !important;
}

/* ═══════════════════════════════════════════════════════════════
   EXPANDER
═══════════════════════════════════════════════════════════════ */
[data-testid="stExpander"] {
    border: 1px solid var(--bg-border) !important;
    border-radius: var(--r-lg) !important;
    background: var(--bg-elevated) !important;
    overflow: hidden !important;
    margin-bottom: 0.5rem !important;
}

[data-testid="stExpander"] details summary {
    font-size: 0.72rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.09em !important;
    text-transform: uppercase !important;
    color: var(--text-2) !important;
    padding: 0.875rem 1.125rem !important;
    background: var(--bg-elevated) !important;
    cursor: pointer !important;
    transition: all var(--ease) !important;
    list-style: none !important;
}

[data-testid="stExpander"] details summary:hover {
    color: var(--text-1) !important;
    background: var(--bg-hover) !important;
}

[data-testid="stExpander"] details[open] summary {
    border-bottom: 1px solid var(--bg-border) !important;
    color: var(--text-1) !important;
}

[data-testid="stExpander"] details > div {
    padding: 1.25rem !important;
}

/* ═══════════════════════════════════════════════════════════════
   DATAFRAME / TABLE
═══════════════════════════════════════════════════════════════ */
[data-testid="stDataFrame"] {
    border: 1px solid var(--bg-border) !important;
    border-radius: var(--r-lg) !important;
    overflow: hidden !important;
}

/* Inner iframe inherits background — force via wrapper */
[data-testid="stDataFrame"] > div {
    border-radius: var(--r-lg) !important;
    overflow: hidden !important;
}

/* ═══════════════════════════════════════════════════════════════
   SPINNER
═══════════════════════════════════════════════════════════════ */
[data-testid="stSpinner"] div {
    border-top-color: var(--accent) !important;
    border-right-color: var(--accent-dim) !important;
    border-bottom-color: var(--accent-dim) !important;
    border-left-color: var(--accent-dim) !important;
}

/* ═══════════════════════════════════════════════════════════════
   CHECKBOX & RADIO (main content)
═══════════════════════════════════════════════════════════════ */
.main [data-testid="stCheckbox"] label {
    font-size: 0.85rem !important;
    color: var(--text-1) !important;
}

.main [data-testid="stRadio"] label {
    font-size: 0.85rem !important;
    color: var(--text-1) !important;
}

/* ═══════════════════════════════════════════════════════════════
   SCROLLBAR
═══════════════════════════════════════════════════════════════ */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--bg-surface); }
::-webkit-scrollbar-thumb {
    background: var(--bg-border);
    border-radius: 3px;
    transition: background var(--ease);
}
::-webkit-scrollbar-thumb:hover { background: var(--accent); }

/* ═══════════════════════════════════════════════════════════════
   SELECTION
═══════════════════════════════════════════════════════════════ */
::selection {
    background: rgba(21, 101, 255, 0.28);
    color: var(--text-1);
}

/* ═══════════════════════════════════════════════════════════════
   UTILITIES — Streamlit-specific overrides
═══════════════════════════════════════════════════════════════ */

/* Remove red/pink error highlight on inputs */
[data-baseweb="input"]:focus-within { box-shadow: none !important; }

/* Dropdown menus from selects */
[data-baseweb="popover"] [role="listbox"] {
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    border-radius: var(--r) !important;
}

[data-baseweb="popover"] [role="option"] {
    font-size: 0.85rem !important;
    color: var(--text-1) !important;
    transition: background var(--ease) !important;
}

[data-baseweb="popover"] [role="option"]:hover {
    background: var(--bg-hover) !important;
}

[data-baseweb="popover"] [aria-selected="true"] {
    background: var(--accent-dim) !important;
    color: #5B9AFF !important;
}

/* Tag chips in multiselect */
[data-baseweb="tag"] {
    background: var(--accent-dim) !important;
    border: 1px solid var(--accent-ring) !important;
    border-radius: 4px !important;
    color: #5B9AFF !important;
    font-size: 0.75rem !important;
}

/* Progress bar */
[data-testid="stProgress"] > div > div > div > div {
    background: var(--accent) !important;
}

/* Tooltip */
[data-baseweb="tooltip"] div {
    background: var(--bg-elevated) !important;
    border: 1px solid var(--bg-border) !important;
    font-size: 0.78rem !important;
    color: var(--text-1) !important;
    border-radius: var(--r) !important;
}

</style>
"""


def inject_css():
    """Inject the enterprise stylesheet. Call once at the top of app.py."""
    st.markdown(_CSS, unsafe_allow_html=True)
