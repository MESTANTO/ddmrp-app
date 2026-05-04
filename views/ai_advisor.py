"""
AI Advisor — Streamlit page.

Uses the NVIDIA NIM API (OpenAI-compatible) to provide a streaming chat
interface over the current DDMRP database state.

Model: deepseek-ai/deepseek-v3-0324 via https://integrate.api.nvidia.com/v1

API key: read from Streamlit secret NVIDIA_API_KEY
  → Manage app → Secrets → add:  NVIDIA_API_KEY = "nvapi-..."
"""

import streamlit as st
from datetime import datetime
from collections import defaultdict
from statistics import mean, stdev

from openai import OpenAI

from database.db import get_session, Item, Buffer, DemandEntry

# ── API credentials ───────────────────────────────────────────────────────────
_NVIDIA_BASE  = "https://integrate.api.nvidia.com/v1"
_MODEL        = "deepseek-ai/deepseek-v3-0324"
_MAX_TOKENS   = 16384

SYSTEM_PROMPT = """You are an expert DDMRP (Demand Driven MRP) advisor integrated into a supply chain management application.

You have access to a real-time snapshot of the company's inventory data (provided below).
Your job is to:
1. Analyse the data and identify problems, risks, and opportunities
2. Suggest concrete, prioritised actions the planner should take TODAY
3. Explain your reasoning briefly and clearly
4. Use DDMRP terminology correctly (buffers, zones, NFP, TOR, ADU, DLT, decoupling points, etc.)

When suggesting actions, be specific: name the item (part number), state the problem, and say what to do.
Format your answers with clear headings and bullet points. Keep responses concise and actionable.

--- CURRENT DATA SNAPSHOT ---
{context}
--- END SNAPSHOT ---
"""

QUICK_PROMPTS = [
    ("🚨 Critical items",   "Which items need immediate attention right now? Focus on execution alarms and stockout risk."),
    ("📦 Buffer sizing",    "Which buffers are incorrectly sized? Look at items where on-hand is consistently far from the green zone midpoint."),
    ("📈 ABC-XYZ actions",  "Based on the ABC/XYZ classification, what control policy changes do you recommend?"),
    ("🔄 Replenishment",    "Are there items that should have a replenishment order placed today? Explain why."),
    ("⚠️ Data quality",     "What data quality issues do you see? Look for missing costs, zero ADU, missing DLT, etc."),
    ("📉 Slow movers",      "Which items appear to be slow movers or dead stock risks? What should we do with them?"),
]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def show():
    st.header("🤖 AI Advisor")
    st.caption(
        "Chat with **DeepSeek V3** (via NVIDIA NIM API) about your DDMRP data. "
        "The model receives a live snapshot of your inventory and suggests prioritised actions."
    )

    # ── Resolve API key from Streamlit secrets ────────────────────────────────
    try:
        api_key = st.secrets["NVIDIA_API_KEY"]
    except Exception:
        st.error("**NVIDIA_API_KEY not found in Streamlit secrets.** Add it via Manage app → Secrets.")
        return

    client = OpenAI(base_url=_NVIDIA_BASE, api_key=api_key)

    # Quick connectivity check
    with st.sidebar:
        st.divider()
        st.markdown("**🤖 AI Advisor**")
        st.caption(f"Model: `{_MODEL}`")
        st.caption("NVIDIA NIM API")
        st.divider()

    # ── Build context once per session (or on demand) ─────────────────────────
    if "ai_context" not in st.session_state:
        with st.spinner("Building data snapshot…"):
            st.session_state["ai_context"] = _build_context()

    col_refresh, col_clear = st.columns([1, 1])
    with col_refresh:
        if st.button("🔄 Refresh data snapshot"):
            with st.spinner("Refreshing…"):
                st.session_state["ai_context"] = _build_context()
            st.success("Snapshot updated.")

    with st.expander("📋 Data snapshot sent to the model", expanded=False):
        st.text(st.session_state["ai_context"])

    # ── Quick-action buttons ──────────────────────────────────────────────────
    st.markdown("**Quick analyses:**")
    btn_cols = st.columns(3)
    for i, (label, prompt) in enumerate(QUICK_PROMPTS):
        if btn_cols[i % 3].button(label, key=f"quick_{i}", use_container_width=True):
            if "ai_messages" not in st.session_state:
                st.session_state["ai_messages"] = []
            st.session_state["ai_messages"].append({"role": "user", "content": prompt})
            _stream_response(client, st.session_state["ai_context"],
                             st.session_state["ai_messages"])
            st.rerun()

    st.divider()

    # ── Chat history ──────────────────────────────────────────────────────────
    if "ai_messages" not in st.session_state:
        st.session_state["ai_messages"] = []

    for msg in st.session_state["ai_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── User input ────────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything about your inventory…")
    if user_input:
        st.session_state["ai_messages"].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        _stream_response(client, st.session_state["ai_context"],
                         st.session_state["ai_messages"])
        st.rerun()

    with col_clear:
        if st.session_state.get("ai_messages") and st.button("🗑️ Clear conversation"):
            st.session_state["ai_messages"] = []
            st.rerun()


