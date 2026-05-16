"""
AI Inventory Manager — merged Streamlit page.

Combines what used to be two separate pages:

  • Inventory Manager Agent  → "Structured Analysis" tab (the 8-skill run)
  • AI Advisor               → "Chat" tab (tool-calling agent)

The chat is an inventory-value expert: it can call any of the 8 skills,
query specific data slices, compute headline € figures, and render
charts inline in the conversation.

All data is scoped to the logged-in user's company_id.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from agents import chat_tools
from agents.inventory_agent import ANALYSIS_CATEGORIES
from agents.llm_client import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    TOOL_CAPABLE_MODELS,
    chat_completion,
    get_api_key,
)
from database.auth import get_company_id, get_current_user
from database.db import SessionLocal, AgentRun, AgentSignal, _migrate_agent_tables


def _ensure_agent_schema():
    """
    Run the agent_runs / agent_signals migration once per Streamlit session.

    init_db() is cached at app boot via @st.cache_resource, so on Streamlit
    Cloud a stale boot can leave the production DB without the newer
    columns (analyses_planned, analyses_done, analysis_category). We
    re-run the idempotent migration here so the page self-heals without
    waiting for a container restart.
    """
    if st.session_state.get("aim_schema_migrated"):
        return
    try:
        _migrate_agent_tables()
        st.session_state["aim_schema_migrated"] = True
    except Exception as exc:
        st.warning(f"Schema migration attempt failed: `{type(exc).__name__}: {exc}`")


# ---------------------------------------------------------------------------
# Category / severity / type metadata (lifted from views/inventory_manager.py)
# ---------------------------------------------------------------------------

_CAT_META = {c["key"]: c for c in ANALYSIS_CATEGORIES}

_CAT_ICON = {
    "data_quality":       "🗂",
    "buffer_nfp":         "🚦",
    "abc_xyz":            "🏷",
    "demand_variability": "📈",
    "safety_stock":       "🛡",
    "overstock":          "📦",
    "supplier_risk":      "🚚",
    "value_reduction":    "💶",
}

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEV_COLOR = {
    "critical": "#E74C3C", "high": "#E67E22", "medium": "#F1C40F",
    "low":      "#2980B9", "info": "#7F8C8D",
}
_SEV_BADGE = {
    "critical": "🔴 CRITICAL", "high": "🟠 HIGH", "medium": "🟡 MEDIUM",
    "low":      "🔵 LOW",      "info": "⚪ INFO",
}
_TYPE_LABEL = {
    "stockout_risk":    "⚡ Stockout Risk",
    "overstock":        "📦 Overstock",
    "low_nfp":          "📉 Low NFP",
    "buffer_resizing":  "🔧 Buffer Resizing",
    "data_quality":     "🗂 Data Quality",
    "demand_trend":     "📈 Demand Trend",
    "abc_xyz_policy":   "🏷 ABC/XYZ Policy",
    "safety_stock_gap": "🛡 Safety Stock",
    "supplier_risk":    "🚚 Supplier Risk",
    "portfolio":        "🌐 Portfolio",
}

# Inventory-value-focused quick prompts (replace the old generic ones)
QUICK_PROMPTS = [
    ("💶 Where is my cash trapped?",
     "Compute the inventory value summary and tell me where the cash is "
     "tied up. Break it down by ABC class and by execution colour, then "
     "render a bar chart of € by ABC class."),
    ("🎯 Top 10 cash-release opportunities",
     "Run the inventory value reduction analysis and give me the top 10 "
     "items ranked by € I can release. State the lever and the action for "
     "each one."),
    ("📦 Rank overstock by € impact",
     "List the overstock items sorted by excess value. Show me the top 10 "
     "with on-hand vs TOG and the € I'd free up by liquidating. Render a "
     "horizontal bar chart of excess € per part."),
    ("🛡 ABC-weighted safety gaps",
     "Which items are below TOR right now? Rank by ABC class first then by "
     "€ gap. Tell me which ones risk a stockout in the next 7 days."),
    ("🚚 Supplier-driven cash drain",
     "Which suppliers are forcing me to carry extra stock (low reliability "
     "or DLT << supplier LT)? Quantify the buffer € I could release by "
     "compressing lead time."),
    ("📉 Stale buffers wasting cash",
     "Run the demand variability analysis. Which items have recent ADU "
     "far below stored ADU? For each, estimate the € freed by right-sizing "
     "the buffer."),
]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def show():
    st.header("🤖 AI Inventory Manager")
    st.caption(
        "An inventory-value expert that combines 8 focused DDMRP skills with a "
        "tool-calling chat. Ask it where your cash is trapped, run any of the "
        "skills on demand, and let it render charts inline."
    )

    # Self-heal the agent_runs / agent_signals schema if a stale init_db
    # cache means the new columns aren't yet on prod.
    _ensure_agent_schema()

    api_key = get_api_key()
    if not api_key:
        st.error("**NVIDIA_API_KEY not found in Streamlit secrets.** "
                 "Add it via Manage app → Secrets to enable the AI features.")
        return

    company_id = get_company_id()
    user       = get_current_user()
    user_id    = user["id"] if user else 0

    # Shared model selector (both tabs use the same)
    with st.sidebar:
        st.divider()
        st.markdown("**🤖 AI Inventory Manager**")
        default_idx = KNOWN_MODELS.index(DEFAULT_MODEL) if DEFAULT_MODEL in KNOWN_MODELS else 0
        model_choice = st.selectbox("Model", KNOWN_MODELS, index=default_idx,
                                    key="aim_model_sel")
        custom = st.text_input("Custom model slug", "", key="aim_model_custom",
                               placeholder="overrides the dropdown")
        model = custom.strip() if custom.strip() else model_choice
        st.caption(f"`{model}`")
        tool_capable = model in TOOL_CAPABLE_MODELS
        st.caption("✅ Tool-calling supported" if tool_capable
                   else "⚠️ This model may not support tool-calling — chat will fall back.")
        st.divider()

    tab_struct, tab_chat = st.tabs([
        "📊 Structured Analysis (8 skills)",
        "💬 Chat",
    ])

    with tab_struct:
        _show_structured_tab(company_id, user_id, model, api_key)

    with tab_chat:
        _show_chat_tab(company_id, model, api_key)


# ===========================================================================
# TAB 1 — Structured Analysis
# (lifted from views/inventory_manager.py, simplified to share the model picker)
# ===========================================================================

def _show_structured_tab(company_id: int, user_id: int, model: str, api_key: str):
    st.caption(
        "Runs **8 sequential focused analyses** — Data Quality · Buffer & NFP · ABC/XYZ · "
        "Demand Variability · Safety Stock · Overstock · Supplier Risk · "
        "**Inventory Value Reduction**. Each uses a dedicated skill and stores "
        "category-tagged signals."
    )

    in_progress = _is_run_in_progress(company_id)
    if st.button("▶  Run Analysis" if not in_progress else "⏳  Running…",
                 type="primary", disabled=in_progress, key="aim_run_btn"):
        _execute_run(company_id, user_id, model, api_key)

    runs = _load_runs(company_id)
    if not runs:
        st.info("No agent runs yet. Click **Run Analysis** to start the first analysis.")
        return

    st.divider()

    # ── Summary metrics ──────────────────────────────────────────────────
    last_run       = runs[0]
    last_completed = next((r for r in runs if r["status"] == "completed"), None)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Runs", len(runs))
    c2.metric("Last Run",
              last_run["run_at"].strftime("%d %b %H:%M") if last_run["run_at"] else "—")
    c3.metric("Signals (last run)", last_run.get("signals_generated", 0))
    if last_completed:
        sigs_last     = _load_signals(company_id, last_completed["id"])
        critical_high = sum(1 for s in sigs_last if s["severity"] in ("critical", "high"))
        c4.metric("Critical / High", critical_high)
    else:
        c4.metric("Critical / High", "—")

    st.divider()

    # ── Run selector ─────────────────────────────────────────────────────
    run_options = {}
    for r in runs:
        icon  = "✅" if r["status"] == "completed" else ("❌" if r["status"] == "failed" else "⏳")
        ts    = r["run_at"].strftime("%Y-%m-%d %H:%M") if r["run_at"] else "?"
        model_short = r["model_used"].split("/")[-1] if r["model_used"] else ""
        label = f"{icon} {ts} — {r['signals_generated']} signals ({model_short})"
        run_options[label] = r["id"]

    selected_label  = st.selectbox("Select run to view", list(run_options.keys()),
                                   key="aim_run_sel")
    selected_run_id = run_options[selected_label]
    selected_run    = next(r for r in runs if r["id"] == selected_run_id)

    if selected_run["status"] == "failed":
        st.error(f"This run failed: {selected_run.get('error_message', 'Unknown error')}")

    _show_run_analysis_summary(selected_run)

    signals = _load_signals(company_id, selected_run_id)
    if not signals:
        st.info("No signals for this run.")
        return

    _show_charts(signals, runs)
    st.divider()
    _show_signals_by_category(signals, company_id, user_id)

    st.divider()
    with st.expander("📋 Run History", expanded=False):
        df_runs = pd.DataFrame(runs)
        if "run_at" in df_runs.columns:
            df_runs["run_at"] = df_runs["run_at"].apply(
                lambda x: x.strftime("%Y-%m-%d %H:%M") if x else "")
        cols = [c for c in ["run_at", "model_used", "status",
                            "items_analysed", "signals_generated", "duration_seconds"]
                if c in df_runs.columns]
        df_runs = df_runs[cols]
        df_runs.columns = ["Run At", "Model", "Status", "Items", "Signals",
                           "Duration (s)"][:len(cols)]
        st.dataframe(df_runs, use_container_width=True, hide_index=True)


# ===========================================================================
# TAB 2 — Chat (tool-calling)
# ===========================================================================

CHAT_SYSTEM_PROMPT = """You are the **AI Inventory Manager**, an inventory-value expert
embedded in a DDMRP supply chain application. You speak with operators and
planners.

