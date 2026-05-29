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

    async def start_services(self, *, runner: callable, session_id: str = "") -> None:
        await self._manager.restore_durable_jobs(runner=runner, session_id=session_id)
        await self._manager._ensure_scheduler()

    async def shutdown(self) -> None:
        await self._manager.shutdown()

    async def schedule(
        self,
        *,
        session_id: str,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
        runner: callable,
    ) -> str:
        return await self._manager.schedule(
            session_id=session_id,
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            durable=durable,
            runner=runner,
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._manager.list_jobs()

    async def cancel(self, job_id: str) -> bool:
        return await self._manager.cancel(job_id)
