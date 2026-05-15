"""Skill data model and SkillManager."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alex.prompts import get_skill_prompt, get_skills_section

if TYPE_CHECKING:
    from alex.skills.store import SkillStore


@dataclass
class Skill:
    """A learned response strategy — not factual knowledge, but *how* to respond."""

    name: str
    pattern: str  # When to use (trigger scenario)
    instruction: str  # How to respond (strategy)
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    status: str = "CANDIDATE"  # CANDIDATE | ACTIVE | DEPRECATED
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    confidence: float = 0.5
    version: int = 1
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0

    def record_use(self, success: bool) -> None:
        self.use_count += 1
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        self._update_confidence()

    def _update_confidence(self) -> None:
        """Bayesian-smoothed confidence estimate."""
        alpha = 2.0
        beta = 2.0
        n = self.success_count + self.failure_count
        self.confidence = (self.success_count + alpha) / (n + alpha + beta) if n > 0 else 0.5

    def to_prompt_text(self) -> str:
        return get_skill_prompt(
            self.id,
            name=self.name,
            pattern=self.pattern,
            instruction=self.instruction,
        )


class SkillManager:
    """Orchestrates the skill subsystems (retrieve, reflect, evolve)."""

    def __init__(
        self,
        store: SkillStore | None = None,
        reflector=None,
        retriever=None,
        evolution=None,
    ) -> None:
        if store is not None:
            self.store = store
        else:
            from alex.skills.store import SkillStore
            self.store = SkillStore()
        self._reflector = reflector
        self._retriever = retriever
        self._evolution = evolution

    # ── retrieval ────────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3) -> list[Skill]:
        """Return all active/candidate skills (model decides which to apply)."""
        return [s for s in self.store.list_all() if s.status in ("ACTIVE", "CANDIDATE")]

    def inject_skills_prompt(self, query: str) -> str:
        """Inject all skills as a directory — model decides which to apply."""
        skills = self.retrieve(query)
        if not skills:
            return ""
        return get_skills_section(skills=[
            {"name": s.name, "pattern": s.pattern, "instruction": s.instruction} for s in skills
        ])

    # ── reflection ───────────────────────────────────────────────────────

    async def reflect(self, recent_messages: list, llm) -> None:
        if self._reflector is None:
            from alex.skills.reflector import Reflector
            self._reflector = Reflector(llm)

        result = await self._reflector.reflect(
            recent_messages,
            [s for s in self.store.list_all() if s.status != "DEPRECATED"],
        )

        for skill in result.new_skills:
            self.store.add(skill)

        for update in result.updated_skills:
            skill_id = update.get("id", "")
            existing = self.store.get(skill_id)
            if existing:
                for k, v in update.items():
                    if k == "id":
                        continue
                    if hasattr(existing, k):
                        setattr(existing, k, v)
                existing.version += 1
                self.store.update(existing)

        for skill_id in result.deprecated_ids:
            self.store.deprecate(skill_id)

        if self._evolution is None:
            from alex.skills.evolution import EvolutionEngine as EE
            self._evolution = EE()
        self._evolution.evolve(self.store)

    # ── feedback ─────────────────────────────────────────────────────────

    def record_usage(self, skill_id: str, success: bool) -> None:
        skill = self.store.get(skill_id)
        if skill:
            skill.record_use(success)
            self.store.update(skill)

    # ── LLM-based merge ──────────────────────────────────────────────────

    async def merge_skills(self, llm) -> dict:
        """Use LLM to intelligently merge redundant skills.

        Returns a summary dict: {"merged": int, "deprecated": int, "remaining": int}
        """
        import json
        from alex.prompts import render
        from alex.llm.json_client import create_json_completion

        active_skills = [s for s in self.store.list_all() if s.status != "DEPRECATED"]
        if len(active_skills) < 2:
            return {"merged": 0, "deprecated": 0, "remaining": len(active_skills)}

        # Render the merge prompt with all skills
        prompt = render("merge_skills_prompt.j2", skills=[
            {"id": s.id, "name": s.name, "pattern": s.pattern,
             "instruction": s.instruction, "tags": s.tags}
            for s in active_skills
        ])

        try:
            text = await create_json_completion([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Output ONLY the JSON object. Start with { and end with }."},
            ])
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

        # Process merged groups
        for group in data.get("merged_groups", []):
            keep_id = group.get("keep_id", "")
            merge_ids = group.get("merge_ids", [])
            keeper = self.store.get(keep_id)
            if not keeper:
                continue

            # Update the keeper with consolidated content
            if group.get("updated_name"):
                keeper.name = group["updated_name"]
            if group.get("updated_pattern"):
                keeper.pattern = group["updated_pattern"]
            if group.get("updated_instruction"):
                keeper.instruction = group["updated_instruction"]
            if group.get("updated_tags"):
                keeper.tags = list(set(keeper.tags + group["updated_tags"]))
            keeper.version += 1
            self.store.update(keeper)

            # Remove merged skills
            for mid in merge_ids:
                if mid != keep_id:
                    self.store.remove(mid)
                    merged_count += 1

        # Process deprecations
        for dep_id in data.get("deprecate_ids", []):
            self.store.deprecate(dep_id)
            deprecated_count += 1

        remaining = len([s for s in self.store.list_all() if s.status != "DEPRECATED"])
        return {"merged": merged_count, "deprecated": deprecated_count, "remaining": remaining}
