"""
Company Setup — shown after first login when the user has no company yet.
Also re-used as the "Company Profile" settings section.
"""

import streamlit as st
from database.auth import (
    get_current_user, create_company, refresh_session_company,
    get_company_info, update_company,
)


CURRENCIES = ["EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
              "RON", "BGN", "HRK", "CAD", "AUD", "NZD", "JPY", "CNY", "INR", "BRL"]

INDUSTRIES = [
    "Manufacturing", "Automotive", "Aerospace & Defence", "Electronics",
    "Food & Beverage", "Pharmaceuticals", "Chemicals", "Consumer Goods",
    "Retail & Distribution", "Construction", "Energy & Utilities",
    "Logistics & Transport", "Medical Devices", "Textile & Apparel", "Other",
]


def show_setup():
    """
    First-time company setup wizard.
    Shown when the authenticated user has no company_id yet.
    """
    user = get_current_user()

    # Hide sidebar
    st.markdown(
        """<style>
        [data-testid="stSidebar"]        { display: none !important; }
        [data-testid="collapsedControl"] { display: none !important; }
        </style>""",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<div style='max-width:600px;margin:4rem auto 0'>"
        "<div style='font-size:1.5rem;font-weight:800;letter-spacing:0.08em;"
        "text-transform:uppercase;color:#E8F0FF;margin-bottom:0.25rem'>Company Setup</div>"
        "<div style='font-size:0.75rem;color:#7A92BB;margin-bottom:2rem'>"
        f"Welcome, <strong>{user['username']}</strong>. "
        "Set up your company profile to continue. You can update these details later.</div>",
        unsafe_allow_html=True,
    )

    with st.form("company_setup_form"):
        st.markdown("**Company Information**")
        c1, c2 = st.columns(2)
        with c1:
            name     = st.text_input("Company Name *", placeholder="Acme Manufacturing Srl")
            industry = st.selectbox("Industry", [""] + INDUSTRIES)
            country  = st.text_input("Country", placeholder="Italy")
        with c2:
            city     = st.text_input("City", placeholder="Milan")
            currency = st.selectbox("Currency", CURRENCIES,
                                    index=CURRENCIES.index("EUR"))
            website  = st.text_input("Website", placeholder="https://acme.com")

        notes = st.text_area("Notes (optional)", height=80,
                             placeholder="Any additional information…")

        submitted = st.form_submit_button("Continue →", type="primary",
                                          use_container_width=True)

    if submitted:
        if not name.strip():
            st.error("Company name is required.")
            return

        ok, result = create_company(
            user_id=user["id"],
            name=name,
            industry=industry,
            country=country,
            city=city,
            currency=currency,
            website=website,
            notes=notes,
        )
        if ok:
            refresh_session_company(user["id"])
            st.success("Company created! Loading your workspace…")
            st.rerun()
        else:
            st.error(f"Error creating company: {result}")

    st.markdown("</div>", unsafe_allow_html=True)


def show_profile():
    """
    Company profile editor — embedded inside the Settings page.
    """
    user       = get_current_user()
    company_id = user.get("company_id")
    info       = get_company_info(company_id) if company_id else {}

    if not info:
        st.warning("No company linked to this account.")
        return

    st.subheader("Company Profile")

    with st.form("company_profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            name     = st.text_input("Company Name *", value=info.get("name", ""))
            industry = st.selectbox(
                "Industry",
                [""] + INDUSTRIES,
                index=([""] + INDUSTRIES).index(info.get("industry", ""))
                if info.get("industry", "") in ([""] + INDUSTRIES) else 0,
            )
            country  = st.text_input("Country", value=info.get("country", ""))
        with c2:
            city     = st.text_input("City", value=info.get("city", ""))
            cur_val  = info.get("currency", "EUR")
            currency = st.selectbox(
                "Currency", CURRENCIES,
                index=CURRENCIES.index(cur_val) if cur_val in CURRENCIES else 0,
            )
            website  = st.text_input("Website", value=info.get("website", ""))

        notes = st.text_area("Notes", value=info.get("notes", ""), height=80)

        saved = st.form_submit_button("Save Company Profile", type="primary")

    if saved:
        if not name.strip():
            st.error("Company name is required.")
            return
        ok = update_company(
            company_id,
            name=name.strip(), industry=industry, country=country.strip(),
            city=city.strip(), currency=currency, website=website.strip(),
            notes=notes.strip(),
        )
        if ok:
            st.success("Company profile saved.")
        else:
            st.error("Error saving company profile.")
