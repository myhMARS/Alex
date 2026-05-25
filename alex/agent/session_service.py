"""SessionService — owns the store-to-agent boundary.

Wraps SessionPersistence and deserialize_message so the Agent layer
doesn't reach into store internals.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import BaseMessage

from alex.memory.base import MemoryBase
from alex.store.session_adapter import SessionPersistence

logger = logging.getLogger(__name__)


class SessionService:
    """Wraps session persistence and history restore at the agent boundary."""

    def list_sessions(self) -> list[dict[str, Any]]:
        return SessionPersistence.list_sessions()

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        return SessionPersistence.load(session_id)

    async def subscribe_store(self, bus: Any) -> None:
        await SessionPersistence.subscribe(bus)

    async def restore_history(
        self, messages: list, memory: MemoryBase, session_id: str,
    ) -> None:
        """Clear memory and replay a serialized message sequence."""
        from alex.store.session import deserialize_message

        await memory.clear(session_id=session_id)
        for item in messages:
            if isinstance(item, BaseMessage):
                msg = item
            elif isinstance(item, dict):
                msg = deserialize_message(item)
            else:
                continue
            await memory.add_message(msg, session_id=session_id)
