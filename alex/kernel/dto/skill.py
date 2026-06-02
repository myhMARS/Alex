"""SkillCard — neutral skill description for cross-module transfer.

The real ``Skill`` model (with its full lifecycle methods) stays inside
the ``skill`` module.  ``SkillCard`` is the minimal view that other modules
need for matching, display, and tool registration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillCard:
    """A skill's public description — enough for matching and display."""

    id: str = ""
    name: str = ""
    pattern: str = ""
    summary: str = ""
    instruction: str = ""  # full instruction text for load_skill
    tags: list[str] = field(default_factory=list)
    version: int = 1
    status: str = "ACTIVE"  # ACTIVE | DEPRECATED
    metadata: dict[str, Any] = field(default_factory=dict)
