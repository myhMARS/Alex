"""MemoryModule — exposes memory operations via the message bus.

Phase 2: thin wrapper around existing MemoryBase implementation.
Phase 3 will convert agent to use bus requests instead of direct calls.
"""

from __future__ import annotations

import logging
from typing import Any

from alex.kernel.contracts.memory import (
    AppendMessages,
    ClearMemory,
    GetContext,
    ReplaceMemory,
)
from alex.memory.base import MemoryBase

logger = logging.getLogger(__name__)


class MemoryModule:
    """Pluggable memory module — provides context storage via request/reply."""

    name = "memory"
    dependencies: list[str] = []

    def __init__(self, backend: MemoryBase | None = None) -> None:
        from alex.memory.buffer import BufferMemory
        self._backend = backend or BufferMemory()
        self._bus: Any = None

    async def start(self, bus: Any) -> None:
        self._bus = bus
        bus.provide(GetContext, self._handle_get_context)
        bus.provide(AppendMessages, self._handle_append)
        bus.provide(ReplaceMemory, self._handle_replace)
        bus.provide(ClearMemory, self._handle_clear)
        logger.info("MemoryModule started (provides GetContext/AppendMessages/ReplaceMemory/ClearMemory)")

    async def stop(self) -> None:
        self._bus = None

    # ── request handlers ─────────────────────────────────────────────────

    async def _handle_get_context(self, req: GetContext) -> list[dict[str, Any]]:
        return await self._backend.get_context(
            session_id=req.session_id,
            query=req.query,
        )

    async def _handle_append(self, req: AppendMessages) -> None:
        await self._backend.append(
            session_id=req.session_id,
            messages=req.messages,
        )

    async def _handle_replace(self, req: ReplaceMemory) -> None:
        await self._backend.replace(
            session_id=req.session_id,
            messages=req.messages,
        )

    async def _handle_clear(self, req: ClearMemory) -> None:
        await self._backend.clear(session_id=req.session_id)

    @property
    def backend(self) -> MemoryBase:
        return self._backend
