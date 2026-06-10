"""Session persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alex.kernel.bus import Request


@dataclass
class ListSessions(Request[list[Any]]):
    """Request the list of saved sessions."""


@dataclass
class LoadSession(Request[dict[str, Any] | None]):
    """Load a session bundle (messages + cron_history) from disk."""
