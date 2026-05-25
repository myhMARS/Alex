"""Memory module public interfaces."""

from __future__ import annotations

from typing import Protocol

from langchain_core.messages import BaseMessage


class MemoryService(Protocol):
    """Runtime message buffer — appends batches atomically, returns context.

    The memory module owns the runtime message sequence and its ordering
    guarantees.  It does NOT persist messages to disk — that belongs to
    the store module.
    """

    async def append(self, session_id: str, messages: list[BaseMessage]) -> None: ...

    async def get_context(self, session_id: str) -> list[BaseMessage]: ...

    async def replace(self, session_id: str, messages: list[BaseMessage]) -> None: ...

    async def clear(self, session_id: str) -> None: ...
