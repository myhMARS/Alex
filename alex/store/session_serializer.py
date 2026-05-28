"""Session serializer — BaseMessage <-> dict roundtrip for session persistence.

This module exists so that SessionService and other agent-layer code
never import deserialize_message from the store internals directly.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage

from alex.store.session import deserialize_message, serialize_message

__all__ = ["deserialize_message", "serialize_message"]


def deserialize_messages(records: list[dict[str, Any]]) -> list[BaseMessage]:
    return [deserialize_message(r) for r in records]


def serialize_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    return [serialize_message(m) for m in messages]
