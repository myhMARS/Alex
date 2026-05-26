"""FeedbackAppService — feedback recording, episode tracking, and reflection.

Extracted from FeedbackRecorder.  Manages per-session feedback state,
recording turn episodes, and triggering periodic / forced reflection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

from alex.bus.events import SkillReflectErrorEvent, SkillReflectEvent
from alex.memory.base import MemoryBase
from alex.skill.models import SkillManager

logger = logging.getLogger(__name__)

_REFLECT_INTERVAL = 5


@dataclass
class FeedbackSessionState:
    """Per-session feedback counters and episode log."""
    turn_count: int = 0
    reflecting: bool = False
    episodes: list[dict] = field(default_factory=list)


class FeedbackAppService:
    """Application service for feedback recording and skill reflection.

    Each session gets its own FeedbackSessionState managed via
    ``_sessions[session_id]``.  The old FeedbackRecorder instance-level
    mutable fields are replaced by this dictionary.
    """

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
        self._sessions: dict[str, FeedbackSessionState] = {session_id: FeedbackSessionState()}

    def _state(self) -> FeedbackSessionState:
        if self._session_id not in self._sessions:
            self._sessions[self._session_id] = FeedbackSessionState()
        return self._sessions[self._session_id]

    @property
    def turn_count(self) -> int:
        return self._state().turn_count

    @property
    def is_reflecting(self) -> bool:
        return self._state().reflecting

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id
        if session_id not in self._sessions:
            self._sessions[session_id] = FeedbackSessionState()

    def reset_session_state(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def provide_feedback(self, positive: bool, last_used_skill_ids: list[str]) -> None:
        for skill_id in last_used_skill_ids:
            self._skills.record_usage(skill_id, positive)

    def record_episode(
        self, user_message: str, loaded_skills: list[str],
        tool_names: list[str], response: str,
    ) -> None:
        self._state().episodes.append({
            "query": user_message[:200],
            "skills_loaded": loaded_skills,
            "tools_used": tool_names,
            "outcome": response[:300],
        })

    async def maybe_reflect(self, last_query_matched: bool) -> None:
        state = self._state()
        state.turn_count += 1

        should_reflect = state.turn_count % _REFLECT_INTERVAL == 0
        if not last_query_matched:
            should_reflect = True

        if should_reflect:
            state.reflecting = True
            try:
                await self._do_reflect()
            finally:
                state.reflecting = False

    async def reflect(self) -> dict:
        state = self._state()
        state.reflecting = True
        try:
            return await self._do_reflect()
        finally:
            state.reflecting = False

    async def _do_reflect(self) -> dict:
        state = self._state()
        try:
            recent = await self._memory.get_context(session_id=self._session_id)
            recent = recent[-20:]
            summary = await self._skills.reflect(recent, self._llm, episodes=state.episodes)
            state.episodes.clear()
            self._push_notification(SkillReflectEvent(
                new=summary.get("new", 0),
                updated=summary.get("updated", 0),
                deprecated=summary.get("deprecated", 0),
                names=summary.get("new_skill_names", []),
                updated_names=summary.get("updated_skill_names", []),
            ))
            return summary
        except Exception:
            logger.warning("Skill reflection failed", exc_info=True)
            self._push_notification(SkillReflectErrorEvent(error="reflection failed"))
            return None
