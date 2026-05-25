"""CronService — wraps CronManager lifecycle for the agent layer."""

from __future__ import annotations

import asyncio
from typing import Any

from alex.scheduler import CronManager


class CronService:
    """Thin wrapper around CronManager for scheduling, cancellation, and lifecycle.

    Owns the CronManager instance and exposes a narrow API that Agent
    delegates to.  The ``runner`` callable (Agent.execute_tool_action) is
    passed at schedule time rather than stored, so CronService stays
    decoupled from Agent internals.
    """

    def __init__(self, notify_callback: callable) -> None:
        self._manager = CronManager(notify_callback)

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._manager.bind_event_loop(loop)

    async def start_services(self) -> None:
        await self._manager._ensure_scheduler()

    async def shutdown(self) -> None:
        await self._manager.shutdown()

    async def schedule(
        self,
        *,
        session_id: str,
        name: str,
        cron: str = "",
        interval_seconds: int | None = None,
        repeat: int = 1,
        subscribe: bool = False,
        run_now: bool = False,
        action: str = "",
        params: dict | None = None,
        runner: callable,
    ) -> str:
        return await self._manager.schedule(
            session_id=session_id,
            name=name,
            cron=cron,
            interval_seconds=interval_seconds,
            repeat=repeat,
            subscribe=subscribe,
            run_now=run_now,
            action=action,
            params=params or {},
            runner=runner,
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._manager.list_jobs()

    async def cancel(self, job_id: str) -> bool:
        return await self._manager.cancel(job_id)
