"""SkillService — concrete skill service with constructor injection.

Implements all skill business logic: retrieval, reflection, merging,
CRUD operations, and prompt injection.  Accepts its dependencies
(storage, reflector, retriever, evolution) via the constructor so
callers can inject test doubles or alternate backends.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from alex.llm.base import LLMConfig
from alex.prompts import get_skills_section, render

if TYPE_CHECKING:
    from alex.skill.repository import SkillStore
    from alex.skill.reflector import Reflector
    from alex.skill.matcher import SkillRetriever
    from alex.skill.evolution import EvolutionEngine
    from alex.skill.models import Skill


class SkillService:
    """Concrete skill service — all business logic, no lazy imports."""

    def __init__(
        self,
        store: SkillStore,
        reflector: Reflector,
        retriever: SkillRetriever,
        evolution: EvolutionEngine,
    ) -> None:
        self._store = store
        self._reflector = reflector
        self._retriever = retriever
        self._evolution = evolution

    # ── retrieval ────────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3) -> list[Skill]:
        return self._retriever.retrieve(query, top_k)

    def inject_skills_prompt(self, query: str) -> str:  # noqa: ARG002  # query reserved for future use
        skills = self._store.list_all()
        active = [s for s in skills if s.status != "DEPRECATED"]
        if not active:
            return ""
        return get_skills_section(skills=[
            {"name": s.name, "pattern": s.pattern} for s in active
        ])

    def get_skill_by_name(self, name: str) -> Skill | None:
        name_lower = name.lower()
        for s in self._store.list_all():
            if s.name.lower() == name_lower and s.status != "DEPRECATED":
                return s
        return None

    # ── reflection ───────────────────────────────────────────────────────

    async def reflect(self, recent_messages: list, config: LLMConfig | None = None, episodes: list[dict] | None = None) -> dict:
        result = await self._reflector.reflect(
            recent_messages,
            [s for s in self._store.list_all() if s.status != "DEPRECATED"],
            episodes=episodes or [],
            config=config,
        )

        for skill in result.new_skills:
            self._store.add(skill)

        updated_names: list[str] = []
        for update in result.updated_skills:
            skill_id = update.get("id", "")
            existing = self._store.get(skill_id)
            if existing:
                for k, v in update.items():
                    if k == "id":
                        continue
                    if hasattr(existing, k):
                        setattr(existing, k, v)
                existing.version += 1
                self._store.update(existing)
                updated_names.append(existing.name)

        for skill_id in result.deprecated_ids:
            self._store.deprecate(skill_id)

        self._evolution.evolve(self._store)

        return {
            "new": len(result.new_skills),
            "updated": len(result.updated_skills),
            "deprecated": len(result.deprecated_ids),
            "new_skill_names": [s.name for s in result.new_skills],
            "updated_skill_names": updated_names,
        }

    # ── CRUD ──────────────────────────────────────────────────────────────

    def list_all(self) -> list[Skill]:
        return self._store.list_all()

    def get_skill(self, skill_id: str) -> Skill | None:
        return self._store.get(skill_id)

    def remove_skill(self, skill_id: str) -> None:
        self._store.remove(skill_id)

    def deprecate_skill(self, skill_id: str) -> None:
        self._store.deprecate(skill_id)

    # ── feedback ─────────────────────────────────────────────────────────

    def record_usage(self, skill_id: str, success: bool) -> None:
        skill = self._store.get(skill_id)
        if skill:
            skill.record_use(success)
            self._store.update(skill)

    # ── LLM-based merge ──────────────────────────────────────────────────

    async def merge_skills(self, config: LLMConfig | None = None) -> dict:
        from alex.llm.json_client import create_json_completion

        active_skills = [s for s in self._store.list_all() if s.status != "DEPRECATED"]
        if len(active_skills) < 2:
            return {"merged": 0, "deprecated": 0, "remaining": len(active_skills)}

        prompt = render("merge_skills_prompt.j2", skills=[
            {"id": s.id, "name": s.name, "pattern": s.pattern,
             "instruction": s.instruction, "tags": s.tags}
            for s in active_skills
        ])

        try:
            text = await create_json_completion([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Output ONLY the JSON object. Start with { and end with }."},
            ], config=config)
        except Exception as e:
            return {"merged": 0, "deprecated": 0, "remaining": len(active_skills), "error": str(e)}

        try:
            from json_repair import repair_json
            data = repair_json(text, return_objects=True)
            if isinstance(data, list):
                data = next((item for item in data if isinstance(item, dict) and "merged_groups" in item), {})
        except Exception:
            return {"merged": 0, "deprecated": 0, "remaining": len(active_skills), "error": "Failed to parse LLM response"}

        if not isinstance(data, dict) or "merged_groups" not in data:
            return {"merged": 0, "deprecated": 0, "remaining": len(active_skills), "error": "No merged_groups in response"}

        merged_count = 0
        deprecated_count = 0

        for group in data.get("merged_groups", []):
            keep_id = group.get("keep_id", "")
            merge_ids = group.get("merge_ids", [])
            keeper = self._store.get(keep_id)
            if not keeper:
                continue

            if group.get("updated_name"):
                keeper.name = group["updated_name"]
            if group.get("updated_pattern"):
                keeper.pattern = group["updated_pattern"]
            if group.get("updated_instruction"):
                keeper.instruction = group["updated_instruction"]
            if group.get("updated_tags"):
                keeper.tags = list(set(keeper.tags + group["updated_tags"]))
            keeper.version += 1
            self._store.update(keeper)

            for mid in merge_ids:
                if mid != keep_id:
                    self._store.remove(mid)
                    merged_count += 1

        for dep_id in data.get("deprecate_ids", []):
            self._store.deprecate(dep_id)
            deprecated_count += 1

        remaining = len([s for s in self._store.list_all() if s.status != "DEPRECATED"])
        return {"merged": merged_count, "deprecated": deprecated_count, "remaining": remaining}
