"""TUI session view state — single source of truth for UI-only mutable state.

All fields here reset on session switch or /clear.  This keeps the reset
logic in one place instead of scattered across controller and app.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionViewState:
    """UI-only state that must reset when the session changes."""

    page_mode: str | None = None
    showing_session_list: bool = False
    session_options: list = field(default_factory=list)
    pending_feedback_turn_id: str = ""
    last_response_rated: bool = True

    def reset(self) -> None:
        """Reset all fields for a new session or /clear."""
        self.page_mode = None
        self.showing_session_list = False
        self.session_options.clear()
        self.pending_feedback_turn_id = ""
        self.last_response_rated = True
