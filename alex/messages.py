"""Message types — plain dict-based messages replacing langchain_core.messages.

All messages use a simple ``{"role": ..., "content": ...}`` dict format
compatible with the OpenAI Chat Completions API.  This removes the
langchain_core.messages dependency entirely.

Role values:
    - ``"system"`` — system prompt
    - ``"user"``   — human message or tool result
    - ``"assistant"`` — AI response (optional tool_calls / reasoning_content)
    - ``"tool"``   — tool execution result (requires tool_call_id)
"""

from __future__ import annotations

from typing import Any

# ── message constructors ──────────────────────────────────────────────────


def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def assistant_message(
    content: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str = "",
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    return msg


def tool_message(content: str, *, tool_call_id: str = "") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "tool", "content": content}
    if tool_call_id:
        msg["tool_call_id"] = tool_call_id
    return msg


# ── message inspectors ────────────────────────────────────────────────────


def is_user(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "user"


def is_assistant(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "assistant"


def is_tool(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "tool"


def is_system(msg: dict[str, Any]) -> bool:
    return msg.get("role") == "system"


def get_reasoning(msg: dict[str, Any]) -> str:
    return str(msg.get("reasoning_content") or "")


def has_tool_calls(msg: dict[str, Any]) -> bool:
    return bool(msg.get("tool_calls"))


def get_tool_call_id(msg: dict[str, Any]) -> str:
    return str(msg.get("tool_call_id") or "")


# ── serialization helpers ─────────────────────────────────────────────────

# Map role strings to constructors for deserialization.
_ROLE_CONSTRUCTORS: dict[str, type] = {}

# We use a simple dict-based serialization that's already JSON-compatible.
# No custom encoder needed — all values are str | list | dict | None.


def serialize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of *msg* (messages are already plain dicts)."""
    return dict(msg)


def deserialize_message(record: dict[str, Any]) -> dict[str, Any]:
    """Return *record* as a message dict (already plain dicts)."""
    return dict(record)


# ── helper for external tool call format ──────────────────────────────────


def openai_tool_call_to_dict(tc: Any, tool_id: str = "") -> dict[str, Any]:
    """Convert an OpenAI ToolCall (or compatible) object to a plain dict.

    Handles both pydantic ToolCall objects (from the SDK) and plain dicts.
    """
    if isinstance(tc, dict):
        fn = tc.get("function", {})
        return {
            "id": tc.get("id", tool_id),
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
            },
        }
    # Pydantic model from OpenAI SDK
    fn = getattr(tc, "function", None)
    return {
        "id": getattr(tc, "id", tool_id) or tool_id,
        "type": "function",
        "function": {
            "name": getattr(fn, "name", "") if fn else "",
            "arguments": getattr(fn, "arguments", "") if fn else "",
        },
    }
