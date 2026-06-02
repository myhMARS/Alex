"""State model tests — session view state.

Tests verify that state transitions are predictable and clean across
the key mutable objects in the application layer.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual", reason="textual is required for TUI state tests")

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
