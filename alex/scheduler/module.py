"""CronModule — cron job scheduling via the message bus.

Phase 2: wraps existing CronManager (three-way split).
Provides ScheduleCron / CancelCron.
Publishes CronTurnRequested.
"""

from __future__ import annotations

import logging
from typing import Any

from alex.scheduler.manager import CronManager
from alex.kernel.contracts.cron import (
    CancelCron,
    CronTurnRequested,
    ListCronJobs,
    ScheduleCron,
)

logger = logging.getLogger(__name__)


class CronModule:
    """Pluggable cron module — manages scheduled jobs via the bus."""

    name = "cron"
    dependencies: list[str] = []

    def __init__(self, cron_manager: Any = None) -> None:
        self._manager = cron_manager
        self._bus: Any = None
        self._session_id: str = ""

    async def start(self, bus: Any) -> None:
        self._bus = bus
        bus.provide(ScheduleCron, self._handle_schedule)
        bus.provide(CancelCron, self._handle_cancel)
        bus.provide(ListCronJobs, self._handle_list_jobs)
        logger.info("CronModule started (provides ScheduleCron/CancelCron/ListCronJobs)")

    async def stop(self) -> None:
        if self._manager is not None:
            try:
                await self._manager.shutdown()
            except Exception:
                pass
        self._bus = None

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    # ── request handlers ─────────────────────────────────────────────────

    async def _handle_schedule(self, req: ScheduleCron) -> str:
        """Schedule a new cron job. Returns the job_id."""
        if self._manager is None:
            self._manager = CronManager(notify=lambda e: self._bus.publish(e) if self._bus else None)

        job_id = await self._manager.schedule(
            session_id=req.session_id or self._session_id,
            cron=req.cron,
            prompt=req.prompt,
            recurring=req.recurring,
            durable=req.durable,
            runner=self._cron_runner,
        )

        return job_id

    async def _handle_cancel(self, req: CancelCron) -> bool:
        """Cancel a scheduled job. Returns True if successful."""
        if self._manager is None:
            return False

        result = await self._manager.cancel(req.job_id)

        return result

    async def _handle_list_jobs(self, _req: ListCronJobs) -> list[dict]:
        """List all cron jobs."""
        if self._manager is None:
            return []
        return self._manager.list_jobs()

    # ── cron runner（通过 bus 发布 CronTurnRequested 触发 agent 执行）──

    async def _cron_runner(
        self, session_id: str, job_id: str, name: str,
        prompt: str, stream_id: str, _wait_until_done: bool = True,
    ) -> str:
        """Cron 触发时的 runner — 发布 CronTurnRequested 到 bus。"""
        self._bus.publish(CronTurnRequested(
            session_id=session_id,
            trigger={
                "job_id": job_id,
                "name": name,
                "prompt": prompt,
                "stream_id": stream_id,
            },
        ))
        return "TRIGGERED"

    @property
    def manager(self) -> Any:
        return self._manager
