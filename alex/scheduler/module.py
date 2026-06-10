"""CronModule — cron job scheduling via the message bus.

Phase 2: wraps existing CronManager (three-way split).
Provides ScheduleCron / CancelCron.
Publishes CronTurnRequested.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from alex.scheduler.manager import CronManager
from alex.kernel.contracts.chat import TurnCompleted, TurnStarted
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
        self._pending_futures: dict[str, asyncio.Future] = {}

    async def start(self, bus: Any) -> None:
        self._bus = bus
        if self._manager is None:
            self._manager = CronManager(notify=lambda e: self._bus.publish(e) if self._bus else None)
        await self._manager.restore_durable_jobs(runner=self._cron_runner, session_id=self._session_id)
        await bus.subscribe(TurnStarted, self._on_turn_started)
        await bus.subscribe(TurnCompleted, self._on_turn_completed)
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
        for future in self._pending_futures.values():
            if not future.done():
                future.cancel()
        self._pending_futures.clear()
        self._bus = None

    # ── TurnStarted → track current session ──────────────────────────────

    async def _on_turn_started(self, event: TurnStarted) -> None:
        if event.session_id:
            self._session_id = event.session_id

    # ── TurnCompleted → resolve cron pending futures ──────────────────────

    async def _on_turn_completed(self, event: TurnCompleted) -> None:
        """当 cron turn 完成时，resolve 对应的 Future。"""
        stream_id = getattr(event, "stream_id", "") or ""
        if not stream_id or not stream_id.startswith("cron:"):
            return
        future = self._pending_futures.pop(stream_id, None)
        if future is not None and not future.done():
            future.set_result(event.content or "")

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

    # ── cron runner（通过 bus 发布 CronTurnRequested，等待 turn 完成）──

    async def _cron_runner(
        self, session_id: str, job_id: str, name: str,
        prompt: str, stream_id: str, _wait_until_done: bool = True,
    ) -> str:
        """Cron 触发时的 runner — 发布事件并等待 TurnCompleted 后返回结果。"""
        if not _wait_until_done:
            self._bus.publish(CronTurnRequested(
                session_id=session_id,
                trigger={
                    "job_id": job_id,
                    "name": name,
                    "prompt": prompt,
                    "stream_id": stream_id,
                },
            ))
            return "ENQUEUED"

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_futures[stream_id] = future

        self._bus.publish(CronTurnRequested(
            session_id=session_id,
            trigger={
                "job_id": job_id,
                "name": name,
                "prompt": prompt,
                "stream_id": stream_id,
            },
        ))

        try:
            return await asyncio.wait_for(future, timeout=600.0)
        except asyncio.TimeoutError:
            return "Error: cron turn timed out after 600s"
        except asyncio.CancelledError:
            return "Error: cron turn cancelled"
        finally:
            self._pending_futures.pop(stream_id, None)

    @property
    def manager(self) -> Any:
        return self._manager
