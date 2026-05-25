"""Tests for FeedbackRecorder — skill usage recording and reflection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from alex.agent.feedback import FeedbackRecorder


def _make_feedback(push=None):
    if push is None:
        push = MagicMock()
    memory = MagicMock()
    memory.get_context = AsyncMock(return_value=[HumanMessage(content="test")])
    skill_manager = MagicMock()
    skill_manager.reflect = AsyncMock(return_value={
        "new": 0, "updated": 0, "deprecated": 0,
        "new_skill_names": [], "updated_skill_names": [],
    })
    llm = MagicMock()
    return FeedbackRecorder(memory, skill_manager, llm, push)


class TestFeedbackRecorder:
    def test_initial_state(self):
        fb = _make_feedback()
        assert fb.turn_count == 0
        assert fb.is_reflecting is False

    def test_provide_feedback_positive(self):
        fb = _make_feedback()
        fb.provide_feedback(True, ["skill1", "skill2"])
        fb._skills.record_usage.assert_any_call("skill1", True)
        fb._skills.record_usage.assert_any_call("skill2", True)
        assert fb._skills.record_usage.call_count == 2

    def test_provide_feedback_negative(self):
        fb = _make_feedback()
        fb.provide_feedback(False, ["skill1"])
        fb._skills.record_usage.assert_called_once_with("skill1", False)

    def test_record_episode(self):
        fb = _make_feedback()
        fb.record_episode(
            "how do I sort a list?",
            ["python_skill"],
            ["web_search"],
            "Use sorted() function",
        )
        assert len(fb._skill_episodes) == 1
        ep = fb._skill_episodes[0]
        assert ep["query"] == "how do I sort a list?"
        assert ep["skills_loaded"] == ["python_skill"]
        assert ep["tools_used"] == ["web_search"]
        assert "sorted()" in ep["outcome"]

    @pytest.mark.asyncio
    async def test_maybe_reflect_periodic(self):
        fb = _make_feedback()
        # Set turn_count so next maybe_reflect triggers (every 5 turns)
        fb._turn_count = 4
        assert await fb.maybe_reflect(True) is None
        assert fb._skills.reflect.called

    @pytest.mark.asyncio
    async def test_maybe_reflect_new_domain(self):
        fb = _make_feedback()
        # New domain (no skills matched) should trigger reflection immediately
        fb._turn_count = 1  # not a multiple of 5
        assert await fb.maybe_reflect(False) is None
        assert fb._skills.reflect.called

    @pytest.mark.asyncio
    async def test_maybe_reflect_no_trigger(self):
        fb = _make_feedback()
        fb._turn_count = 1  # not periodic, last_query_matched=True
        assert await fb.maybe_reflect(True) is None
        assert not fb._skills.reflect.called

    @pytest.mark.asyncio
    async def test_reflect_force(self):
        fb = _make_feedback()
        result = await fb.reflect()
        assert fb._skills.reflect.called
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_reflect_publishes_event(self):
        push = MagicMock()
        fb = _make_feedback(push=push)
        await fb.reflect()
        assert push.called

    @pytest.mark.asyncio
    async def test_reflect_error_publishes_error_event(self):
        push = MagicMock()
        fb = _make_feedback(push=push)
        fb._skills.reflect = AsyncMock(side_effect=RuntimeError("boom"))
        result = await fb.reflect()
        assert result == {}
        # Should have published SkillReflectErrorEvent
        assert push.called

    def test_set_session_id(self):
        fb = _make_feedback()
        fb.set_session_id("new-session")
        assert fb._session_id == "new-session"
