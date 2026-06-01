"""LLM-based reflection engine — analyzes conversations to extract skills."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from alex import messages as msg
from alex.llm.base import LLMConfig
from alex.prompts import get_reflection_prompt
from alex.skill.models import Skill


@dataclass
class ReflectionResult:
    new_skills: list[Skill] = field(default_factory=list)
    updated_skills: list[dict] = field(default_factory=list)
    deprecated_ids: list[str] = field(default_factory=list)


class Reflector:
    """Calls an LLM to analyze conversations and extract execution workflows."""

    async def reflect(
        self,
        messages: list[dict[str, Any]],
        existing_skills: list[Skill],
        episodes: list[dict] | None = None,
        config: LLMConfig | None = None,
    ) -> ReflectionResult:
        """Analyze accumulated conversation experience to extract reusable methodologies."""
        from alex.llm import create_json_completion

        full_prompt = get_reflection_prompt(
            existing_skills=[
                {"id": s.id, "name": s.name, "pattern": s.pattern}
                for s in existing_skills
            ],
            episodes=episodes or [],
        )

        # Format messages for API — distinguish roles
        api_messages: list[dict[str, str]] = [{"role": "system", "content": full_prompt}]
        for m in messages:
            if msg.is_user(m):
                api_messages.append({"role": "user", "content": str(m.get("content", ""))})
            elif msg.is_assistant(m):
                api_messages.append({"role": "assistant", "content": str(m.get("content", ""))})
            elif msg.is_tool(m):
                label = f"[tool_result id={msg.get_tool_call_id(m)}] {m.get('content', '')}"
                api_messages.append({"role": "user", "content": label})
            elif msg.is_system(m):
                api_messages.append({"role": "system", "content": str(m.get("content", ""))})
            elif m.get("content"):
                api_messages.append({"role": "assistant", "content": str(m.get("content", ""))})
        api_messages.append({"role": "user", "content": "Extract execution workflows from this conversation. Output only JSON."})

        text = await create_json_completion(api_messages, max_tokens=4096, config=config)
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