**Your headline expertise is inventory value (€) analysis and reduction.**
Whenever the user asks anything, your first instinct is to quantify the €
impact and recommend cash-release actions ranked by `eur_release × confidence`.
You operate under skill_08 (Inventory Value Reduction) as your primary
playbook, and the other 7 skills feed into it.

You have direct access to the company's live inventory data through TOOLS.
Always call a tool before answering a data question — never guess numbers.
After a tool returns, summarise the result for the user in plain English
with concrete € figures, item names, and prioritized actions. Use bullet
points and short headings.

When a chart would help, call `render_chart` with kind ∈ {bar, pie, scatter,
line}; it renders inline beneath your message.

Hard rules:
1. Never recommend reducing stock on an item whose execution_color is red
   or dark_red — service risk trumps cash release.
2. Quote the € figure to the nearest euro in titles, two decimals in detail.
3. Cite the specific tool you used so the user can verify.
4. Stop calling tools once you have enough information to answer; aim for
   ≤ 3 tool calls per turn.

**Available tools (call by their EXACT names):**
- `inventory_value_summary` — totals: on-hand €, annual usage €, € by ABC, € by execution color.
- `list_overstock(top_n)` — items where on_hand > top_of_green, sorted by excess €.
- `list_safety_gaps(top_n)` — items where on_hand < top_of_red.
- `list_low_nfp(top_n)` — items with low Net Flow Position.
- `list_supplier_risk(top_n)` — suppliers with reliability or lead-time risk.
- `list_demand_trends(top_n)` — items whose recent ADU diverges from stored ADU.
- `list_data_quality()` — items missing key master data.
- `abc_xyz_matrix()` — 9-cell ABC/XYZ counts and € per cell.
- `run_skill(skill_id)` — execute one focused skill (1..8) and return its signals.
- `propose_value_reduction(target_eur)` — runs skill_08 and returns ranked levers.
- `render_chart({kind, title, x, y, x_label, y_label})` — renders inline; `x` and `y` MUST be arrays of equal length.

