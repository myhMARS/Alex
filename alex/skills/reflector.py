"""LLM-based reflection engine — analyzes conversations to extract skills."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage, HumanMessage

from alex.prompts import get_reflection_prompt
from alex.skills.base import Skill


@dataclass
class ReflectionResult:
    new_skills: list[Skill] = field(default_factory=list)
    updated_skills: list[dict] = field(default_factory=list)  # partial updates: {"id": ..., field: value}
    deprecated_ids: list[str] = field(default_factory=list)


class Reflector:
    """Calls an LLM to analyze conversations and extract execution workflows."""

    async def reflect(
        self,
        messages: list[BaseMessage],
        existing_skills: list[Skill],
        episodes: list[dict] | None = None,
    ) -> ReflectionResult:
        """Analyze accumulated conversation experience to extract reusable methodologies."""
        from alex.llm.json_client import create_json_completion

        full_prompt = get_reflection_prompt(
            existing_skills=[
                {"id": s.id, "name": s.name, "pattern": s.pattern}
                for s in existing_skills
            ],
            episodes=episodes or [],
        )

        # Format messages for API
        api_messages: list[dict[str, str]] = [{"role": "system", "content": full_prompt}]
        for msg in messages:
            if isinstance(msg, HumanMessage):
                api_messages.append({"role": "user", "content": msg.content})
            elif hasattr(msg, "content") and msg.content:
                api_messages.append({"role": "assistant", "content": msg.content})
        api_messages.append({"role": "user", "content": "Extract execution workflows from this conversation. Output only JSON."})

        text = await create_json_completion(api_messages, max_tokens=4096)
        return self._parse(text)

    def _parse(self, text: str) -> ReflectionResult:
        """Parse LLM JSON output into ReflectionResult."""
        try:
            from json_repair import repair_json
            data = repair_json(text, return_objects=True)
        except Exception:
            return ReflectionResult()

        if not isinstance(data, dict):
            return ReflectionResult()

        result = ReflectionResult()
        for item in data.get("new_skills", []):
            if isinstance(item, dict):
                result.new_skills.append(Skill(
                    name=item.get("name", ""),
                    pattern=item.get("pattern", ""),
                    instruction=item.get("instruction", ""),
                    tags=item.get("tags", []),
                ))
        for item in data.get("updated_skills", []):
            if isinstance(item, dict):
                sid = item.pop("id", "")
                if sid:
                    result.updated_skills.append({"id": sid, **item})
        result.deprecated_ids = data.get("deprecated_ids", [])
        return result
