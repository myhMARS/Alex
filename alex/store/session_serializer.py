"""Session serializer — convenience re-exports from alex.store.session.

All serialization uses plain dict messages.  This module is kept for
backward compatibility.
"""

from __future__ import annotations

from typing import Any

from alex.store.session import deserialize_message, serialize_message

__all__ = ["deserialize_message", "serialize_message"]


def deserialize_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [deserialize_message(r) for r in records]


def serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [serialize_message(m) for m in messages]