**Tool-call format:** If your runtime supports OpenAI-style structured
tool calls (function calling), use that. Otherwise, emit each tool call
as a single line of text exactly like:

    <tool_call>{"name": "list_overstock", "arguments": {"top_n": 10}}</tool_call>

— one tool per `<tool_call>` block, valid JSON inside. The app parses
both formats. Never invent tool names not listed above.
"""


def _show_chat_tab(company_id: int, model: str, api_key: str):
    # Initialise per-session state buckets
    st.session_state.setdefault("aim_chat_messages", [])  # display messages
    st.session_state.setdefault("aim_chat_charts",   {})  # idx → list of chart specs

    # Header / controls row
    c_refresh, c_clear = st.columns([1, 1])
    with c_refresh:
        st.caption(f"Using model: `{model}`")
    with c_clear:
        if st.session_state["aim_chat_messages"] and st.button(
                "🗑️ Clear conversation", key="aim_chat_clear"):
            st.session_state["aim_chat_messages"] = []
            st.session_state["aim_chat_charts"]   = {}
            st.rerun()

    # Quick-action buttons
    st.markdown("**Quick analyses:**")
    btn_cols = st.columns(3)
    for i, (label, prompt) in enumerate(QUICK_PROMPTS):
        if btn_cols[i % 3].button(label, key=f"aim_qp_{i}",
                                  use_container_width=True):
            _handle_user_turn(prompt, company_id, model, api_key)
            st.rerun()

    st.divider()

    # ── Render history ────────────────────────────────────────────────
    for idx, msg in enumerate(st.session_state["aim_chat_messages"]):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            # Inline charts for this assistant message
            for spec in st.session_state["aim_chat_charts"].get(idx, []):
                _render_chart_spec(spec)

    # ── Input ─────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything about your inventory…")
    if user_input:
        _handle_user_turn(user_input, company_id, model, api_key)
        st.rerun()


def _handle_user_turn(user_text: str, company_id: int, model: str, api_key: str):
    """
    Append the user message, run the tool-calling loop, append the assistant
    message and any chart specs to session state. Renders happen on rerun.
    """
    msgs = st.session_state["aim_chat_messages"]
    msgs.append({"role": "user", "content": user_text})

    # Build API messages — keep the last 8 turns to bound context
    history_for_api = []
    for m in msgs[-16:]:
        history_for_api.append({"role": m["role"], "content": m["content"]})

    api_messages: list[dict] = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        *history_for_api,
    ]

    with st.spinner(f"Asking {model.split('/')[-1]}…"):
        try:
            assistant_text, chart_specs = _run_tool_loop(
                api_messages, model=model, company_id=company_id, api_key=api_key,
            )
        except Exception as exc:
            assistant_text = f"❌ {type(exc).__name__}: {exc}"
            chart_specs    = []

    msgs.append({"role": "assistant", "content": assistant_text or "_(empty reply)_"})
    if chart_specs:
        st.session_state["aim_chat_charts"][len(msgs) - 1] = chart_specs


def _run_tool_loop(
    api_messages: list[dict], *, model: str, company_id: int, api_key: str,
    max_iter: int = 6,
) -> tuple[str, list[dict]]:
    """
    OpenAI-style tool-calling loop. Returns (final_assistant_text, chart_specs).

    Falls back to a tool-less call if the API rejects the tools parameter.
    """
    chart_specs: list[dict] = []
    tool_capable = model in TOOL_CAPABLE_MODELS
    last_text    = ""

    for _ in range(max_iter):
        try:
            if tool_capable:
                resp = chat_completion(
                    api_messages, model=model,
                    tools=chat_tools.TOOL_SCHEMAS, tool_choice="auto",
                    temperature=0.3, max_tokens=4096,
                )
            else:
                resp = chat_completion(
                    api_messages, model=model,
                    temperature=0.3, max_tokens=4096,
                )
        except Exception as exc:
            # If tools were rejected, drop them and try once more
            if tool_capable and ("tool" in str(exc).lower() or "400" in str(exc)):
                tool_capable = False
                continue
            raise

        choice  = resp.choices[0]
        message = choice.message
        tool_calls = getattr(message, "tool_calls", None) or []
        content    = message.content or ""

        # Some models (Kimi, Qwen, some llama variants) emit tool calls as
        # plain text inside <tool_call>...</tool_call> rather than the
        # structured tool_calls field. Treat both as equivalent.
        inline_calls = chat_tools.parse_inline_tool_calls(content)

        if not tool_calls and not inline_calls:
            last_text = content
            break

        # Echo the assistant message into history. If the model used the
        # structured field, preserve it; otherwise append the plain text.
        if tool_calls:
            api_messages.append({
                "role":      "assistant",
                "content":   content,
                "tool_calls": [
                    {
                        "id":   tc.id,
                        "type": "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })
        else:
            api_messages.append({"role": "assistant", "content": content})

        # Execute structured tool calls
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = chat_tools.dispatch(
                name, company_id=company_id, model=model,
                api_key=api_key, arguments=args,
            )

            canonical = chat_tools.resolve_tool_name(name)
            if canonical == "render_chart":
                chart_specs.append(args)

            api_messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "name":         name,
                "content":      chat_tools.serialise_for_llm(result),
            })

        # Execute inline (text-form) tool calls. We feed the result back
        # via a user-role message because the OpenAI 'tool' role requires a
        # preceding tool_call_id, which inline calls don't have.
        for canonical, args in inline_calls:
            result = chat_tools.dispatch(
                canonical, company_id=company_id, model=model,
                api_key=api_key, arguments=args,
            )
            if canonical == "render_chart":
                chart_specs.append(args)
            api_messages.append({
                "role":    "user",
                "content": (f"[tool result: {canonical}]\n"
                            f"{chat_tools.serialise_for_llm(result)}"),
            })
        # loop continues — let the model see tool results and either call
        # more tools or return a final answer
    else:
        last_text = (last_text or
                     "_(reached tool-call budget; returning partial reasoning)_")

    return last_text or "_(empty reply)_", chart_specs


def _coerce_axis(val) -> list:
    """
    Coerce a chart axis value coming from the LLM into a plain Python list.
    Accepts: list, tuple, dict (uses keys), scalar (wraps in [val]), None.
    """
    if val is None:
        return []
    if isinstance(val, dict):
        # LLMs often pass {label: value} maps; caller decides which side to use.
        return list(val.keys())
    if isinstance(val, (list, tuple)):
        return list(val)
    # Scalar string / number — wrap so DataFrame doesn't choke on "all scalar".
    return [val]


def _render_chart_spec(spec: dict):
    """Render a chart from the `render_chart` tool spec inline in the chat."""
    kind  = (spec.get("kind") or "bar").lower()
    title = spec.get("title") or ""
    xl    = spec.get("x_label") or ""
    yl    = spec.get("y_label") or ""

    raw_x = spec.get("x")
    raw_y = spec.get("y")

    # Accept {"x": {"A": 1, "B": 2}} as a convenience encoding too.
    if isinstance(raw_x, dict) and raw_y is None:
        x = list(raw_x.keys())
        y = list(raw_x.values())
    else:
        x = _coerce_axis(raw_x)
        y = _coerce_axis(raw_y)

    if not x or not y:
        st.caption(f"⚠️ Chart skipped — missing x or y data ({title or kind})")
        return
    if len(x) != len(y):
        # Truncate to common length rather than dropping the chart entirely.
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        if n == 0:
            st.caption(f"⚠️ Chart skipped (mismatched x/y, n=0): {title}")
            return

    x_col = xl or "x"
    y_col = yl or "y"
    try:
        df = pd.DataFrame({x_col: x, y_col: y})
        if kind == "pie":
            fig = px.pie(df, names=x_col, values=y_col, title=title)
        elif kind == "scatter":
            fig = px.scatter(df, x=x_col, y=y_col, title=title)
        elif kind == "line":
            fig = px.line(df, x=x_col, y=y_col, title=title, markers=True)
        else:
            fig = px.bar(df, x=x_col, y=y_col, title=title)
        fig.update_layout(height=320, margin=dict(t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.caption(f"⚠️ Chart render failed ({type(exc).__name__}): {exc}")


# ===========================================================================
# Structured-tab helpers (lifted from views/inventory_manager.py)
# ===========================================================================

def _load_runs(company_id: int) -> list[dict]:
    session = SessionLocal()
    try:
        runs = (session.query(AgentRun)
                .filter(AgentRun.company_id == company_id)
                .order_by(AgentRun.run_at.desc())
                .limit(50).all())
        return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in runs]
    except Exception as exc:
        st.error(f"Could not load agent runs: `{type(exc).__name__}: {exc}`. "
                 "The `agent_runs` table schema may be out of date — try restarting the app.")
        return []
    finally:
        session.close()


def _load_signals(company_id: int, run_id: int) -> list[dict]:
    session = SessionLocal()
    try:
        sigs = (session.query(AgentSignal)
                .filter(AgentSignal.company_id == company_id,
                        AgentSignal.run_id     == run_id)
                .order_by(AgentSignal.analysis_category, AgentSignal.id)
                .all())
        return [{c.name: getattr(s, c.name) for c in s.__table__.columns} for s in sigs]
    except Exception as exc:
        st.caption(f"⚠️ Could not load signals for run {run_id}: `{type(exc).__name__}: {exc}`")
        return []
    finally:
        session.close()


def _mark_actioned(signal_id: int, company_id: int, user_id: int):
    session = SessionLocal()
    try:
        sig = (session.query(AgentSignal)
               .filter(AgentSignal.id == signal_id,
                       AgentSignal.company_id == company_id)
               .first())
        if sig:
            sig.is_actioned = True
            sig.actioned_at = datetime.utcnow()
            sig.actioned_by = user_id
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _is_run_in_progress(company_id: int) -> bool:
    """
    Check if there's a running AgentRun. We deliberately select only the `id`
    column so the query survives even when legacy databases miss some of the
    newer columns defined on the AgentRun model (the SELECT list is minimal).
    """
    session = SessionLocal()
    try:
        row = (session.query(AgentRun.id)
                      .filter(AgentRun.company_id == company_id,
                              AgentRun.status     == "running")
                      .first())
        return row is not None
    except Exception as exc:
        st.warning(f"Could not check for running analyses: `{type(exc).__name__}: {exc}`")
        return False
    finally:
        session.close()


def _show_run_analysis_summary(run: dict):
    planned_raw = run.get("analyses_planned") or "[]"
    done_raw    = run.get("analyses_done")    or "[]"
    try:
        planned = json.loads(planned_raw) if isinstance(planned_raw, str) else planned_raw
        done    = json.loads(done_raw)    if isinstance(done_raw, str) else done_raw
    except (json.JSONDecodeError, TypeError):
        planned, done = [], []

    if not planned:
        return

    st.markdown("**Analyses run:**")
    badges = []
    for key in planned:
        meta  = _CAT_META.get(key, {})
        icon  = _CAT_ICON.get(key, "🔎")
        label = meta.get("label", key)
        if key in done:
            badges.append(f"<span style='background:#0D3A1F;color:#4ADE80;border:1px solid #166534;"
                          f"border-radius:4px;padding:2px 8px;font-size:0.72rem;margin:2px'>"
                          f"{icon} {label} ✓</span>")
        else:
            badges.append(f"<span style='background:#1A1A2E;color:#6B7280;border:1px solid #374151;"
                          f"border-radius:4px;padding:2px 8px;font-size:0.72rem;margin:2px'>"
                          f"{icon} {label}</span>")
    st.markdown("<div style='line-height:2.2'>" + " ".join(badges) + "</div>",
                unsafe_allow_html=True)
    st.markdown("")


def _execute_run(company_id: int, user_id: int, model: str, api_key: str):
    from agents.inventory_agent import run_inventory_agent

    progress_state = {c["key"]: {"status": "pending", "n": 0}
                      for c in ANALYSIS_CATEGORIES}
    progress_placeholder = st.empty()

    def _render_progress():
        rows = []
        for cat in ANALYSIS_CATEGORIES:
            key  = cat["key"]
            s    = progress_state[key]
            icon = _CAT_ICON.get(key, "🔎")
            if s["status"] == "pending":
                status_str, n_str = "⬜ Waiting", ""
            elif s["status"] == "running":
                status_str, n_str = "⏳ Running…", ""
            elif s["status"] == "done":
                status_str = "✅ Done"
                n_str = f"{s['n']} signal{'s' if s['n'] != 1 else ''}"
            else:
                status_str = "❌ Error"
                n_str = f"{s['n']} signal{'s' if s['n'] != 1 else ''}"
            rows.append({"Analysis": f"{icon} {cat['label']}",
                         "Status":   status_str,
                         "Signals":  n_str})
        with progress_placeholder.container():
            st.markdown(f"**Running {model.split('/')[-1]}** — "
                        f"{len(ANALYSIS_CATEGORIES)} analyses queued")
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                         hide_index=True, height=320)

    def _progress_callback(category_key: str, status: str, n_signals: int):
        progress_state[category_key]["status"] = status
        progress_state[category_key]["n"]      = n_signals
        _render_progress()

    _render_progress()

    try:
        run_dict, _signals = run_inventory_agent(
            company_id=company_id, user_id=user_id, model=model, api_key=api_key,
            progress_callback=_progress_callback,
        )
        dur = run_dict.get("duration_seconds", 0)
        n   = run_dict.get("signals_generated", 0)
        progress_placeholder.success(
            f"✅ All {len(ANALYSIS_CATEGORIES)} analyses complete — "
            f"**{n} signals** generated in {dur:.1f}s."
        )
    except Exception as exc:
        progress_placeholder.error(f"❌ Unexpected error: {exc}")

    st.rerun()


def _show_charts(signals: list[dict], runs: list[dict]):
    col_a, col_b, col_c = st.columns(3)

    with col_a:
        sev_counts: dict = {}
        for s in signals:
            sev_counts[s["severity"]] = sev_counts.get(s["severity"], 0) + 1
        df_sev = pd.DataFrame([
            {"severity": k, "count": v, "color": _SEV_COLOR.get(k, "#999")}
            for k, v in sorted(sev_counts.items(),
                               key=lambda x: _SEV_ORDER.get(x[0], 9))
        ])
        if not df_sev.empty:
            fig = px.bar(
                df_sev, x="severity", y="count", color="severity",
                color_discrete_map={r["severity"]: r["color"] for _, r in df_sev.iterrows()},
                labels={"severity": "", "count": "Signals"},
                title="Signals by Severity", height=280,
            )
            fig.update_layout(showlegend=False, margin=dict(t=40, b=20))
            st.plotly_chart(fig, use_container_width=True)

    with col_b:
        cat_counts: dict = {}
        for s in signals:
            key  = s.get("analysis_category", "")
            meta = _CAT_META.get(key, {})
            lbl  = f"{_CAT_ICON.get(key, '🔎')} {meta.get('label', key)}"
            cat_counts[lbl] = cat_counts.get(lbl, 0) + 1
        df_cat = pd.DataFrame(
            [{"category": k, "count": v}
             for k, v in sorted(cat_counts.items(), key=lambda x: -x[1])]
        )
        if not df_cat.empty:
            fig2 = px.bar(df_cat, x="count", y="category", orientation="h",
                          labels={"count": "Signals", "category": ""},
                          title="Signals by Analysis", height=280)
            fig2.update_layout(margin=dict(t=40, b=20),
                               yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig2, use_container_width=True)

    with col_c:
        completed = [r for r in runs if r["status"] == "completed"]
        if len(completed) >= 2:
            df_hist = pd.DataFrame([{
                "run_at":  r["run_at"].strftime("%m-%d %H:%M"),
                "signals": r["signals_generated"],
            } for r in reversed(completed[-10:])])
            fig3 = px.line(df_hist, x="run_at", y="signals", markers=True,
                           labels={"run_at": "", "signals": "Signals"},
                           title="Signal Trend (last 10 runs)", height=280)
            fig3.update_layout(margin=dict(t=40, b=20))
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.caption("Signal trend available after 2+ completed runs.")


def _show_signals_by_category(signals: list[dict], company_id: int, user_id: int):
    st.subheader("Findings & Recommendations")

    fc1, fc2, fc3 = st.columns([2, 2, 1])
    with fc1:
        all_sevs = sorted({s["severity"] for s in signals},
                          key=lambda x: _SEV_ORDER.get(x, 9))
        sel_sevs = st.multiselect("Severity filter", all_sevs, default=all_sevs,
                                  key="aim_filter_sev")
    with fc2:
        all_cats = list({s.get("analysis_category", "") for s in signals
                         if s.get("analysis_category")})
        all_cats_sorted = [c["key"] for c in ANALYSIS_CATEGORIES if c["key"] in all_cats]
        sel_cats = st.multiselect(
            "Analysis filter", all_cats_sorted, default=all_cats_sorted,
            key="aim_filter_cat",
            format_func=lambda k: f"{_CAT_ICON.get(k, '🔎')} "
                                  f"{_CAT_META.get(k, {}).get('label', k)}",
        )
    with fc3:
        show_actioned = st.checkbox("Show actioned", value=False, key="aim_show_actioned")

    by_cat: dict = {}
    for s in signals:
        cat_key = s.get("analysis_category", "")
        if cat_key not in sel_cats:
            continue
        if s["severity"] not in sel_sevs:
            continue
        if not show_actioned and s["is_actioned"]:
            continue
        by_cat.setdefault(cat_key, []).append(s)

    if not by_cat:
        st.info("No signals match the current filters.")
        return

    total_filtered = sum(len(v) for v in by_cat.values())
    st.caption(f"Showing {total_filtered} signals across {len(by_cat)} analyses")

    for cat in ANALYSIS_CATEGORIES:
        key      = cat["key"]
        cat_sigs = by_cat.get(key, [])
        if not cat_sigs:
            continue

        crit  = sum(1 for s in cat_sigs if s["severity"] == "critical")
        high  = sum(1 for s in cat_sigs if s["severity"] == "high")
        med   = sum(1 for s in cat_sigs if s["severity"] == "medium")
        worst = "critical" if crit else ("high" if high else ("medium" if med else "low"))

        summary_parts = []
        if crit: summary_parts.append(f"🔴 {crit}")
        if high: summary_parts.append(f"🟠 {high}")
        if med:  summary_parts.append(f"🟡 {med}")
        low_ct  = sum(1 for s in cat_sigs if s["severity"] == "low")
        info_ct = sum(1 for s in cat_sigs if s["severity"] == "info")
        if low_ct:  summary_parts.append(f"🔵 {low_ct}")
        if info_ct: summary_parts.append(f"⚪ {info_ct}")

        header = (f"{_CAT_ICON.get(key, '🔎')} {cat['label']} — "
                  f"{len(cat_sigs)} signal{'s' if len(cat_sigs) != 1 else ''} "
                  f"{' '.join(summary_parts)}")
        with st.expander(header, expanded=(worst in ("critical", "high"))):
            _show_category_signals(cat_sigs, key, company_id, user_id)


def _show_category_signals(signals: list[dict], category_key: str,
                            company_id: int, user_id: int):
    rows = []
    for s in signals:
        rows.append({
            "id":       s["id"],
            "Severity": _SEV_BADGE.get(s["severity"], s["severity"]),
            "Type":     _TYPE_LABEL.get(s["signal_type"], s["signal_type"]),
            "Part #":   s["part_number"],
            "Title":    s["title"],
            "Metric":   (f"{s['metric_name']} = {s['metric_value']:.2f}"
                         if s["metric_value"] is not None and s["metric_name"] else ""),
            "Done":     "✅" if s["is_actioned"] else "",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df.drop(columns=["id"]), use_container_width=True, hide_index=True,
                 height=min(40 + 35 * len(df), 380))

    sel_idx = st.selectbox(
        "View signal detail", options=range(len(signals)),
        format_func=lambda i: f"{_SEV_BADGE.get(signals[i]['severity'], '')}  "
                              f"{signals[i]['title'][:80]}",
        key=f"aim_detail_sel_{category_key}",
        label_visibility="collapsed",
    )
    if sel_idx is not None and sel_idx < len(signals):
        _show_signal_detail(signals[sel_idx], company_id, user_id)


def _show_signal_detail(sig: dict, company_id: int, user_id: int):
    sev_color = _SEV_COLOR.get(sig["severity"], "#999")
    st.markdown(
        f"""<div style="border-left:4px solid {sev_color};
                        padding:0.75rem 1rem;
                        background:var(--bg-elevated,#112240);
                        border-radius:6px;margin-top:0.5rem">
            <div style="font-size:0.65rem;font-weight:700;letter-spacing:0.12em;
                        text-transform:uppercase;color:{sev_color}">
                {_SEV_BADGE.get(sig['severity'], sig['severity'])} &nbsp;·&nbsp;
                {_TYPE_LABEL.get(sig['signal_type'], sig['signal_type'])} &nbsp;·&nbsp;
                {sig['part_number']}
            </div>
            <div style="font-size:1.0rem;font-weight:600;margin:0.4rem 0 0.2rem;color:#E8F0FF">
                {sig['title']}
            </div>
        </div>""", unsafe_allow_html=True,
    )

    d_col, r_col = st.columns([1, 1])
    with d_col:
        st.markdown("**Analysis**")
        st.markdown(sig["detail"] or "_No detail provided._")
        if sig["metric_name"] and sig["metric_value"] is not None:
            st.caption(
                f"📊 `{sig['metric_name']}` = **{sig['metric_value']:.2f}**"
                + (f"  (threshold: {sig['metric_threshold']:.2f})"
                   if sig["metric_threshold"] is not None else "")
            )
    with r_col:
        st.markdown("**Recommended Action**")
        st.markdown(sig["recommendation"] or "_No recommendation provided._")
        if not sig["is_actioned"]:
            if st.button("✅ Mark as Actioned",
                         key=f"aim_action_{sig['id']}_{sig.get('analysis_category', '')}",
                         type="secondary"):
                _mark_actioned(sig["id"], company_id, user_id)
                st.success("Marked as actioned.")
                st.rerun()
        else:
            at = sig["actioned_at"]
            ts = at.strftime("%Y-%m-%d %H:%M") if at else "unknown time"
            st.success(f"Actioned on {ts}")
