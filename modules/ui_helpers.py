"""
Shared UI helpers for the DDMRP app.

Centralises the "show top N items first, allow user to expand" pattern used
across every table/chart page. Keeps every page snappy on first render and
lets the user opt into bigger views only when needed.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import streamlit as st

# Standard set of row-limit options used everywhere
DEFAULT_LIMIT_OPTIONS = [10, 25, 50, 100, "All"]
DEFAULT_LIMIT = 10


def top_n_selector(
    total_rows: int,
    key: str,
    options: Optional[list] = None,
    default: int | str = DEFAULT_LIMIT,
    label: str = "Show",
    inline_with_search: bool = False,
) -> int:
    """
    Render a small "Show top N" selector and return the effective row limit.

    - `total_rows`: total number of rows available (used to cap and label)
    - `key`: unique Streamlit widget key
    - returns: integer row count to display (returns total_rows for "All")
    """
    if options is None:
        options = DEFAULT_LIMIT_OPTIONS

    # Find the default index
    try:
        default_idx = options.index(default)
    except ValueError:
        default_idx = 0

    choice = st.selectbox(
        f"{label} (of {total_rows:,})",
        options,
        index=default_idx,
        key=key,
        help="Initial page render is limited for speed. Increase to load more rows.",
    )

    if choice == "All" or (isinstance(choice, int) and choice >= total_rows):
        return max(total_rows, 1)
    return int(choice)


def limited_dataframe(
    df: pd.DataFrame,
    key: str,
    sort_by: Optional[str] = None,
    ascending: bool = True,
    default: int | str = DEFAULT_LIMIT,
    options: Optional[list] = None,
    label: str = "Show top",
    show_search: bool = False,
    search_columns: Optional[list[str]] = None,
    height: Optional[int] = None,
    hide_index: bool = True,
    column_config: Optional[dict] = None,
    use_container_width: bool = True,
):
    """
    Render a dataframe with a top-N selector and optional search box.

    Returns the (possibly filtered + limited) dataframe that was displayed,
    so callers can re-use the same slice for charts or counts.
    """
    if df is None or df.empty:
        st.info("No data available.")
        return df

    total = len(df)

    # Row controls
    if show_search and search_columns:
        col_n, col_search = st.columns([1, 2])
        with col_n:
            n = top_n_selector(total, key=f"{key}_n", options=options, default=default, label=label)
        with col_search:
            q = st.text_input("Search", key=f"{key}_q",
                              placeholder="filter by part #, description, …",
                              label_visibility="collapsed").strip()
        if q:
            mask = False
            for col in search_columns:
                if col in df.columns:
                    mask = mask | df[col].astype(str).str.contains(q, case=False, na=False)
            df = df[mask] if not isinstance(mask, bool) else df
    else:
        n = top_n_selector(total, key=f"{key}_n", options=options, default=default, label=label)

    # Sort then limit
    if sort_by and sort_by in df.columns:
        df_view = df.sort_values(sort_by, ascending=ascending).head(n)
    else:
        df_view = df.head(n)

    shown = len(df_view)
    st.caption(f"Showing {shown:,} of {total:,} rows")

    st.dataframe(
        df_view,
        use_container_width=use_container_width,
        hide_index=hide_index,
        column_config=column_config,
        height=height,
    )
    return df_view


def filter_top_n(
    df: pd.DataFrame,
    key: str,
    sort_by: Optional[str] = None,
    ascending: bool = True,
    default: int | str = DEFAULT_LIMIT,
    options: Optional[list] = None,
    label: str = "Show top",
) -> pd.DataFrame:
    """
    Lightweight variant: just renders the selector and returns the limited
    dataframe slice without displaying it. Useful when the caller wants to
    show its own custom-rendered table or chart.
    """
    if df is None or df.empty:
        return df
    total = len(df)
    n = top_n_selector(total, key=f"{key}_n", options=options, default=default, label=label)
    if sort_by and sort_by in df.columns:
        return df.sort_values(sort_by, ascending=ascending).head(n)
    return df.head(n)
