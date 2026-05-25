"""Skill module public interfaces — stable service and repository contracts.

Defines the SkillServicePort that callers (Agent, TUI) depend on.
Matches the concrete SkillService implementation so the port no longer drifts.
"""

from __future__ import annotations

from typing import Any, Protocol


class SkillServicePort(Protocol):
    """Skill lifecycle — retrieval, reflection, CRUD, merging, and feedback.

    The Agent depends only on this interface, never on SkillStore internals.
    Matches the actual SkillService / SkillManager public API.
    """

    # ── retrieval ────────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3) -> list[Any]: ...

    def inject_skills_prompt(self, query: str) -> str: ...

    def get_skill_by_name(self, name: str) -> Any | None: ...

    def get_skill(self, skill_id: str) -> Any | None: ...

    # ── reflection ───────────────────────────────────────────────────────

    async def reflect(self, recent_messages: list, llm: Any, episodes: list[dict] | None = None) -> dict: ...

    # ── CRUD ─────────────────────────────────────────────────────────────

    def list_all(self) -> list[Any]: ...

    def remove_skill(self, skill_id: str) -> None: ...

    def deprecate_skill(self, skill_id: str) -> None: ...

    # ── feedback ────────────────────────────────────────────────────────

    def record_usage(self, skill_id: str, success: bool) -> None: ...

    # ── merge ───────────────────────────────────────────────────────────

    async def merge_skills(self, llm: Any) -> dict: ...


# Legacy alias for backward-compatible references in agent/ports.py
SkillService = SkillServicePort
