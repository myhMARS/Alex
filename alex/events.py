"""Typed notification events — replace bare-dict dispatch between Agent and consumers.

Agent pushes these to a pending queue; the TUI (or any frontend) drains the
queue and renders/dispatches based on the concrete type, not string matching.
Cron stream events reuse the existing StreamEvent type so that regular chat
and cron subscriptions share the same rendering pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillReflectEvent:
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


@dataclass
class SkillReflectErrorEvent:
    """Emitted when skill reflection fails."""
    error: str = ""


@dataclass
class CronJobEvent:
    """Emitted when a cron job finishes a run or updates status."""
    job_id: str = ""
    session_id: str = ""
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


@dataclass
class CronDebugEvent:
    """Debug-level cron message — shown as a transient toast."""
    message: str = ""
