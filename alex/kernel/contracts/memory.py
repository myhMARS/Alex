"""Memory contracts — context retrieval and mutation (all request/reply)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alex.kernel.bus import Request


@dataclass
class GetContext(Request[list[dict[str, Any]]]):
    """Request the full conversation history for a session."""
    query: str | None = None


@dataclass
class AppendMessages(Request[None]):
    """Append messages to the conversation history."""
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReplaceMemory(Request[None]):
    """Atomically clear + append (used for session restore)."""
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ClearMemory(Request[None]):
    """Clear all messages for a session."""
    pass