# ---------------------------------------------------------------------------
# Streaming call
# ---------------------------------------------------------------------------

def _stream_response(client: OpenAI, context: str, messages: list):
    system = SYSTEM_PROMPT.format(context=context)
    api_messages = [{"role": "system", "content": system}] + messages

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_reply = ""
        try:
            stream = client.chat.completions.create(
                model=_MODEL,
                messages=api_messages,
                temperature=1,
                top_p=0.95,
                max_tokens=_MAX_TOKENS,
                extra_body={"chat_template_kwargs": {"thinking": False}},
                stream=True,
            )
            for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta.content
                if delta is not None:
                    full_reply += delta
                    placeholder.markdown(full_reply + "▌")
            placeholder.markdown(full_reply)
        except Exception as exc:
            full_reply = f"❌ API error: {exc}"
            placeholder.error(full_reply)

    messages.append({"role": "assistant", "content": full_reply})


# ---------------------------------------------------------------------------
# Data context builder
# ---------------------------------------------------------------------------

def _build_context() -> str:
    session = get_session()
    try:
        items   = session.query(Item).order_by(Item.part_number).all()
        demands = session.query(DemandEntry).all()
        buffers = {b.item_id: b for b in session.query(Buffer).all()}
    finally:
        session.close()

    if not items:
        return "No items in the database yet."

    lines = []
    lines.append(f"Report generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Total items: {len(items)}  |  Items with buffer: {len(buffers)}")
    lines.append("")

    demand_by_item: dict[int, list] = defaultdict(list)
    for d in demands:
        demand_by_item[d.item_id].append(d)

    # ── Per-item rows + ABC/XYZ ───────────────────────────────────────────────
    item_rows = []
    for item in items:
        buf = buffers.get(item.id)
        item_demands = demand_by_item.get(item.id, [])

        if item_demands:
            total_qty = sum(d.quantity for d in item_demands)
            span = max(1, (max(d.demand_date for d in item_demands) -
                           min(d.demand_date for d in item_demands)).days)
            annual_usage = total_qty * 365.0 / span
        else:
            annual_usage = (item.adu or 0.0) * 365.0

        annual_value = annual_usage * (item.unit_cost or 0.0)
        cv = _cv(item, item_demands)
        xyz = "X" if cv < 0.5 else ("Y" if cv < 1.0 else "Z")
        item_rows.append((item, buf, annual_usage, annual_value, cv, xyz))

    sorted_rows = sorted(item_rows, key=lambda r: r[3], reverse=True)
    total_val = sum(r[3] for r in sorted_rows) or 1.0

    # ── Item status table ─────────────────────────────────────────────────────
    lines.append("=== ITEM INVENTORY STATUS ===")
    lines.append(
        f"{'Part#':<14} {'Description':<24} {'Type':<4} "
        f"{'OnHand':>8} {'ADU':>6} {'DLT':>5} {'Cost€':>8} {'AnnVal€':>10} "
        f"{'TOR':>8} {'Stat%':>6} {'Color':<10} {'ABC'} {'XYZ'}"
    )
    lines.append("-" * 120)

    cum = 0.0
    for item, buf, annual_usage, annual_value, cv, xyz in sorted_rows:
        abc = "A" if cum / total_val < 0.70 else ("B" if cum / total_val < 0.90 else "C")
        cum += annual_value

        tor   = getattr(buf, "top_of_red",        0) if buf else 0
        sp    = f"{buf.buffer_status_pct*100:.0f}%" if (buf and buf.buffer_status_pct) else "—"
        color = (buf.execution_color or "—")         if buf else "—"

        lines.append(
            f"{item.part_number:<14} {item.description[:24]:<24} {item.item_type or 'P':<4} "
            f"{item.on_hand:>8.1f} {item.adu or 0:>6.2f} {item.dlt or 0:>5.1f} "
            f"{item.unit_cost or 0:>8.2f} {annual_value:>10,.0f} "
            f"{tor:>8.1f} {sp:>6} {color:<10} {abc}   {xyz}"
        )

    lines.append("")

    # ── Execution alarms ─────────────────────────────────────────────────────
    lines.append("=== EXECUTION ALARMS ===")
    alarms = [(item, buf) for item, buf, *_ in item_rows
              if buf and buf.execution_color in ("red", "dark_red")]
    if alarms:
        for item, buf in alarms:
            sp = f"{buf.buffer_status_pct*100:.0f}%" if buf.buffer_status_pct else "?"
            lines.append(
                f"  ❗ {item.part_number} ({item.description[:30]}): "
                f"{buf.execution_color.upper()} | on_hand={item.on_hand:.1f} | status={sp}"
            )
    else:
        lines.append("  No red/dark-red alarms.")
    lines.append("")

    # ── Buffer zones ──────────────────────────────────────────────────────────
    lines.append("=== BUFFER ZONES ===")
    buf_zones = [(item, buf) for item, buf, *_ in item_rows
                 if buf and getattr(buf, "top_of_green", 0)]
    if buf_zones:
        lines.append(f"  {'Part#':<14} {'OnHand':>8} {'TOR':>8} {'TOY':>8} {'TOG':>8} {'Stat%':>6} {'Color':<10}")
        for item, buf in buf_zones:
            sp = f"{buf.buffer_status_pct*100:.0f}%" if buf.buffer_status_pct else "—"
            lines.append(
                f"  {item.part_number:<14} {item.on_hand:>8.1f} "
                f"{getattr(buf,'top_of_red',0):>8.1f} "
                f"{getattr(buf,'top_of_yellow',0):>8.1f} "
                f"{getattr(buf,'top_of_green',0):>8.1f} "
                f"{sp:>6} {buf.execution_color or '—':<10}"
            )
    else:
        lines.append("  No buffers with calculated zones. Run Replenishment Signals first.")
    lines.append("")

    # ── ABC/XYZ matrix ────────────────────────────────────────────────────────
    lines.append("=== ABC / XYZ MATRIX (item counts) ===")
    matrix: dict[str, int] = defaultdict(int)
    cum2 = 0.0
    for item, buf, annual_usage, annual_value, cv, xyz in sorted_rows:
        abc = "A" if cum2 / total_val < 0.70 else ("B" if cum2 / total_val < 0.90 else "C")
        cum2 += annual_value
        matrix[f"{abc}-{xyz}"] += 1
    lines.append("       X    Y    Z")
    for a in ["A", "B", "C"]:
        lines.append(f"  {a}  | " + " | ".join(f"{matrix.get(f'{a}-{x}',0):>3}" for x in "XYZ") + " |")
    lines.append("")

    # ── Data quality ──────────────────────────────────────────────────────────
    lines.append("=== DATA QUALITY ISSUES ===")
    issues = []
    for item, buf, *_ in item_rows:
        if not item.unit_cost:
            issues.append(f"  • {item.part_number}: unit_cost = 0")
        if not item.adu and not demand_by_item.get(item.id):
            issues.append(f"  • {item.part_number}: ADU = 0 and no demand history")
        if not item.dlt and buf:
            issues.append(f"  • {item.part_number}: buffer exists but DLT = 0")
        if buf and not item.lead_time_factor:
            issues.append(f"  • {item.part_number}: LTF = 0 (zones will be zero)")
    if issues:
        lines.extend(issues[:40])
        if len(issues) > 40:
            lines.append(f"  … and {len(issues)-40} more")
    else:
        lines.append("  No issues detected.")

    return "\n".join(lines)


def _cv(item, demands) -> float:
    if len(demands) >= 4:
        weekly: dict = defaultdict(float)
        for d in demands:
            key = d.demand_date.isocalendar()[:2]
            weekly[key] += d.quantity
        vals = list(weekly.values())
        if len(vals) >= 2 and mean(vals) > 0:
            return stdev(vals) / mean(vals)
    return item.variability_factor or 0.0
