"""
Login / Register page — shown when the user is not authenticated.
No sidebar. Full-page centred layout using st.columns.
"""

import streamlit as st
from database.auth import authenticate, register_user, login


def show():
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"]        { display: none !important; }
        [data-testid="collapsedControl"] { display: none !important; }

        .login-logo {
            font-size: 1.5rem;
            font-weight: 800;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: #E8F0FF;
            margin-bottom: 0.15rem;
            margin-top: 2rem;
            font-family: 'IBM Plex Sans', sans-serif;
        }
        .login-sub {
            font-size: 0.62rem;
            font-weight: 700;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: #3D5577;
            margin-bottom: 1.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Centre the form using columns
    _, col, _ = st.columns([1, 1.1, 1])

    with col:
        st.markdown('<div class="login-logo">DDMRP</div>', unsafe_allow_html=True)
        st.markdown('<div class="login-sub">Demand Driven MRP Platform</div>', unsafe_allow_html=True)

        tab_login, tab_register = st.tabs(["Sign In", "Create Account"])

        # ── Sign In ───────────────────────────────────────────────────────────
        with tab_login:
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("Username", placeholder="your.username")
                password = st.text_input("Password", type="password", placeholder="••••••••")
                submitted = st.form_submit_button("Sign In", type="primary",
                                                  use_container_width=True)

            if submitted:
                if not username or not password:
                    st.error("Please enter both username and password.")
                else:
                    user = authenticate(username, password)
                    if user:
                        login(user)
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")

        # ── Create Account ────────────────────────────────────────────────────
        with tab_register:
            with st.form("register_form", clear_on_submit=True):
                new_user  = st.text_input("Username *", placeholder="choose a username",
                                          key="reg_username")
                new_email = st.text_input("Email", placeholder="you@company.com",
                                          key="reg_email")
                new_pw    = st.text_input("Password *", type="password",
                                          placeholder="min. 6 characters", key="reg_pw")
                new_pw2   = st.text_input("Confirm Password *", type="password",
                                          placeholder="repeat password", key="reg_pw2")
                reg_submit = st.form_submit_button("Create Account", type="primary",
                                                   use_container_width=True)

            if reg_submit:
                if not new_user or not new_pw:
                    st.error("Username and password are required.")
                elif len(new_pw) < 6:
                    st.error("Password must be at least 6 characters.")
                elif new_pw != new_pw2:
                    st.error("Passwords do not match.")
                else:
                    ok, result = register_user(new_user, new_pw, new_email)
                    if ok:
                        login(result)
                        st.rerun()
                    else:
                        st.error(result)
