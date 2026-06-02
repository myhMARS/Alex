"""Skill contracts — retrieval, loading, and reflection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alex.kernel.bus import Command, Event, Request
from alex.kernel.dto.skill import SkillCard


# ── Requests (agent → skill module) ──────────────────────────────────────────

@dataclass
class RetrieveSkills(Request[list[SkillCard]]):
    """Search for skills matching *query*.

    Returns ``list[SkillCard]``.
    """
    query: str = ""
    top_k: int = 3


@dataclass
class LoadSkill(Request[SkillCard]):
    """Load a skill's full instruction by name.

    Returns ``SkillCard`` (with instruction populated) or raises.
    """
    skill_name: str = ""


# ── Commands ─────────────────────────────────────────────────────────────────

@dataclass
class ReflectSkills(Command):
    """Trigger skill reflection (feedback-based evolution).

    This is fire-and-forget — results come via SkillsReflected event.
    """
    pass


# ── Events ───────────────────────────────────────────────────────────────────

@dataclass
class SkillsReflected(Event):
    """Published after skill reflection completes."""
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
class SkillLoaded(Event):
    """Published when a skill is loaded during a turn (UI notification)."""
    skill_name: str = ""
    skill_pattern: str = ""


@dataclass
class SkillReflectError(Event):
    """Published when skill reflection fails."""
    error: str = ""


# ── Additional management requests ───────────────────────────────────────────

@dataclass
class ListSkills(Request[list[dict[str, Any]]]):
    """List all skills (optionally filtered by status)."""
    include_deprecated: bool = False


@dataclass
class DeleteSkill(Request[str | None]):
    """Delete a skill by id or name prefix.  Returns name or None."""
    target: str = ""


@dataclass
class DeprecateSkill(Request[str | None]):
    """Deprecate a skill by id or name prefix.  Returns name or None."""
    target: str = ""


@dataclass
class GetSkillName(Request[str]):
    """Get the display name for a skill id.  Returns the name or the id."""
    skill_id: str = ""


@dataclass
class RecordSkillUsage(Request[None]):
    """Record skill usage feedback."""
    skill_id: str = ""
    positive: bool = True


@dataclass
class MergeSkills(Request[dict]):
    """Trigger skill merge.  Returns merge result dict."""
    pass
