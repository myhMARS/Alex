"""Sliding-window buffer memory implementation."""

from __future__ import annotations

import asyncio
from typing import Any

from alex.memory.base import MemoryBase


class BufferMemory(MemoryBase):
    """Sliding-window memory partitioned by session_id.

    Retains the last `max_size` messages per session.  No persistence.
    Write operations are serialised via a per-session lock so that
    concurrent producers (e.g. user chat and cron stream replies)
    never interleave messages within a turn.
    """

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def _get_messages(self, session_id: str) -> list[dict[str, Any]]:
        if session_id not in self._messages:
            self._messages[session_id] = []
        return self._messages[session_id]

    def _append(self, session_id: str, msg: dict[str, Any]) -> None:
        msgs = self._get_messages(session_id)
        msgs.append(msg)
        if len(msgs) > self._max_size:
            self._messages[session_id] = msgs[-self._max_size:]

    async def add_message(self, msg: dict[str, Any], session_id: str = "") -> None:
        async with self._get_lock(session_id):
            self._append(session_id, msg)

    async def add_messages(self, msgs: list[dict[str, Any]], session_id: str = "") -> None:
        async with self._get_lock(session_id):
            for m in msgs:
                self._append(session_id, m)

    async def get_context(self, session_id: str = "", query: str | None = None) -> list[dict[str, Any]]:
        return list(self._get_messages(session_id))

    async def clear(self, session_id: str = "") -> None:
        async with self._get_lock(session_id):
            self._messages.pop(session_id, None)

    @property
    def size(self) -> int:
        return sum(len(msgs) for msgs in self._messages.values())

    def get_context_sync(self, session_id: str = "") -> list[dict[str, Any]]:
        return list(self._get_messages(session_id))
