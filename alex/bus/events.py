"""Event type re-exports — single source of truth is ``alex.kernel.contracts``.

This module exists for **backward compatibility**.  New code should import
directly from ``alex.kernel.contracts`` or ``alex.kernel.bus``.

All event types ultimately inherit from ``alex.kernel.bus.Event``
(or ``Command`` / ``Request``), so the bus dispatch works uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Base types (kernel is canonical) ──────────────────────────────────────────
from alex.kernel.bus import Event, Command

# ── Chat contracts ────────────────────────────────────────────────────────────
from alex.kernel.contracts.chat import (
    FeedbackSubmitted,
    ThinkingUpdated,
    TokenEmitted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    UserTurnRequested,
)

# ── Tool contracts ────────────────────────────────────────────────────────────
from alex.kernel.contracts.tools import (
    ToolFinished,
    ToolsProvided,
    ToolStarted,
)

# ── Skill contracts ───────────────────────────────────────────────────────────
from alex.kernel.contracts.skills import (
    SkillLoaded,
    SkillReflectError as SkillReflectErrorEvent,
    SkillsReflected as SkillReflectEvent,
)

# ── Session contracts ─────────────────────────────────────────────────────────
from alex.kernel.contracts.session import (
    SessionRestored,
)

# ── Cron contracts ────────────────────────────────────────────────────────────
from alex.kernel.contracts.cron import (
    CronJobEvent,
    CronTurnRequested,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Old-only event types (no kernel equivalent — keep here for now)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class DomainEvent(Event):
    """Marker — a state change that other modules may react to."""


@dataclass
class UIEvent(Event):
    """Marker — a signal destined for the frontend."""


# ── Commands (no kernel equivalent yet) ───────────────────────────────────────

@dataclass
class ResumeSessionRequested(Command):
    """User requested to resume a saved session."""
    pass  # session_id carries the target


@dataclass
class ClearSessionRequested(Command):
    """User requested to clear the current session."""


# ── Domain events (internal / streaming — no kernel equivalent) ───────────────

@dataclass
class SkillMatched(DomainEvent):
    """A skill was matched for the current turn (internal to agent)."""
    skill_ids: list[str] = field(default_factory=list)


@dataclass
class ToolExecutionRequested(DomainEvent):
    """A tool execution was requested (internal to agent)."""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""


@dataclass
class ToolExecutionCompleted(DomainEvent):
    """A tool execution completed (internal to agent)."""
    tool_name: str = ""
    run_id: str = ""
    output: Any = None
    error: str = ""


@dataclass
class CronScheduled(DomainEvent):
    """A cron job was scheduled."""
    job_id: str = ""
    name: str = ""


@dataclass
class CronTriggered(DomainEvent):
    """A cron job was triggered."""
    job_id: str = ""
    name: str = ""


@dataclass
class SkillReflected(DomainEvent):
    """Skill reflection completed (older name — prefer SkillsReflected/SkillReflectEvent)."""
    new: int = 0
    updated: int = 0
    deprecated: int = 0


@dataclass
class CronBatch(DomainEvent):
    """Carries the cron turn's message batch to the bus."""
    stream_id: str = ""
    messages: list[Any] = field(default_factory=list)


@dataclass
class CronDone(DomainEvent):
    """Sentinel that ends a cron stream."""
    stream_id: str = ""
    content: str = ""
    thinking: str = ""


@dataclass
class CronError(DomainEvent):
    """Sentinel for a failed cron stream."""
    stream_id: str = ""
    error: str = ""


# ── UI events ─────────────────────────────────────────────────────────────────

@dataclass
class ToastRequested(UIEvent):
    """Request a toast notification in the TUI."""
    message: str = ""
    level: str = "info"  # info | warning | error


@dataclass
class CronDebugEvent(DomainEvent):
    """Debug-level cron message — shown as a transient toast."""
    message: str = ""


@dataclass
class CronRecordPersist(DomainEvent):
    """Cross-session cron record — published by TUI, consumed by store adapter."""
    record: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Re-export canonical names for code that imports from here
# ═══════════════════════════════════════════════════════════════════════════════

# These are imported above from kernel and re-exported locally.
# ``from alex.bus.events import TurnStarted`` now gets the kernel's TurnStarted.

__all__ = [
    # Base
    "Event",
    "Command",
    "DomainEvent",
    "UIEvent",
    # Chat
    "ThinkingUpdated",
    "TokenEmitted",
    "TurnStarted",
    "TurnCompleted",
    "TurnFailed",
    "UserTurnRequested",
    # Tools
    "ToolStarted",
    "ToolFinished",
    "ToolsProvided",
    # Skills
    "SkillLoaded",
    "SkillReflectEvent",
    "SkillReflectErrorEvent",
    "SkillReflected",
    "SkillMatched",
    # Session
    "SessionRestored",
    # Cron
    "CronJobEvent",
    "CronTurnRequested",
    "CronScheduled",
    "CronTriggered",
    "CronBatch",
    "CronDone",
    "CronError",
    "CronDebugEvent",
    "CronRecordPersist",
    # Commands
    "ResumeSessionRequested",
    "FeedbackSubmitted",
    "ClearSessionRequested",
    # Internal domain
    "ToolExecutionRequested",
    "ToolExecutionCompleted",
    # UI
    "ToastRequested",
]
