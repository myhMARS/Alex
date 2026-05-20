"""Sliding-window buffer memory implementation."""

import asyncio

from langchain_core.messages import BaseMessage

from alex.memory.base import MemoryBase


class BufferMemory(MemoryBase):
    """Simple sliding-window memory.

    Retains the last `max_size` messages. No persistence.
    Write operations are serialised via an internal lock so that
    concurrent producers (e.g. user chat and cron stream replies)
    never interleave messages within a turn.
    """

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._messages: list[BaseMessage] = []
        self._write_lock = asyncio.Lock()

    def _append(self, msg: BaseMessage) -> None:
        self._messages.append(msg)
        if len(self._messages) > self._max_size:
            self._messages = self._messages[-self._max_size:]

    async def add_message(self, msg: BaseMessage) -> None:
        async with self._write_lock:
            self._append(msg)

    async def add_messages(self, msgs: list[BaseMessage]) -> None:
        async with self._write_lock:
            for m in msgs:
                self._append(m)

    async def get_context(self, query: str | None = None) -> list[BaseMessage]:
        return list(self._messages)

    async def clear(self) -> None:
        async with self._write_lock:
            self._messages.clear()

    @property
    def size(self) -> int:
        return len(self._messages)

    def get_context_sync(self) -> list[BaseMessage]:
        return list(self._messages)
