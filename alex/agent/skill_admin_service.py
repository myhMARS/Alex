"""SkillAdminAppService — skill management application service.

Extracted from Agent's skill-related methods.  Handles listing,
deletion, deprecation, merging, and load-tool creation.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from alex.skill.models import SkillManager


class SkillAdminAppService:
    """Application service for skill CRUD and management."""

    def __init__(self, skill_manager: SkillManager, llm: BaseChatModel) -> None:
        self._skills = skill_manager
        self._llm = llm

    def list_skills(self) -> list[dict]:
        all_skills = self._skills.list_all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "use_count": s.use_count,
                "success_count": s.success_count,
                "failure_count": s.failure_count,
                "pattern": s.pattern,
                "instruction": s.instruction,
                "tags": s.tags,
            }
            for s in all_skills
        ]

    def _find_skill(self, target: str):
        for s in self._skills.list_all():
            if s.id.startswith(target) or s.name.lower() == target.lower():
                return s
        return None

    def delete_skill(self, target: str) -> str | None:
        found = self._find_skill(target)
        if found:
            self._skills.remove_skill(found.id)
            return found.name
        return None

    def deprecate_skill(self, target: str) -> str | None:
        found = self._find_skill(target)
        if found:
            self._skills.deprecate_skill(found.id)
            return found.name
        return None

    async def merge_skills(self) -> dict:
        return await self._skills.merge_skills(self._llm)

    async def load_skill(self, skill_name: str) -> str:
        skill = self._skills.get_skill_by_name(skill_name)
        if skill:
            return f"[Skill: {skill.name}]\n\nWhen to apply: {skill.pattern}\n\nExecution methodology:\n{skill.instruction}"
        names = [s.name for s in self._skills.list_all() if s.status != "DEPRECATED"]
        return f"Skill '{skill_name}' not found. Available: {', '.join(names)}"

    def get_skill_name(self, skill_id: str) -> str:
        skill = self._skills.get_skill(skill_id)
        return skill.name if skill else skill_id
