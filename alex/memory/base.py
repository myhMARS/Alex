"""Abstract async memory interface for the Alex agent.

Messages are plain ``{"role": ..., "content": ...}`` dicts,
compatible with the OpenAI Chat Completions API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryBase(ABC):
    """Abstract async memory interface.

    Agent depends only on this interface. Swap implementations
    (BufferMemory, Mem0, vector DB) without touching Agent code.

    All methods accept session_id so implementations can partition
    messages by session (e.g. networked backends).  In-process backends
    may ignore the parameter.
    """

    @abstractmethod
    async def add_message(self, msg: dict[str, Any], session_id: str = "") -> None: ...

    @abstractmethod
    async def add_messages(self, msgs: list[dict[str, Any]], session_id: str = "") -> None: ...

    async def append(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """MemoryService-compatible batch append."""
        await self.add_messages(messages, session_id=session_id)

    @abstractmethod
    async def get_context(self, session_id: str = "", query: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def clear(self, session_id: str = "") -> None: ...

    async def replace(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """MemoryService-compatible atomic replace — clear then append."""
        await self.clear(session_id=session_id)
        await self.add_messages(messages, session_id=session_id)

    @property
    @abstractmethod
    def size(self) -> int: ...

    async def summarize(self) -> str:
        """Optional — generate a summary of memory contents."""
        return ""

    async def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Optional — semantic search over memory (RAG-ready)."""
        return []

    def get_context_sync(self, session_id: str = "") -> list[dict[str, Any]]:
        """Synchronous fallback for internal reads (local backends only).

        Remote backends may raise NotImplementedError — Agent uses this
        only for non-blocking introspection (history property, etc.).
        """
        raise NotImplementedError
