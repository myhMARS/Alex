"""State model tests — session switching, feedback state, cron cancel.

Tests verify that state transitions are predictable and clean across
the key mutable objects in the application layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alex import messages as msg


async def _async_noop(_event) -> None:
    return None


async def _append_notification(notifications: list, event) -> None:
    notifications.append(event)


# ── FeedbackSessionState lifecycle ────────────────────────────────────────────

class TestFeedbackSessionState:
    """Feedback state must be isolated per session, track turn counts,
    and reset cleanly."""

    def test_state_auto_creates_for_new_session(self):
        from alex.agent.feedback_service import FeedbackAppService, FeedbackSessionState

        svc = FeedbackAppService(
            memory=MagicMock(),
            skill_manager=MagicMock(),
            config=MagicMock(),
            push_notification=_async_noop,
        )
        assert svc.turn_count == 0
        assert svc.is_reflecting is False

    def test_session_switch_resets_state(self):
        from alex.agent.feedback_service import FeedbackAppService, FeedbackSessionState

        svc = FeedbackAppService(
            memory=MagicMock(),
            skill_manager=MagicMock(),
            config=MagicMock(),
            push_notification=_async_noop,
        )

        svc.set_session_id("session-a")
        svc._state().turn_count = 10
        assert svc.turn_count == 10

        svc.set_session_id("session-b")
        assert svc.turn_count == 0
        assert svc._sessions["session-a"].turn_count == 10

    def test_reset_session_state_clears_specific_session(self):
        from alex.agent.feedback_service import FeedbackAppService

        svc = FeedbackAppService(
            memory=MagicMock(),
            skill_manager=MagicMock(),
            config=MagicMock(),
            push_notification=_async_noop,
        )
        svc.set_session_id("sid")
        svc._state().turn_count = 5
        assert "sid" in svc._sessions

        svc.reset_session_state("sid")
        assert "sid" not in svc._sessions

    def test_record_episode_appends(self):
        from alex.agent.feedback_service import FeedbackAppService

        svc = FeedbackAppService(
            memory=MagicMock(),
            skill_manager=MagicMock(),
            config=MagicMock(),
            push_notification=_async_noop,
        )
        svc.set_session_id("ep-session")
        svc.record_episode("user query", ["skill1"], ["tool1"], "response")

        episodes = svc._state().episodes
        assert len(episodes) == 1
        assert episodes[0]["query"] == "user query"
        assert episodes[0]["skills_loaded"] == ["skill1"]

    @pytest.mark.asyncio
    async def test_maybe_reflect_triggers_on_interval(self):
        from alex.agent.feedback_service import FeedbackAppService

        memory = MagicMock()
        memory.get_context = AsyncMock(return_value=[msg.user_message("hi")])

        skills = MagicMock()
        skills.reflect = AsyncMock(return_value={
            "new": 0, "updated": 0, "deprecated": 0,
            "new_skill_names": [], "updated_skill_names": [],
        })

        notifications = []
        svc = FeedbackAppService(
            memory=memory,
            skill_manager=skills,
            config=MagicMock(),
            push_notification=lambda e: _append_notification(notifications, e),
        )
        svc.set_session_id("reflect-session")
        svc._state().turn_count = 4

        await svc.maybe_reflect(True)
        assert svc.turn_count == 5
        assert skills.reflect.called


# ── Cron cancel semantics ──────────────────────────────────────────────────────

class TestCronCancel:
    """Cron jobs must cancel cleanly."""

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self):
        from alex.agent.cron_service import CronService

        svc = CronService(notify_callback=lambda e: None)
        result = await svc.cancel("nonexistent-job-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_existing_job_returns_true(self):
        from alex.agent.cron_service import CronService
        from alex.scheduler.manager import CronJob

        svc = CronService(notify_callback=lambda e: None)
        job = CronJob(
            id="manual-job",
            session_id="s1",
            name="test-job",
            cron="*/5 * * * *",
            prompt="say hi",
            recurring=True,
            durable=False,
        )
        svc._manager._jobs["manual-job"] = job
        assert svc._manager.get_job("manual-job") is not None

        result = await svc.cancel("manual-job")
        assert result is True
        cancelled = svc._manager.get_job("manual-job")
        assert cancelled.status == "CANCELLED"


# ── Session view state ────────────────────────────────────────────────────────

class TestSessionViewState:
    """SessionViewState must reset cleanly on session switch."""

    def test_reset_clears_all_fields(self):
        from alex.tui.view_state import SessionViewState

        state = SessionViewState()
        state.page_mode = "cron_history"
        state.showing_session_list = True
        state.session_options = [("id1", "Session 1")]
        state.pending_feedback_turn_id = "turn123"
        state.last_response_rated = False

        state.reset()

        assert state.page_mode is None
        assert state.showing_session_list is False
        assert state.session_options == []
        assert state.pending_feedback_turn_id == ""
        assert state.last_response_rated is True

    def test_default_values(self):
        from alex.tui.view_state import SessionViewState

        state = SessionViewState()
        assert state.page_mode is None
        assert state.showing_session_list is False
        assert state.pending_feedback_turn_id == ""
        assert state.last_response_rated is True
