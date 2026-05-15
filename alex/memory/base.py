"""Abstract async memory interface for the Alex agent."""

from abc import ABC, abstractmethod

from langchain_core.messages import BaseMessage


class MemoryBase(ABC):
    """Abstract async memory interface.

    Agent depends only on this interface. Swap implementations
    (BufferMemory, Mem0, MemGPT, vector DB) without touching Agent code.

    All methods are async to support networked backends (Redis, Mem0, etc.).
    """

    @abstractmethod
    async def add_message(self, msg: BaseMessage) -> None: ...

    @abstractmethod
    async def add_messages(self, msgs: list[BaseMessage]) -> None: ...

    @abstractmethod
    async def get_context(self, query: str | None = None) -> list[BaseMessage]: ...

    @abstractmethod
    async def clear(self) -> None: ...

    @property
    @abstractmethod
    def size(self) -> int: ...

    async def summarize(self) -> str:
        """Optional — generate a summary of memory contents."""
        return ""

    async def search(self, query: str, top_k: int = 5) -> list[BaseMessage]:
        """Optional — semantic search over memory (RAG-ready)."""
        return []

    def get_context_sync(self) -> list[BaseMessage]:
        """Synchronous fallback for internal reads (local backends only).

        Remote backends may raise NotImplementedError — Agent uses this
        only for non-blocking introspection (history property, etc.).
        """
        raise NotImplementedError
