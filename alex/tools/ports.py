"""Tools module public interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolExecutionContext:
    """Runtime context injected into every tool execution.

    Replaces the bare ``session_id`` string so future session-aware
    tools (audit, logging, cron_history) can receive context without
    implicit coupling to the agent host.
    """

    session_id: str
    turn_id: str | None = None
    source: str = "user"  # "user" | "cron" | "system"
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolRegistry(Protocol):
    """Tool registration — add, remove, and look up tools by name."""

    def register(self, tool: Any) -> None: ...
    def unregister(self, name: str) -> None: ...
    def get(self, name: str) -> Any | None: ...
    def list(self) -> list[Any]: ...


class ToolExecutor(Protocol):
    """Tool execution — run a registered tool by name with arguments."""

    async def execute(self, ctx: ToolExecutionContext, name: str, args: dict[str, Any]) -> str: ...


class CronScheduler(Protocol):
    """Cron job lifecycle — schedule and cancel background jobs."""

    async def schedule_cron_job(
        self,
        *,
        name: str,
        cron: str = "",
        interval_seconds: int | None = None,
        repeat: int = 1,
        subscribe: bool = False,
        run_now: bool = False,
        action: str = "",
        params: dict | None = None,
    ) -> str: ...

    async def cancel_cron_job(self, job_id: str) -> bool: ...
