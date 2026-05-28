"""Feedback recording — skill usage tracking and periodic reflection."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel

from alex.bus.events import SkillReflectErrorEvent, SkillReflectEvent
from alex.memory.base import MemoryBase
from alex.skill import SkillService

logger = logging.getLogger(__name__)

_REFLECT_INTERVAL = 5  # periodic reflect every N turns


@dataclass
class FeedbackSessionState:
    """Per-session feedback state — one instance per active session.

    Replaces the old instance-level fields so session switching is
    explicit and multi-session debugging is straightforward.
    """

    turn_count: int = 0
    reflecting: bool = False
    episodes: list[dict] = field(default_factory=list)


class FeedbackRecorder:
    """Records skill usage feedback and triggers periodic skill reflection.

    Manages per-session FeedbackSessionState so that session switching
    resets all feedback counters and episode buffers cleanly.
    """

    def __init__(
        self,
        memory: MemoryBase,
        skill_manager: SkillService,
        llm: BaseChatModel,
        push_notification: callable,
        session_id: str = "",
    ) -> None:
        self._memory = memory
        self._skills = skill_manager
        self._llm = llm
        self._push_notification = push_notification
        self._session_id = session_id
        self._sessions: dict[str, FeedbackSessionState] = {}
        if session_id:
            self._sessions[session_id] = FeedbackSessionState()

    # ── per-session state access ─────────────────────────────────────────

    def _state(self) -> FeedbackSessionState:
        """Return (and auto-create if missing) state for current session."""
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
        # Reset the session-scoped counters on switch
        st = self._sessions[session_id]
        st.turn_count = 0
        st.episodes.clear()

    def reset_session_state(self, session_id: str) -> None:
        """Explicitly clear feedback state for a session (used on /clear)."""
        if session_id in self._sessions:
            del self._sessions[session_id]

    def provide_feedback(self, positive: bool, last_used_skill_ids: list[str]) -> None:
        """Record skill usage and trigger reflection on negative feedback."""
        for skill_id in last_used_skill_ids:
            self._skills.record_usage(skill_id, positive)

    def record_episode(
        self, user_message: str, loaded_skills: list[str],
        tool_names: list[str], response: str,
    ) -> None:
        """Record a problem-solving episode for multi-turn skill extraction."""
        self._state().episodes.append({
            "query": user_message[:200],
            "skills_loaded": loaded_skills,
            "tools_used": tool_names,
            "outcome": response[:300],
        })

    async def maybe_reflect(self, last_query_matched: bool) -> None:
        """Check reflection triggers and run if needed."""
        st = self._state()
        st.turn_count += 1

        should_reflect = st.turn_count % _REFLECT_INTERVAL == 0
        if not last_query_matched:
            should_reflect = True

        if should_reflect:
            st.reflecting = True
            await self._do_reflect()
            st.reflecting = False

    async def reflect(self) -> dict:
        """Force skill reflection. Returns summary dict."""
        st = self._state()
        st.reflecting = True
        try:
            return await self._do_reflect()
        finally:
            st.reflecting = False

    async def _do_reflect(self) -> dict:
        st = self._state()
        st.reflecting = True
        try:
            recent = await self._memory.get_context(session_id=self._session_id)
            recent = recent[-20:]
            summary = await self._skills.reflect(recent, self._llm, episodes=st.episodes)
            st.episodes.clear()
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
            st.reflecting = False
