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

# ── Cron contracts ────────────────────────────────────────────────────────────
from alex.kernel.contracts.cron import (
    CronJobEvent,
    CronTurnRequested,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Base marker types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class DomainEvent(Event):
    """Marker — a state change that other modules may react to."""


@dataclass
class UIEvent(Event):
    """Marker — a signal destined for the frontend."""


# ── Active event types ────────────────────────────────────────────────────────


@dataclass
class CronDebugEvent(DomainEvent):
    """Debug-level cron message — shown as a transient toast."""
    message: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# Re-export canonical names for code that imports from here
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Base
    "Event",
    "Command",
    "DomainEvent",
    "UIEvent",
    # Chat
    "FeedbackSubmitted",
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
    # Cron
    "CronJobEvent",
    "CronTurnRequested",
    "CronDebugEvent",
]
