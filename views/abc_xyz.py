"""
ABC / XYZ / ACV² Analysis — Streamlit page.

Uses data already in the app:
  - Item.unit_cost for value
  - DemandEntry rows for annual usage and demand variability
  - Item.adu × 365 as fallback when no demand history exists
  - Item.variability_factor as CV fallback when <4 demand periods available

ABC: items ranked by annual consumption value (unit_cost × annual usage)
     A = top 70 % of total value, B = next 20 %, C = remaining 10 %
XYZ: items ranked by demand variability (coefficient of variation of weekly demand)
     X = CV < 0.5, Y = 0.5 ≤ CV < 1.0, Z = CV ≥ 1.0
ACV²: 3×3 matrix of ABC × XYZ (9 cells)
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from database.db import get_session, Item, DemandEntry


# ── Colour palettes ──────────────────────────────────────────────────────────
ABC_COLOR = {"A": "#E74C3C", "B": "#F39C12", "C": "#27AE60"}
XYZ_COLOR = {"X": "#2980B9", "Y": "#E67E22", "Z": "#C0392B"}

# ACV² cell colours: greener = easier to manage, redder = needs tightest control
ACVS_COLOR = {
    ("A", "X"): "#7B241C",   # darkest red
    ("A", "Y"): "#C0392B",
    ("A", "Z"): "#E74C3C",
    ("B", "X"): "#D35400",
    ("B", "Y"): "#F39C12",
    ("B", "Z"): "#F7DC6F",
    ("C", "X"): "#1E8449",
    ("C", "Y"): "#27AE60",
    ("C", "Z"): "#A9DFBF",   # lightest green
}
ACVS_LABEL = {
    ("A", "X"): "Critical — tight control,\nprecise forecast",
    ("A", "Y"): "Critical — advanced\nforecasting needed",
    ("A", "Z"): "Critical — special handling,\nhigh safety stock",
    ("B", "X"): "Moderate — standard\nforecast",
    ("B", "Y"): "Moderate — enhanced\nforecast",
    ("B", "Z"): "Moderate — intermittent\nhandling",
    ("C", "X"): "Basic — bulk ordering\nOK",
    ("C", "Y"): "Minimal — simple\nforecast",
    ("C", "Z"): "Minimal — simplest\nhandling",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _cached_compute(abc_a: float, abc_ab: float, xyz_x: float, xyz_y: float) -> pd.DataFrame:
    """Load all items + demand entries and compute classifications — cached 5 min."""
    session = get_session()
    try:
        items = session.query(Item).order_by(Item.part_number).all()
        demands = session.query(DemandEntry).all()
    finally:
        session.close()
    if not items:
        return pd.DataFrame()
    return _compute(items, demands, abc_a, abc_ab, xyz_x, xyz_y)


def show():
    st.header("ABC / XYZ / ACV² Analysis")
    st.caption(
        "Classify inventory by **consumption value** (ABC) and **demand variability** (XYZ), "
        "then cross them in the **ACV² matrix** to prioritise control actions."
    )

    # ── Threshold controls ────────────────────────────────────────────────────
    with st.expander("⚙️ Classification thresholds", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            abc_a = st.slider("ABC — A threshold (% of total value)", 50, 85, 70,
                              help="Items from top until this % of total value = category A")
        with col2:
            abc_ab = st.slider("ABC — A+B threshold (%)", abc_a + 5, 98, min(abc_a + 20, 90),
                               help="Items from top until this % = categories A+B combined")
        with col3:
            xyz_x = st.slider("XYZ — X max CV", 0.10, 0.80, 0.50,
                               help="Coefficient of variation below this → category X")
        with col4:
            xyz_y = st.slider("XYZ — Y max CV", xyz_x + 0.05, 2.0, max(xyz_x + 0.50, 1.0),
                               help="CV above X threshold and below this → category Y")

    # ── Compute classifications (cached by threshold values) ──────────────────
    df = _cached_compute(abc_a / 100, abc_ab / 100, xyz_x, xyz_y)

    if df.empty:
        st.info("No items in Material Master yet. Add items first.")
        return

    if df.empty:
        st.warning("No data available to classify.")
        return

    missing_cost = (df["unit_cost"] == 0).sum()
    missing_demand = (df["annual_usage"] == 0).sum()
    if missing_cost or missing_demand:
        cols = st.columns(2)
        if missing_cost:
            cols[0].warning(f"⚠️ {missing_cost} item(s) have **unit cost = 0** — excluded from ABC value ranking.")
        if missing_demand:
            cols[1].warning(f"⚠️ {missing_demand} item(s) have **no demand data and ADU = 0** — classified as C/Z by default.")

    # ── KPI strip ────────────────────────────────────────────────────────────
    total_val = df["annual_value"].sum()
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total items", len(df))
    k2.metric("Annual value (€)", f"{total_val:,.0f}")
    k3.metric("Category A items", int((df["abc"] == "A").sum()))
    k4.metric("Category X items", int((df["xyz"] == "X").sum()))
    k5.metric("Critical (A-X)", int(((df["abc"] == "A") & (df["xyz"] == "X")).sum()))

    st.divider()

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab_abc, tab_xyz, tab_matrix, tab_table = st.tabs([
        "📊 ABC Analysis", "📈 XYZ Analysis", "🔲 ACV² Matrix", "📋 Full Classification Table"
    ])

    with tab_abc:
        _render_abc(df)

    with tab_xyz:
        _render_xyz(df)

    with tab_matrix:
        _render_matrix(df)

    with tab_table:
        _render_table(df)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def _compute(items, demands, abc_a_thr, abc_ab_thr, xyz_x_thr, xyz_y_thr) -> pd.DataFrame:
    # Index demand by item
    demand_by_item: dict[int, list] = defaultdict(list)
    for d in demands:
        demand_by_item[d.item_id].append(d)

    rows = []
    for item in items:
        item_demands = demand_by_item.get(item.id, [])

        # ── Annual usage ──────────────────────────────────────────────────
        if item_demands:
            total_qty = sum(d.quantity for d in item_demands)
            dates = [d.demand_date for d in item_demands]
            span_days = max(1, (max(dates) - min(dates)).days)
            annual_usage = total_qty * 365.0 / span_days
        else:
            annual_usage = (item.adu or 0.0) * 365.0

        annual_value = annual_usage * (item.unit_cost or 0.0)

        # ── Demand variability (CV of weekly buckets) ─────────────────────
        cv = _compute_cv(item, item_demands)

        rows.append({
            "id":           item.id,
            "part_number":  item.part_number,
            "description":  item.description,
            "category":     item.category or "",
            "item_type":    item.item_type or "P",
            "unit_cost":    item.unit_cost or 0.0,
            "annual_usage": annual_usage,
            "annual_value": annual_value,
            "cv":           cv,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # ── ABC classification ────────────────────────────────────────────────
    df_val = df[df["annual_value"] > 0].copy()
    df_zero = df[df["annual_value"] <= 0].copy()

    if not df_val.empty:
        df_val = df_val.sort_values("annual_value", ascending=False).reset_index(drop=True)
        total_v = df_val["annual_value"].sum()
        df_val["cum_pct"] = df_val["annual_value"].cumsum() / total_v
        # Shift cum_pct back by 1 row: item goes into A if it was needed to reach the threshold
        df_val["cum_pct_prev"] = df_val["cum_pct"].shift(1, fill_value=0.0)
        df_val["abc"] = df_val["cum_pct_prev"].apply(
            lambda c: "A" if c < abc_a_thr else ("B" if c < abc_ab_thr else "C")
        )
        df_zero["abc"] = "C"
        df = pd.concat([df_val, df_zero], ignore_index=True)
    else:
        df["abc"] = "C"
        df["cum_pct"] = 0.0

    # ── XYZ classification ────────────────────────────────────────────────
    def _xyz(cv):
        if cv < xyz_x_thr:  return "X"
        if cv < xyz_y_thr:  return "Y"
        return "Z"

    df["xyz"] = df["cv"].apply(_xyz)
    df["acvs"] = df["abc"] + "-" + df["xyz"]

    return df


def _compute_cv(item, demands) -> float:
    """Coefficient of variation of weekly demand buckets."""
    if len(demands) >= 4:
        weekly: dict = defaultdict(float)
        for d in demands:
            # ISO week key
            week_key = d.demand_date.isocalendar()[:2]  # (year, week)
            weekly[week_key] += d.quantity

        vals = list(weekly.values())
        if len(vals) >= 2:
            mean_v = statistics.mean(vals)
            if mean_v > 0:
                return statistics.stdev(vals) / mean_v

    # Fallback: use variability_factor (0 = low, 1 = high)
    return item.variability_factor or 0.0


# ---------------------------------------------------------------------------
# ABC tab
# ---------------------------------------------------------------------------

def _render_abc(df: pd.DataFrame):
    col_chart, col_pie = st.columns([3, 1])

    # Sort by value descending (A items first)
    df_sorted = df.sort_values("annual_value", ascending=False).reset_index(drop=True)
    df_sorted["rank"] = range(1, len(df_sorted) + 1)

    total_v = df_sorted["annual_value"].sum()
    df_sorted["cum_pct"] = df_sorted["annual_value"].cumsum() / max(total_v, 1) * 100
    df_sorted["color"] = df_sorted["abc"].map(ABC_COLOR)

    with col_chart:
        st.subheader("Annual Consumption Value by Item")
        # Pareto: bars coloured by ABC + cumulative % line
        fig = go.Figure()
        for cat in ["A", "B", "C"]:
            mask = df_sorted["abc"] == cat
            fig.add_trace(go.Bar(
                x=df_sorted.loc[mask, "rank"],
                y=df_sorted.loc[mask, "annual_value"],
                name=f"Category {cat}",
                marker_color=ABC_COLOR[cat],
                hovertext=df_sorted.loc[mask].apply(
                    lambda r: f"<b>{r['part_number']}</b><br>{r['description']}<br>"
                              f"Value: €{r['annual_value']:,.0f}<br>Category: {r['abc']}",
                    axis=1,
                ),
                hoverinfo="text",
            ))

        fig.add_trace(go.Scatter(
            x=df_sorted["rank"],
            y=df_sorted["cum_pct"],
            name="Cumulative %",
            yaxis="y2",
            line=dict(color="#2C3E50", width=2, dash="dash"),
            hoverinfo="skip",
        ))

        fig.update_layout(
            barmode="stack",
            xaxis=dict(title="Items (ranked by value)", showticklabels=False),
            yaxis=dict(title="Annual Value (€)", showgrid=True),
            yaxis2=dict(title="Cumulative %", overlaying="y", side="right",
                        range=[0, 105], ticksuffix="%"),
            legend=dict(orientation="h", y=-0.15),
            height=400,
            margin=dict(t=20, b=40),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_pie:
        st.subheader("Distribution")
        # Two donuts: items count and value
        cat_stats = df.groupby("abc").agg(
            count=("id", "count"),
            value=("annual_value", "sum"),
        ).reset_index()

        fig2 = go.Figure()
        fig2.add_trace(go.Pie(
            labels=cat_stats["abc"],
            values=cat_stats["count"],
            name="Items",
            hole=0.45,
            marker_colors=[ABC_COLOR[c] for c in cat_stats["abc"]],
            textinfo="label+percent",
            domain={"x": [0, 1], "y": [0.52, 1]},
        ))
        fig2.add_trace(go.Pie(
            labels=cat_stats["abc"],
            values=cat_stats["value"],
            name="Value",
            hole=0.45,
            marker_colors=[ABC_COLOR[c] for c in cat_stats["abc"]],
            textinfo="label+percent",
            domain={"x": [0, 1], "y": [0, 0.48]},
            showlegend=False,
        ))
        fig2.update_layout(
            annotations=[
                dict(text="Items", x=0.5, y=0.76, showarrow=False, font_size=12),
                dict(text="Value", x=0.5, y=0.24, showarrow=False, font_size=12),
            ],
            height=400,
            margin=dict(t=10, b=10, l=10, r=10),
            showlegend=False,
        )
        st.plotly_chart(fig2, use_container_width=True)

    # Category summary
    st.subheader("Category Summary")
    summary = df.groupby("abc").agg(
        Items=("id", "count"),
        **{"Annual Value (€)": ("annual_value", "sum")},
        **{"Avg Value (€)": ("annual_value", "mean")},
        **{"Avg CV": ("cv", "mean")},
    ).reset_index().rename(columns={"abc": "Category"})
    total_v_all = summary["Annual Value (€)"].sum()
    summary["% of Total Value"] = (summary["Annual Value (€)"] / max(total_v_all, 1) * 100).round(1)
    summary["Annual Value (€)"] = summary["Annual Value (€)"].map("{:,.0f}".format)
    summary["Avg Value (€)"] = summary["Avg Value (€)"].map("{:,.0f}".format)
    summary["Avg CV"] = summary["Avg CV"].map("{:.2f}".format)

    def _style_abc(val):
        color = ABC_COLOR.get(val, "")
        return f"background-color: {color}; color: white; font-weight: bold" if color else ""

    st.dataframe(
        summary.style.map(_style_abc, subset=["Category"]),
        use_container_width=True, hide_index=True,
    )


# ---------------------------------------------------------------------------
# XYZ tab
# ---------------------------------------------------------------------------

def _render_xyz(df: pd.DataFrame):
    col_chart, col_pie = st.columns([3, 1])

    df_sorted = df.sort_values("cv", ascending=False).reset_index(drop=True)
    df_sorted["rank"] = range(1, len(df_sorted) + 1)

    with col_chart:
        st.subheader("Coefficient of Variation by Item")
        fig = go.Figure()
        for cat in ["Z", "Y", "X"]:
            mask = df_sorted["xyz"] == cat
            fig.add_trace(go.Bar(
                x=df_sorted.loc[mask, "rank"],
                y=df_sorted.loc[mask, "cv"],
                name=f"Category {cat}",
                marker_color=XYZ_COLOR[cat],
                hovertext=df_sorted.loc[mask].apply(
                    lambda r: f"<b>{r['part_number']}</b><br>{r['description']}<br>"
                              f"CV: {r['cv']:.2f}<br>Category: {r['xyz']}",
                    axis=1,
                ),
                hoverinfo="text",
            ))

        fig.update_layout(
            barmode="stack",
            xaxis=dict(title="Items (ranked by CV)", showticklabels=False),
            yaxis=dict(title="Coefficient of Variation (CV)"),
            legend=dict(orientation="h", y=-0.15),
            height=400,
            margin=dict(t=20, b=40),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_pie:
        st.subheader("Distribution")
        cat_stats = df.groupby("xyz").agg(count=("id", "count")).reset_index()
        fig2 = go.Figure(go.Pie(
            labels=cat_stats["xyz"],
            values=cat_stats["count"],
            hole=0.45,
            marker_colors=[XYZ_COLOR[c] for c in cat_stats["xyz"]],
            textinfo="label+percent",
        ))
        fig2.update_layout(height=400, margin=dict(t=10, b=10, l=10, r=10), showlegend=False)
        st.plotly_chart(fig2, use_container_width=True)

    # Scatter: annual value vs CV, coloured by XYZ
    st.subheader("Value vs Variability Scatter")
    fig3 = px.scatter(
        df,
        x="cv",
        y="annual_value",
        color="xyz",
        color_discrete_map=XYZ_COLOR,
        hover_data={"part_number": True, "description": True, "abc": True,
                    "annual_value": ":.0f", "cv": ":.2f"},
        labels={"cv": "Coefficient of Variation", "annual_value": "Annual Value (€)", "xyz": "XYZ"},
        height=350,
    )
    fig3.update_traces(marker=dict(size=9, line=dict(width=1, color="white")))
    fig3.update_layout(margin=dict(t=20, b=20))
    st.plotly_chart(fig3, use_container_width=True)

    # Category summary
    st.subheader("Category Summary")
    summary = df.groupby("xyz").agg(
        Items=("id", "count"),
        **{"Avg CV": ("cv", "mean")},
        **{"Max CV": ("cv", "max")},
        **{"Annual Value (€)": ("annual_value", "sum")},
    ).reset_index().rename(columns={"xyz": "Category"})
    summary["Avg CV"] = summary["Avg CV"].map("{:.2f}".format)
    summary["Max CV"] = summary["Max CV"].map("{:.2f}".format)
    summary["Annual Value (€)"] = summary["Annual Value (€)"].map("{:,.0f}".format)

    def _style_xyz(val):
        color = XYZ_COLOR.get(val, "")
        return f"background-color: {color}; color: white; font-weight: bold" if color else ""

    st.dataframe(
        summary.style.map(_style_xyz, subset=["Category"]),
        use_container_width=True, hide_index=True,
    )


# ---------------------------------------------------------------------------
# ACV² Matrix tab
# ---------------------------------------------------------------------------

def _render_matrix(df: pd.DataFrame):
    # 3×3 heatmap
    cells: dict[tuple, dict] = {}
    for abc_c in ["A", "B", "C"]:
        for xyz_c in ["X", "Y", "Z"]:
            mask = (df["abc"] == abc_c) & (df["xyz"] == xyz_c)
            sub = df[mask]
            cells[(abc_c, xyz_c)] = {
                "count": len(sub),
                "value": sub["annual_value"].sum(),
                "items": sub["part_number"].tolist(),
            }

    # Build heatmap as plotly figure
    z_count = [[cells[(a, x)]["count"] for x in ["X", "Y", "Z"]] for a in ["A", "B", "C"]]
    z_value = [[cells[(a, x)]["value"] for x in ["X", "Y", "Z"]] for a in ["A", "B", "C"]]

    # Use item count as intensity for colour, but overlay value text
    annotations = []
    for i, abc_c in enumerate(["A", "B", "C"]):
        for j, xyz_c in enumerate(["X", "Y", "Z"]):
            cell = cells[(abc_c, xyz_c)]
            color = ACVS_COLOR[(abc_c, xyz_c)]
            annotations.append(dict(
                x=j, y=i,
                text=(
                    f"<b>{abc_c}-{xyz_c}</b><br>"
                    f"{cell['count']} items<br>"
                    f"€{cell['value']:,.0f}"
                ),
                showarrow=False,
                font=dict(size=13, color="white"),
                bgcolor=color,
                bordercolor="white",
                borderwidth=2,
            ))

    # Custom coloured heatmap via a scatter matrix of squares
    fig = go.Figure()

    for i, abc_c in enumerate(["A", "B", "C"]):
        for j, xyz_c in enumerate(["X", "Y", "Z"]):
            cell = cells[(abc_c, xyz_c)]
            color = ACVS_COLOR[(abc_c, xyz_c)]
            label = ACVS_LABEL[(abc_c, xyz_c)]
            hover_items = "<br>".join(cell["items"][:10])
            if len(cell["items"]) > 10:
                hover_items += f"<br>…+{len(cell['items'])-10} more"

            fig.add_trace(go.Scatter(
                x=[j], y=[i],
                mode="markers+text",
                marker=dict(
                    size=120,
                    color=color,
                    symbol="square",
                    line=dict(color="white", width=3),
                ),
                text=[f"<b>{abc_c}-{xyz_c}</b><br>{cell['count']} items<br>€{cell['value']:,.0f}"],
                textfont=dict(size=12, color="white"),
                hovertext=(
                    f"<b>{abc_c}-{xyz_c}</b><br>"
                    f"{label.replace(chr(10), ' ')}<br>"
                    f"Items: {cell['count']}<br>"
                    f"Value: €{cell['value']:,.0f}<br>"
                    f"<br><b>Items:</b><br>{hover_items or '—'}"
                ),
                hoverinfo="text",
                showlegend=False,
            ))

    fig.update_layout(
        xaxis=dict(
            tickmode="array", tickvals=[0, 1, 2],
            ticktext=["X — Stable", "Y — Variable", "Z — Intermittent"],
            title="XYZ (Demand Variability)",
            showgrid=False, zeroline=False,
        ),
        yaxis=dict(
            tickmode="array", tickvals=[0, 1, 2],
            ticktext=["A — High Value", "B — Medium Value", "C — Low Value"],
            title="ABC (Consumption Value)",
            showgrid=False, zeroline=False,
            autorange="reversed",
        ),
        height=480,
        margin=dict(t=30, b=60, l=140, r=20),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Legend / guidance
    st.markdown("""
