"""Session persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alex.kernel.bus import Event, Request


@dataclass
class ListSessions(Request[list[Any]]):
    """Request the list of saved sessions."""


@dataclass
class LoadSession(Request[list[dict[str, Any]] | None]):
    """Load a session's messages from disk."""


@dataclass
class SaveSession(Event):
    """Published by store when TurnCompleted fires — persists the session."""
    messages: list[Any] = field(default_factory=list)


@dataclass
class SessionRestored(Event):
    """Published after a session's history has been restored into memory."""
    messages: list[Any] = field(default_factory=list)
    message_count: int = 0
