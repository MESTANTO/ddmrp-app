"""
Login / Register page — shown when the user is not authenticated.
No sidebar. Full-page centred layout.
"""

import streamlit as st
from database.auth import authenticate, register_user, login


def show():
    # ── Page chrome ──────────────────────────────────────────────────────────
    st.markdown(
        """
        <style>
        /* Hide sidebar on the login page */
        [data-testid="stSidebar"]        { display: none !important; }
        [data-testid="collapsedControl"] { display: none !important; }

        /* Full-height centred card */
        .login-wrap {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 85vh;
        }
        .login-card {
            width: 420px;
            max-width: 96vw;
            background: var(--bg-elevated, #112240);
            border: 1px solid var(--bg-border, #1E3356);
            border-top: 3px solid var(--accent, #1565FF);
            border-radius: 10px;
            padding: 2.5rem 2.25rem;
        }
        .login-logo {
            font-size: 1.5rem;
            font-weight: 800;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: #E8F0FF;
            margin-bottom: 0.2rem;
        }
        .login-sub {
            font-size: 0.65rem;
            font-weight: 700;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: #3D5577;
            margin-bottom: 1.75rem;
        }
        </style>
        <div class="login-wrap">
          <div class="login-card">
            <div class="login-logo">DDMRP</div>
            <div class="login-sub">Demand Driven MRP Platform</div>
        """,
        unsafe_allow_html=True,
    )

    tab_login, tab_register = st.tabs(["Sign In", "Create Account"])

    # ── Sign In ───────────────────────────────────────────────────────────────
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

    # ── Create Account ────────────────────────────────────────────────────────
    with tab_register:
        with st.form("register_form", clear_on_submit=True):
            new_user = st.text_input("Username *", placeholder="choose a username",
                                     key="reg_username")
            new_email = st.text_input("Email", placeholder="you@company.com",
                                      key="reg_email")
            new_pw   = st.text_input("Password *", type="password",
                                     placeholder="min. 6 characters", key="reg_pw")
            new_pw2  = st.text_input("Confirm Password *", type="password",
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
                    login(result)   # result is the user dict on success
                    st.success("Account created! Setting up your workspace…")
                    st.rerun()
                else:
                    st.error(result)

    st.markdown("</div></div>", unsafe_allow_html=True)
