"""Chat / conversation contracts — events and commands for the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alex.kernel.bus import Command, Event


# ── Commands (inbound to agent) ──────────────────────────────────────────────

@dataclass
class UserTurnRequested(Command):
    """TUI publishes this when the user submits a message."""
    user_text: str = ""


# ── UI events (agent → TUI, broadcast) ───────────────────────────────────────

@dataclass
class TokenEmitted(Event):
    """Streaming text delta for the chat bubble."""
    delta: str = ""
    stream_id: str = ""


@dataclass
class ThinkingUpdated(Event):
    """Streaming thinking/reasoning delta (DeepSeek mode)."""
    delta: str = ""
    stream_id: str = ""


# ── Domain events ────────────────────────────────────────────────────────────

@dataclass
class TurnStarted(Event):
    """A conversation turn has begun."""
    kind: str = "user"  # "user" | "cron"
    user_input: str = ""


@dataclass
class TurnCompleted(Event):
    """A conversation turn finished successfully."""
    kind: str = "user"
    messages: list[Any] = field(default_factory=list)
    message_batch: list[Any] = field(default_factory=list)
    content: str = ""
    thinking: str = ""
    stream_id: str = ""


@dataclass
class TurnFailed(Event):
    """A conversation turn failed."""
    error: str = ""


@dataclass
class FeedbackSubmitted(Command):
    """User submitted feedback for a turn (triggers skill reflection)."""
    positive: bool = True
