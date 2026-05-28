"""
Streamlit views for the deterministic optimization module.

Each `ses0X_<name>.py` view is a self-contained Streamlit page that calls
the matching solver in `modules/optimization/`.

The dispatcher lives in `views/optimization/home.py` and is wired into the
app sidebar via the `optimization_home` slug in `app.py`.
"""
