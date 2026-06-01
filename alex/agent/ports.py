"""Agent module public interfaces — the sole boundary TUI / frontends depend on.

AgentFacade is the main contract.  Narrow protocols are referenced by
orchestrator and sub-components.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Protocol

from alex.tools.ports import ToolExecutionContext


class AgentFacade(Protocol):
    """Public API of the agent — the sole boundary TUI / frontends depend on.

    This is a concrete-as-protocol: Agent already implements every method.
    Defining it as a Protocol lets the TUI type-hint against the contract
    rather than the implementation, enabling alternate backends.
    """

    # ── lifecycle ───────────────────────────────────────────────────────

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    async def start_services(self) -> None: ...

    async def shutdown(self) -> None: ...

    # ── bus ─────────────────────────────────────────────────────────────

    @property
    def bus(self) -> Any | None: ...

    def bind_event_bus(self, bus: Any) -> None: ...

    # ── session ─────────────────────────────────────────────────────────

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None: ...

    @property
    def session_id(self) -> str: ...

    async def restore_history(self, messages: list) -> None: ...

    async def clear_history(self) -> None: ...

    # ── chat ────────────────────────────────────────────────────────────

    @property
    def last_turn_result(self) -> Any | None: ...

    def chat_stream(self, user_message: str) -> AsyncIterator[Any]: ...

    # ── feedback ────────────────────────────────────────────────────────

    def provide_feedback(self, positive: bool, turn_id: str = "") -> None: ...

    # ── skills ──────────────────────────────────────────────────────────

    async def reflect(self) -> dict: ...

    def list_skills(self) -> list[dict]: ...

    def delete_skill(self, target: str) -> str | None: ...

    def deprecate_skill(self, target: str) -> str | None: ...

    async def merge_skills(self) -> dict: ...

    # ── session persistence ─────────────────────────────────────────────

    def list_sessions(self) -> list[dict]: ...

    def load_session(self, session_id: str) -> dict | None: ...

    async def subscribe_store(self, bus: Any) -> None: ...

    # ── cron ────────────────────────────────────────────────────────────

    def list_cron_jobs(self) -> list[dict]: ...

    def list_session_cron_history(self, query: str = "", limit: int = 20) -> list[dict]: ...


# ── Narrow internal protocols ────────────────────────────────────────────


class LLMGateway(Protocol):
    """Streaming LLM — the orchestrator calls this to get token/tool events."""

    async def stream(
        self, messages: list[dict[str, Any]], system_prompt: str
    ) -> AsyncIterator[dict[str, Any]]: ...


class MemoryPort(Protocol):
    """Runtime message storage — read/write the conversation history."""

    async def get_context(self, session_id: str) -> list[dict[str, Any]]: ...

    async def append(self, session_id: str, messages: list[dict[str, Any]]) -> None: ...

    async def clear(self, session_id: str) -> None: ...


class ToolExecutorPort(Protocol):
    """Tool execution — run a registered tool by name within a session context."""

    async def execute(self, ctx: ToolExecutionContext, name: str, args: dict[str, Any]) -> str: ...


__all__ = [
    "AgentFacade",
    "LLMGateway",
    "MemoryPort",
    "ToolExecutorPort",
]
