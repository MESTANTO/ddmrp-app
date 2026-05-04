"""
AI Advisor — Streamlit page.

Connects to a locally-running Ollama instance, builds a structured snapshot
of the current DDMRP database state, and provides a streaming chat interface
so the user can interrogate data and get suggested actions.

Requirements:
  • Ollama running locally:  https://ollama.com/download
  • At least one model pulled: `ollama pull llama3` (or any model you prefer)
  • Default URL: http://localhost:11434  (configurable in the sidebar)

The page works offline from Streamlit Cloud — it just needs network access
to your local Ollama instance.
"""

import json
import requests
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

from database.db import (
    get_session, Item, Buffer, DemandEntry, SupplyEntry,
    ProcessNode, BomLine
)

OLLAMA_DEFAULT = "http://localhost:11434"

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
    ("🚨 Critical items", "Which items need immediate attention right now? Focus on execution alarms and stockout risk."),
    ("📦 Buffer sizing", "Which buffers are incorrectly sized? Look at items where on-hand is consistently far from the green zone midpoint."),
    ("📈 ABC-XYZ actions", "Based on the ABC/XYZ classification, what control policy changes do you recommend?"),
    ("🔄 Replenishment", "Are there items that should have a replenishment order placed today? Explain why."),
    ("⚠️ Data quality", "What data quality issues do you see? Look for missing costs, zero ADU, missing DLT, etc."),
    ("📉 Slow movers", "Which items appear to be slow movers or dead stock risks? What should we do with them?"),
]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def show():
    st.header("🤖 AI Advisor")
    st.caption(
        "Chat with a local LLM (via **Ollama**) about your DDMRP data. "
        "The model receives a live snapshot of your inventory and can suggest actions."
    )

    # ── Sidebar config ────────────────────────────────────────────────────────
    with st.sidebar:
        st.divider()
        st.markdown("**🤖 AI Advisor settings**")
        ollama_url = st.text_input("Ollama URL", value=OLLAMA_DEFAULT, key="ollama_url")
        model_name = _pick_model(ollama_url)
        if model_name:
            st.success(f"Model: `{model_name}`")
        st.divider()

    if not model_name:
        st.error(
            "**Cannot reach Ollama.** Make sure Ollama is running locally:\n\n"
            "```bash\n# Install: https://ollama.com/download\nollama serve\n"
            "ollama pull llama3   # or any model you prefer\n```\n\n"
            f"Then check that `{ollama_url}` is reachable from this browser."
        )
        return

    # ── Build context once per session (or on demand) ────────────────────────
    if "ai_context" not in st.session_state or st.button("🔄 Refresh data snapshot", key="refresh_ctx"):
        with st.spinner("Building data snapshot…"):
            st.session_state["ai_context"] = _build_context()

    with st.expander("📋 Current data snapshot sent to the model", expanded=False):
        st.text(st.session_state["ai_context"])

    # ── Chat history ──────────────────────────────────────────────────────────
    if "ai_messages" not in st.session_state:
        st.session_state["ai_messages"] = []

    # Quick-action buttons
    st.markdown("**Quick analyses:**")
    cols = st.columns(3)
    for i, (label, prompt) in enumerate(QUICK_PROMPTS):
        if cols[i % 3].button(label, key=f"quick_{i}", use_container_width=True):
            st.session_state["ai_messages"].append({"role": "user", "content": prompt})
            _stream_response(ollama_url, model_name,
                             st.session_state["ai_context"],
                             st.session_state["ai_messages"])
            st.rerun()

    st.divider()

    # Render chat history
    for msg in st.session_state["ai_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # User input
    user_input = st.chat_input("Ask anything about your inventory…")
    if user_input:
        st.session_state["ai_messages"].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        _stream_response(ollama_url, model_name,
                         st.session_state["ai_context"],
                         st.session_state["ai_messages"])
        st.rerun()

    if st.session_state["ai_messages"]:
        if st.button("🗑️ Clear conversation", key="clear_chat"):
            st.session_state["ai_messages"] = []
            st.rerun()


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def _pick_model(base_url: str) -> str | None:
    """Fetch available models from Ollama; let user pick one. Returns model name or None."""
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        return None

    if not models:
        st.sidebar.warning("No models found. Run `ollama pull <model>` first.")
        return None

    return st.sidebar.selectbox("Model", models, key="ollama_model")


def _stream_response(base_url: str, model: str, context: str, messages: list):
    """Call Ollama /api/chat with streaming and append the assistant reply."""
    system = SYSTEM_PROMPT.format(context=context)

    payload = {
        "model": model,
        "stream": True,
        "messages": [{"role": "system", "content": system}] + messages,
    }

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_reply = ""
        try:
            with requests.post(f"{base_url}/api/chat", json=payload,
                               stream=True, timeout=120) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    delta = chunk.get("message", {}).get("content", "")
                    full_reply += delta
                    placeholder.markdown(full_reply + "▌")
                    if chunk.get("done"):
                        break
            placeholder.markdown(full_reply)
        except Exception as exc:
            full_reply = f"❌ Error communicating with Ollama: {exc}"
            placeholder.error(full_reply)

    messages.append({"role": "assistant", "content": full_reply})


# ---------------------------------------------------------------------------
# Data context builder
# ---------------------------------------------------------------------------

def _build_context() -> str:
    """
    Pull a structured snapshot of the DB and format it as plain text
    that the LLM can reason over.
    """
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
    lines.append(f"Total items: {len(items)}")
    lines.append(f"Items with DDMRP buffer: {len(buffers)}")
    lines.append("")

    # ── Demand index ─────────────────────────────────────────────────────────
    demand_by_item: dict[int, list] = defaultdict(list)
    for d in demands:
        demand_by_item[d.item_id].append(d)

    # ── Per-item data ─────────────────────────────────────────────────────────
    lines.append("=== ITEM INVENTORY STATUS ===")
    lines.append(
        f"{'Part#':<14} {'Desc':<22} {'Type':<4} "
        f"{'OnHand':>8} {'ADU':>6} {'DLT':>5} "
        f"{'Cost':>8} {'AnnVal':>10} "
        f"{'TOR':>8} {'Status%':>8} {'ExecColor':<10} {'ABC':<3} {'XYZ':<3}"
    )
    lines.append("-" * 115)

    item_rows = []
    for item in items:
        buf = buffers.get(item.id)

        # Annual value
        item_demands = demand_by_item.get(item.id, [])
        if item_demands:
            total_qty = sum(d.quantity for d in item_demands)
            span = max(1, (max(d.demand_date for d in item_demands) -
                           min(d.demand_date for d in item_demands)).days)
            annual_usage = total_qty * 365.0 / span
        else:
            annual_usage = (item.adu or 0.0) * 365.0
        annual_value = annual_usage * (item.unit_cost or 0.0)

        # CV for XYZ
        cv = _cv(item, item_demands)
        xyz = "X" if cv < 0.5 else ("Y" if cv < 1.0 else "Z")

        # ABC (we compute a rough one here — full ranking not available per item)
        # We'll attach it properly in the summary; for per-row just store annual_value
        item_rows.append((item, buf, annual_usage, annual_value, cv, xyz))

    # ABC ranking
    sorted_rows = sorted(item_rows, key=lambda r: r[3], reverse=True)
    total_val = sum(r[3] for r in sorted_rows)
    cum = 0.0
    for item, buf, annual_usage, annual_value, cv, xyz in sorted_rows:
        cum_prev = cum
        cum += annual_value
        abc = "A" if cum_prev / max(total_val, 1) < 0.70 else (
              "B" if cum_prev / max(total_val, 1) < 0.90 else "C")

        tor   = buf.top_of_red if buf and hasattr(buf, "top_of_red") else 0
        sp    = f"{buf.buffer_status_pct*100:.0f}%" if buf and buf.buffer_status_pct else "—"
        color = buf.execution_color if buf and buf.execution_color else "—"

        lines.append(
            f"{item.part_number:<14} {item.description[:22]:<22} {item.item_type or 'P':<4} "
            f"{item.on_hand:>8.1f} {item.adu or 0:>6.2f} {item.dlt or 0:>5.1f} "
            f"{item.unit_cost or 0:>8.2f} {annual_value:>10,.0f} "
            f"{tor:>8.1f} {sp:>8} {color:<10} {abc:<3} {xyz:<3}"
        )

    lines.append("")

    # ── Execution alarms ─────────────────────────────────────────────────────
    lines.append("=== EXECUTION ALARMS ===")
    alarm_items = [
        (item, buf) for item, buf, *_ in item_rows
        if buf and buf.execution_color in ("red", "dark_red")
    ]
    if alarm_items:
        for item, buf in alarm_items:
            pct = f"{buf.buffer_status_pct*100:.0f}%" if buf.buffer_status_pct else "?"
            lines.append(
                f"  ❗ {item.part_number} ({item.description[:30]}): "
                f"status={buf.execution_color.upper()}, "
                f"on_hand={item.on_hand:.1f}, status%={pct}"
            )
    else:
        lines.append("  No red/dark-red execution alarms.")
    lines.append("")

    # ── Data quality issues ───────────────────────────────────────────────────
    lines.append("=== DATA QUALITY ISSUES ===")
    issues = []
    for item, buf, *_ in item_rows:
        if not item.unit_cost:
            issues.append(f"  • {item.part_number}: unit_cost = 0 (cannot compute value)")
        if not item.adu and not demand_by_item.get(item.id):
            issues.append(f"  • {item.part_number}: ADU = 0 and no demand history")
        if not item.dlt and buf:
            issues.append(f"  • {item.part_number}: has buffer but DLT = 0")
        if buf and not item.lead_time_factor:
            issues.append(f"  • {item.part_number}: LTF = 0 (buffer zones will be zero)")
    if issues:
        lines.extend(issues[:30])
        if len(issues) > 30:
            lines.append(f"  … and {len(issues)-30} more issues")
    else:
        lines.append("  No data quality issues detected.")
    lines.append("")

    # ── ABC/XYZ summary ───────────────────────────────────────────────────────
    lines.append("=== ABC / XYZ SUMMARY ===")
    abc_counts: dict[str, int] = defaultdict(int)
    xyz_counts: dict[str, int] = defaultdict(int)
    matrix:     dict[str, int] = defaultdict(int)
    cum2 = 0.0
    for item, buf, annual_usage, annual_value, cv, xyz in sorted_rows:
        cum_prev = cum2
        cum2 += annual_value
        abc = "A" if cum_prev / max(total_val, 1) < 0.70 else (
              "B" if cum_prev / max(total_val, 1) < 0.90 else "C")
        abc_counts[abc] += 1
        xyz_counts[xyz] += 1
        matrix[f"{abc}-{xyz}"] += 1

    for cat in ["A", "B", "C"]:
        lines.append(f"  {cat}: {abc_counts[cat]} items")
    lines.append("")
    for cat in ["X", "Y", "Z"]:
        lines.append(f"  {cat}: {xyz_counts[cat]} items")
    lines.append("")
    lines.append("  ACV² matrix (item counts):")
    for a in ["A", "B", "C"]:
        row_str = "  " + a + " | " + " | ".join(
            f"{matrix.get(f'{a}-{x}', 0):>4}" for x in ["X", "Y", "Z"]
        )
        lines.append(row_str)
    lines.append("        X      Y      Z")
    lines.append("")

    # ── Buffer zone summary ───────────────────────────────────────────────────
    lines.append("=== BUFFER ZONE OVERVIEW ===")
    buf_with_zones = [
        (item, buf) for item, buf, *_ in item_rows
        if buf and getattr(buf, "top_of_green", 0)
    ]
    if buf_with_zones:
        lines.append(
            f"  {'Part#':<14} {'OnHand':>8} {'TOR':>8} {'TOY':>8} {'TOG':>8} "
            f"{'Status%':>8} {'Color':<10}"
        )
        for item, buf in buf_with_zones:
            sp = f"{buf.buffer_status_pct*100:.0f}%" if buf.buffer_status_pct else "—"
            lines.append(
                f"  {item.part_number:<14} {item.on_hand:>8.1f} "
                f"{getattr(buf,'top_of_red',0):>8.1f} "
                f"{getattr(buf,'top_of_yellow',0):>8.1f} "
                f"{getattr(buf,'top_of_green',0):>8.1f} "
                f"{sp:>8} {buf.execution_color or '—':<10}"
            )
    else:
        lines.append("  No buffers with calculated zones yet. Run Replenishment Signals first.")

    return "\n".join(lines)


def _cv(item, demands) -> float:
    from statistics import mean, stdev
    if len(demands) >= 4:
        weekly: dict = defaultdict(float)
        for d in demands:
            key = d.demand_date.isocalendar()[:2]
            weekly[key] += d.quantity
        vals = list(weekly.values())
        if len(vals) >= 2 and mean(vals) > 0:
            return stdev(vals) / mean(vals)
    return item.variability_factor or 0.0