| Cell | Risk Level | Recommended action |
|------|-----------|-------------------|
| 🔴 **A-X** | Critical | Continuous review, precise forecast, safety stock = 0 |
| 🔴 **A-Y** | Critical | Advanced statistical forecast, frequent review |
| 🟠 **A-Z** | High | High safety stock, expediting protocols |
| 🟠 **B-X** | Moderate | Periodic review, standard forecast |
| 🟡 **B-Y** | Moderate | Enhanced forecast, moderate safety stock |
| 🟡 **B-Z** | Moderate | Min-Max policy, intermittent demand model |
| 🟢 **C-X** | Low | Bulk ordering, simple EOQ |
| 🟢 **C-Y** | Low | Minimal monitoring |
| 🟢 **C-Z** | Minimal | Stock out opportunistically |
""")

    # Top 10 items across all cells
    st.subheader("Top 10 Items by Annual Value")
    top10 = (df.sort_values("annual_value", ascending=False)
               .head(10)[["part_number", "description", "category",
                           "annual_usage", "annual_value", "cv", "abc", "xyz", "acvs"]]
               .copy())
    top10["annual_usage"] = top10["annual_usage"].map("{:,.1f}".format)
    top10["annual_value"] = top10["annual_value"].map("{:,.0f}".format)
    top10["cv"] = top10["cv"].map("{:.2f}".format)
    top10.columns = ["Part #", "Description", "Category", "Annual Usage",
                     "Annual Value (€)", "CV", "ABC", "XYZ", "ACV²"]

    def _style_row(row):
        bg = ACVS_COLOR.get((row["ABC"], row["XYZ"]), "#FFFFFF")
        # Light styling — just colour the classification cells
        return [""] * 6 + [
            f"background-color:{ABC_COLOR.get(row['ABC'],'')};color:white;font-weight:bold",
            f"background-color:{XYZ_COLOR.get(row['XYZ'],'')};color:white;font-weight:bold",
            f"background-color:{bg};color:white;font-weight:bold",
        ]

    st.dataframe(top10.style.apply(_style_row, axis=1),
                 use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Full Classification Table tab
# ---------------------------------------------------------------------------

def _render_table(df: pd.DataFrame):
    st.subheader("All Items — ABC / XYZ / ACV² Classification")

    # Filters
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        filter_abc = st.multiselect("Filter ABC", ["A", "B", "C"], default=["A", "B", "C"])
    with col_f2:
        filter_xyz = st.multiselect("Filter XYZ", ["X", "Y", "Z"], default=["X", "Y", "Z"])
    with col_f3:
        search = st.text_input("Search part / description", "")

    mask = df["abc"].isin(filter_abc) & df["xyz"].isin(filter_xyz)
    if search:
        mask &= (
            df["part_number"].str.contains(search, case=False, na=False) |
            df["description"].str.contains(search, case=False, na=False)
        )

    display = df[mask].sort_values("annual_value", ascending=False).copy()
    display["annual_usage"] = display["annual_usage"].map("{:,.1f}".format)
    display["annual_value"] = display["annual_value"].map("{:,.0f}".format)
    display["unit_cost"] = display["unit_cost"].map("{:,.2f}".format)
    display["cv"] = display["cv"].map("{:.2f}".format)

    display = display[["part_number", "description", "item_type", "category",
                        "unit_cost", "annual_usage", "annual_value", "cv",
                        "abc", "xyz", "acvs"]].copy()
    display.columns = ["Part #", "Description", "Type", "Category",
                       "Unit Cost (€)", "Annual Usage", "Annual Value (€)", "CV",
                       "ABC", "XYZ", "ACV²"]

    def _style_full(row):
        bg = ACVS_COLOR.get((row["ABC"], row["XYZ"]), "#FFFFFF")
        result = [""] * 8
        result.append(f"background-color:{ABC_COLOR.get(row['ABC'],'')};color:white;font-weight:bold")
        result.append(f"background-color:{XYZ_COLOR.get(row['XYZ'],'')};color:white;font-weight:bold")
        result.append(f"background-color:{bg};color:white;font-weight:bold")
        return result

    st.caption(f"Showing {len(display)} of {len(df)} items")
    st.dataframe(
        display.style.apply(_style_full, axis=1),
        use_container_width=True,
        hide_index=True,
        height=520,
    )
