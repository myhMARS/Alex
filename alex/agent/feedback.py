"""Feedback recording — skill usage tracking and periodic reflection."""

from __future__ import annotations

import logging

from langchain_core.language_models import BaseChatModel

from alex.bus.events import SkillReflectErrorEvent, SkillReflectEvent
from alex.memory.base import MemoryBase
from alex.skill.models import SkillManager

logger = logging.getLogger(__name__)

_REFLECT_INTERVAL = 5  # periodic reflect every N turns


class FeedbackRecorder:
    """Records skill usage feedback and triggers periodic skill reflection."""

    def __init__(
        self,
        memory: MemoryBase,
        skill_manager: SkillManager,
        llm: BaseChatModel,
        push_notification: callable,
        session_id: str = "",
    ) -> None:
        self._memory = memory
        self._skills = skill_manager
        self._llm = llm
        self._push_notification = push_notification
        self._session_id = session_id
        self._turn_count = 0
        self._reflecting = False
        self._skill_episodes: list[dict] = []

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def is_reflecting(self) -> bool:
        return self._reflecting

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id
        self._turn_count = 0
        self._skill_episodes.clear()

    def provide_feedback(self, positive: bool, last_used_skill_ids: list[str]) -> None:
        """Record skill usage and trigger reflection on negative feedback."""
        for skill_id in last_used_skill_ids:
            self._skills.record_usage(skill_id, positive)

    def record_episode(
        self, user_message: str, loaded_skills: list[str],
        tool_names: list[str], response: str,
    ) -> None:
        """Record a problem-solving episode for multi-turn skill extraction."""
        self._skill_episodes.append({
            "query": user_message[:200],
            "skills_loaded": loaded_skills,
            "tools_used": tool_names,
            "outcome": response[:300],
        })

    async def maybe_reflect(self, last_query_matched: bool) -> None:
        """Check reflection triggers and run if needed."""
        self._turn_count += 1

        should_reflect = self._turn_count % _REFLECT_INTERVAL == 0
        if not last_query_matched:
            should_reflect = True

        if should_reflect:
            self._reflecting = True
            await self._do_reflect()
            self._reflecting = False

    async def reflect(self) -> dict:
        """Force skill reflection. Returns summary dict."""
        self._reflecting = True
        try:
            return await self._do_reflect()
        finally:
            self._reflecting = False

    async def _do_reflect(self) -> dict:
        self._reflecting = True
        try:
            recent = await self._memory.get_context(session_id=self._session_id)
            recent = recent[-20:]
            summary = await self._skills.reflect(recent, self._llm, episodes=self._skill_episodes)
            self._skill_episodes.clear()
            self._push_notification(SkillReflectEvent(
                new=summary.get("new", 0),
                updated=summary.get("updated", 0),
                deprecated=summary.get("deprecated", 0),
                names=summary.get("new_skill_names", []),
                updated_names=summary.get("updated_skill_names", []),
            ))
            return summary
        except Exception as e:
            logger.warning("Skill reflection failed", exc_info=True)
            self._push_notification(SkillReflectErrorEvent(error=str(e)))
            return {}
        finally:
            self._reflecting = False
