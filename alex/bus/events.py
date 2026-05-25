"""Unified typed event definitions — single source of truth for all cross-module events.

All events inherit from Event → Command / DomainEvent / UIEvent.
Standard metadata (event_id, session_id, turn_id, source, ts) is inherited
from Event so every handler can rely on it.
"""

from __future__ import annotations

import uuid
import time as _time
from dataclasses import dataclass, field
from typing import Any


# ── Base ──────────────────────────────────────────────────────────────────────

@dataclass
class Event:
    """Base for every event in the system."""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    turn_id: str = ""
    source: str = ""   # module that emitted the event
    ts: float = field(default_factory=_time.time)


@dataclass
class Command(Event):
    """Marker — a request to perform an action."""


@dataclass
class DomainEvent(Event):
    """Marker — a state change that other modules may react to."""


@dataclass
class UIEvent(Event):
    """Marker — a signal destined for the frontend."""


# ── Commands ──────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class UserTurnRequested(Command):
    user_text: str = ""


@dataclass(slots=True)
class CronTurnRequested(Command):
    trigger: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResumeSessionRequested(Command):
    pass  # session_id carries the target


@dataclass(slots=True)
class FeedbackSubmitted(Command):
    positive: bool = True


@dataclass(slots=True)
class ClearSessionRequested(Command):
    pass


# ── Domain events ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class TurnStarted(DomainEvent):
    kind: str = "user"  # "user" | "cron"


@dataclass(slots=True)
class SkillMatched(DomainEvent):
    skill_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolExecutionRequested(DomainEvent):
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""


@dataclass(slots=True)
class ToolExecutionCompleted(DomainEvent):
    tool_name: str = ""
    run_id: str = ""
    output: Any = None
    error: str = ""


@dataclass(slots=True)
class TurnCompleted(DomainEvent):
    kind: str = "user"
    messages: list[Any] = field(default_factory=list)
    content: str = ""
    thinking: str = ""


@dataclass(slots=True)
class TurnFailed(DomainEvent):
    error: str = ""


@dataclass(slots=True)
class CronScheduled(DomainEvent):
    job_id: str = ""
    name: str = ""


@dataclass(slots=True)
class CronTriggered(DomainEvent):
    job_id: str = ""
    name: str = ""


@dataclass(slots=True)
class SkillReflected(DomainEvent):
    new: int = 0
    updated: int = 0
    deprecated: int = 0


@dataclass(slots=True)
class CronBatch(DomainEvent):
    """Carries the cron turn's message batch to the bus."""
    stream_id: str = ""
    messages: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class CronDone(DomainEvent):
    """Sentinel that ends a cron stream."""
    stream_id: str = ""
    content: str = ""
    thinking: str = ""


@dataclass(slots=True)
class CronError(DomainEvent):
    """Sentinel for a failed cron stream."""
    stream_id: str = ""
    error: str = ""


# ── UI events ─────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ThinkingUpdated(UIEvent):
    delta: str = ""
    stream_id: str = ""


@dataclass(slots=True)
class TokenEmitted(UIEvent):
    delta: str = ""
    stream_id: str = ""


@dataclass(slots=True)
class ToolStarted(UIEvent):
    tool_id: str = ""
    tool_name: str = ""
    tool_input: Any = None
    is_cron: bool = False
    stream_id: str = ""


@dataclass(slots=True)
class ToolFinished(UIEvent):
    tool_id: str = ""
    output: Any = None
    is_cron: bool = False
    stream_id: str = ""


@dataclass(slots=True)
class ToastRequested(UIEvent):
    message: str = ""
    level: str = "info"  # info | warning | error


@dataclass(slots=True)
class SkillLoaded(UIEvent):
    skill_name: str = ""
    skill_pattern: str = ""


@dataclass(slots=True)
class SessionRestored(UIEvent):
    messages: list[Any] = field(default_factory=list)
    message_count: int = 0


# ── Subsystem-specific domain events ──────────────────────────────────────────

@dataclass(slots=True)
class SkillReflectEvent(DomainEvent):
    """Emitted after skill reflection completes successfully."""
    new: int = 0
    updated: int = 0
    deprecated: int = 0
    names: list[str] = field(default_factory=list)
    updated_names: list[str] = field(default_factory=list)

    @property
    def toast(self) -> str:
        parts = []
        if self.new:
            parts.append(f"{self.new} new: {', '.join(self.names)}")
        if self.updated:
            if self.updated_names:
                parts.append(f"{self.updated} updated: {', '.join(self.updated_names)}")
            else:
                parts.append(f"{self.updated} updated")
        if self.deprecated:
            parts.append(f"{self.deprecated} deprecated")
        return "Skills refined — " + "; ".join(parts) if parts else "Skills refined"


@dataclass(slots=True)
class SkillReflectErrorEvent(DomainEvent):
    """Emitted when skill reflection fails."""
    error: str = ""


@dataclass(slots=True)
class CronJobEvent(DomainEvent):
    """Emitted when a cron job finishes a run or updates status."""
    job_id: str = ""
    name: str = ""
    status: str = ""         # SUCCESS / FAILED / RUNNING / CANCELLED
    subscribe: bool = False  # True when the job has a subscribed LLM reply
    action: str = ""
    params: dict = field(default_factory=dict)
    runs_done: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""         # tool output on success
    error: str = ""          # error message on failure
    tool_call_id: str = ""


@dataclass(slots=True)
class CronDebugEvent(DomainEvent):
    """Debug-level cron message — shown as a transient toast."""
    message: str = ""


@dataclass(slots=True)
class CronRecordPersist(DomainEvent):
    """Cross-session cron record — published by TUI, consumed by store adapter."""
    record: dict = field(default_factory=dict)
