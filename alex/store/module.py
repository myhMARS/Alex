"""StoreModule — session persistence via the message bus.

Phase 2: wraps existing store implementation.
Subscribes to TurnCompleted to persist sessions.
Provides ListSessions / LoadSession for session management.
"""

from __future__ import annotations

import logging
from typing import Any

from alex.kernel.contracts.chat import TurnCompleted
from alex.kernel.contracts.session import (
    ListSessions,
    LoadSession,
)

logger = logging.getLogger(__name__)


class StoreModule:
    """Pluggable store module — persists sessions on TurnCompleted."""

    name = "store"
    dependencies: list[str] = []

    def __init__(self) -> None:
        self._bus: Any = None

    async def start(self, bus: Any) -> None:
        self._bus = bus
        # Subscribe to persistence triggers
        await bus.subscribe(TurnCompleted, self._on_turn_completed)
        # Provide session management
        bus.provide(ListSessions, self._handle_list_sessions)
        bus.provide(LoadSession, self._handle_load_session)
        logger.info("StoreModule started (provides ListSessions/LoadSession, subscribes TurnCompleted)")

    async def stop(self) -> None:
        self._bus = None

    # ── event handlers ───────────────────────────────────────────────────

    async def _on_turn_completed(self, event: TurnCompleted) -> None:
        """Persist the session when a turn completes."""
        from alex.store.session import save_session
        try:
            save_session(event.session_id, event.messages)
            logger.debug("Session %s persisted (%d messages)", event.session_id, len(event.messages))
        except Exception:
            logger.warning("Failed to persist session %s", event.session_id, exc_info=True)

    # ── request handlers ─────────────────────────────────────────────────

    async def _handle_list_sessions(self, _req: ListSessions) -> list[Any]:
        from alex.store.session import list_sessions
        return list_sessions()

    async def _handle_load_session(self, req: LoadSession) -> dict | None:
        from alex.store.session import load_session_bundle
        return load_session_bundle(req.session_id)
