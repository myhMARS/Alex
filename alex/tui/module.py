"""TuiModule — wraps the TUI application in the Module interface.

Phase 7: The TUI publishes UserTurnRequested and subscribes to streaming
UI events (TokenEmitted, ThinkingUpdated, etc.) instead of calling
agent.chat_stream() directly.
"""

from __future__ import annotations

import logging
from typing import Any

from alex.kernel.contracts.chat import (
    ThinkingUpdated,
    TokenEmitted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    UserTurnRequested,
)
from alex.kernel.contracts.skills import SkillLoaded, SkillsReflected
from alex.kernel.contracts.tools import ToolApprovalRequested, ToolFinished, ToolStarted
from alex.kernel.contracts.cron import CronJobEvent

logger = logging.getLogger(__name__)


class TuiModule:
    """Pluggable TUI module — bridges the Textual UI with the message bus.

    The TUI:
    - Publishes UserTurnRequested when the user hits enter
    - Subscribes to streaming events (TokenEmitted, ThinkingUpdated, etc.)
    - Subscribes to status events (CronJobEvent, SkillsReflected, etc.)
    - Shows tool approval modals via ToolApprovalRequested/Resolved
    """

    name = "tui"

    def __init__(self, app: Any = None) -> None:
        self._app = app
        self._bus: Any = None

    async def start(self, bus: Any) -> None:
        self._bus = bus
        # Subscribe to all UI-relevant events
        for event_type in (
            TurnStarted, TokenEmitted, ThinkingUpdated, SkillLoaded,
            ToolStarted, ToolFinished, TurnCompleted, TurnFailed,
            SkillsReflected, CronJobEvent, ToolApprovalRequested,
        ):
            await bus.subscribe(event_type, self._route_to_app)

        logger.info("TuiModule started (subscribes to UI events, publishes UserTurnRequested)")

    async def stop(self) -> None:
        self._bus = None

    # ── publish helpers (called by TUI widgets) ──────────────────────────

    def publish_user_turn(self, session_id: str, user_text: str) -> None:
        """Called by the TUI input widget when the user submits a message."""
        if self._bus:
            self._bus.publish(UserTurnRequested(
                session_id=session_id,
                user_text=user_text,
            ))

    # ── event routing ────────────────────────────────────────────────────

    async def _route_to_app(self, event: Any) -> None:
        """Route bus events to the TUI app for rendering.

        In the current architecture, the TUI already has event handlers
        registered directly.  When the bus is used as the sole channel,
        the TUI's existing rendering hooks are called here.
        """
        if self._app is not None:
            try:
                # Delegate to the app's existing event dispatch
                await self._app.post_message(event)
            except Exception:
                logger.debug("Failed to dispatch event to TUI app", exc_info=True)

    @property
    def bus(self) -> Any:
        return self._bus

    @property
    def app(self) -> Any:
        return self._app
