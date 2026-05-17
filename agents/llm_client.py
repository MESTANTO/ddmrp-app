"""
Shared LLM client for NVIDIA NIM (OpenAI-compatible).

Both the inventory agent (focused per-skill analyses) and the chat
interface use this module so the endpoint, key resolution, and known
model list are centralised.
"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st
from openai import OpenAI


NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"

# The app is pinned to Kimi K2 — a 1T-parameter MoE model hosted on
# NVIDIA NIM. It is excellent at long-context inventory reasoning and at
# emitting structured tool calls. Other slugs remain commented out for
# reference / future experiments only.
DEFAULT_MODEL = "moonshotai/kimi-k2-instruct-0905"

KNOWN_MODELS = [
    "moonshotai/kimi-k2-instruct-0905",
]

# Kimi K2 on NVIDIA NIM does NOT expose OpenAI's structured `tools`
# parameter — it emits tool calls inline as <tool_call>{...}</tool_call>
# text instead. The chat loop already parses that format, so we leave
# this set empty: the API is called WITHOUT tools=[...] and we rely on
# the inline-tool-call parser to dispatch invocations.
TOOL_CAPABLE_MODELS: set[str] = set()


def get_api_key() -> Optional[str]:
    """Return the NVIDIA API key from Streamlit secrets, or None."""
    try:
        return st.secrets["NVIDIA_API_KEY"]
    except Exception:
        return None


def get_client(timeout: float = 300.0) -> Optional[OpenAI]:
    """
    Build an OpenAI client pointing at the NVIDIA NIM endpoint.

    Returns None if the API key is not configured.
    """
    key = get_api_key()
    if not key:
        return None
    return OpenAI(base_url=NVIDIA_BASE, api_key=key, timeout=timeout)


def chat_completion(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    tools: Optional[list[dict]] = None,
    tool_choice: str = "auto",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = 300.0,
) -> Any:
    """
    Thin wrapper around `client.chat.completions.create`.

    Raises if the API key is missing or the call fails — callers handle.
    """
    client = get_client(timeout=timeout)
    if client is None:
        raise RuntimeError("NVIDIA_API_KEY not configured in Streamlit secrets.")

    kwargs: dict = {
        "model":       model.strip()[:100],
        "messages":    messages,
        "temperature": temperature,
        "max_tokens":  max_tokens,
        "stream":      False,
    }
    if tools:
        kwargs["tools"]       = tools
        kwargs["tool_choice"] = tool_choice

    return client.chat.completions.create(**kwargs)
