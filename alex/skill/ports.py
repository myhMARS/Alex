"""Skill module public interfaces."""

from __future__ import annotations

from typing import Any, Protocol


class SkillService(Protocol):
    """Skill lifecycle — matching, loading, reflection, and feedback.

    The Agent depends only on this interface, never on SkillRepository
    or SkillStore internals.
    """

    async def build_prompt_section(self, query: str) -> str: ...

    async def load(self, name: str) -> str | None: ...

    async def reflect(self, episode: dict[str, Any]) -> dict[str, Any]: ...

    async def record_feedback(self, turn_id: str, positive: bool) -> None: ...

    async def list_skills(self) -> list[dict[str, Any]]: ...
