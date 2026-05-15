"""Sliding-window buffer memory implementation."""

from langchain_core.messages import BaseMessage

from alex.memory.base import MemoryBase


class BufferMemory(MemoryBase):
    """Simple sliding-window memory.

    Retains the last `max_size` messages. No persistence.
    """

    def __init__(self, max_size: int = 100) -> None:
        self._max_size = max_size
        self._messages: list[BaseMessage] = []

    async def add_message(self, msg: BaseMessage) -> None:
        self._messages.append(msg)
        if len(self._messages) > self._max_size:
            self._messages = self._messages[-self._max_size:]

    async def add_messages(self, msgs: list[BaseMessage]) -> None:
        for m in msgs:
            await self.add_message(m)

    async def get_context(self, query: str | None = None) -> list[BaseMessage]:
        return list(self._messages)

    async def clear(self) -> None:
        self._messages.clear()

    @property
    def size(self) -> int:
        return len(self._messages)

    def get_context_sync(self) -> list[BaseMessage]:
        return list(self._messages)
