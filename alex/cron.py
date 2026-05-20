from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alex.events import CronDebugEvent, CronJobEvent


class CronParseError(ValueError):
    pass


def _build_cron_trigger(cron_expr: str, tzinfo) -> object:
    cron_expr = (cron_expr or "").strip()
    if not cron_expr:
        raise CronParseError("Empty cron expression")
    try:
        from apscheduler.triggers.cron import CronTrigger
    except Exception as e:
        raise CronParseError(f"apscheduler is required: {type(e).__name__}: {e}") from e

    parts = cron_expr.split()
    if len(parts) == 5:
        return CronTrigger.from_crontab(cron_expr, timezone=tzinfo)
    if len(parts) == 6:
        second, minute, hour, day, month, dow = parts
        return CronTrigger(
            second=second,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone=tzinfo,
        )
    raise CronParseError("Cron must have 5 (min) or 6 (sec) fields")


def _build_interval_trigger(interval_seconds: int, tzinfo) -> object:
    try:
        from apscheduler.triggers.interval import IntervalTrigger
    except Exception as e:
        raise CronParseError(f"apscheduler is required: {type(e).__name__}: {e}") from e
    if interval_seconds <= 0:
        raise CronParseError("interval_seconds must be >= 1")
    return IntervalTrigger(seconds=interval_seconds, timezone=tzinfo)


def _build_trigger(*, cron_expr: str, interval_seconds: int | None, tzinfo) -> object:
    if interval_seconds is not None:
        if (cron_expr or "").strip():
            raise CronParseError("Provide either interval_seconds or cron")
        return _build_interval_trigger(int(interval_seconds), tzinfo)
    return _build_cron_trigger(cron_expr, tzinfo)


def _next_cron_time(after_ts: float, cron_expr: str) -> float:
    cron_expr = (cron_expr or "").strip()
    if not cron_expr:
        raise CronParseError("Empty cron expression")

    base = datetime.fromtimestamp(after_ts).astimezone()
    trigger = _build_cron_trigger(cron_expr, base.tzinfo)
    nxt = trigger.get_next_fire_time(None, base)
    if nxt is None:
        raise CronParseError("No next fire time")
    return float(nxt.timestamp())


@dataclass
class CronJob:
    id: str
    session_id: str
    name: str
    cron: str
    interval_seconds: int | None
    repeat: int
    subscribe: bool
    run_now: bool
    action: str
    params: dict
    status: str = "SCHEDULED"
    runs_done: int = 0
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_error: str = ""
    last_result: str = ""
    next_run_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "name": self.name,
            "cron": self.cron,
            "interval_seconds": self.interval_seconds,
            "repeat": self.repeat,
            "subscribe": self.subscribe,
            "run_now": self.run_now,
            "action": self.action,
            "params": self.params,
            "status": self.status,
            "runs_done": self.runs_done,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "next_run_at": self.next_run_at,
        }

class CronManager:
    def __init__(self, notify: callable) -> None:
        self._notify_cb = notify
        self._loop: asyncio.AbstractEventLoop | None = None
        self._jobs: dict[str, CronJob] = {}
        self._runners: dict[str, callable] = {}
        self._aps_job_ids: dict[str, object] = {}
        self._scheduler = None
        self._lock: asyncio.Lock | None = None

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        sch = self._scheduler
        if sch is None:
            return
        try:
            bound = getattr(sch, "_eventloop", None)
        except Exception:
            bound = None
        if bound is not None and bound is not loop:
            try:
                sch.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
            self._aps_job_ids.clear()

    def _emit(self, event: Any) -> None:
        loop = self._loop
        if loop is None:
            self._notify_cb(event)
            return
        try:
            loop.call_soon_threadsafe(self._notify_cb, event)
        except Exception:
            self._notify_cb(event)

    def _debug(self, message: str, *, job: CronJob | None = None) -> None:
        if os.environ.get("ALEX_CRON_DEBUG", "").strip().lower() not in ("1", "true", "yes", "on"):
            return
        self._emit(CronDebugEvent(message=message))

    async def _ensure_scheduler(self) -> None:
        loop = self._loop
        if loop is not None:
            try:
                cur = asyncio.get_running_loop()
            except RuntimeError:
                cur = None
            if cur is not None and cur is not loop:
                fut = asyncio.run_coroutine_threadsafe(self._ensure_scheduler_inner(), loop)
                await asyncio.wrap_future(fut)
                return
        await self._ensure_scheduler_inner()

    async def _ensure_scheduler_inner(self) -> None:
        if self._scheduler is not None:
            return
        from apscheduler.executors.asyncio import AsyncIOExecutor
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
        if self._lock is None:
            self._lock = asyncio.Lock()
        self._scheduler = AsyncIOScheduler(
            event_loop=loop,
            executors={"default": AsyncIOExecutor()},
            job_defaults={"misfire_grace_time": 300, "coalesce": True, "max_instances": 1},
        )
        try:
            def _on_event(ev) -> None:
                et = getattr(ev, "code", None)
                jid = getattr(ev, "job_id", "")
                msg = f"event={et} job_id={jid}"
                exc = getattr(ev, "exception", None)
                if exc:
                    msg += f" exc={type(exc).__name__}: {exc}"
                self._debug(msg)

            self._scheduler.add_listener(_on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        except Exception:
            pass
        self._scheduler.start()
        self._debug("scheduler_started")
        for job_id, job in list(self._jobs.items()):
            if job.status == "CANCELLED":
                continue
            if job.repeat > 0 and job.runs_done >= job.repeat:
                continue
            runner = self._runners.get(job_id)
            if runner is None:
                continue
            try:
                self._schedule_aps(job, runner)
                self._aps_job_ids[job_id] = job_id
            except Exception:
                pass

    async def shutdown(self) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass
        self._scheduler = None
        self._aps_job_ids.clear()
        self._runners.clear()
        self._loop = None

    def list_jobs(self) -> list[dict]:
        if self._scheduler is not None:
            for jid, job in self._jobs.items():
                aps_id = self._aps_job_ids.get(jid) or jid
                try:
                    aps_job = self._scheduler.get_job(aps_id)
                except Exception:
                    aps_job = None
                try:
                    nr = getattr(aps_job, "next_run_time", None) if aps_job else None
                    if nr is not None:
                        job.next_run_at = float(nr.timestamp())
                except Exception:
                    pass
                if aps_job is None and job.status == "RUNNING":
                    done = job.repeat > 0 and job.runs_done >= job.repeat
                    if done:
                        job.status = "FAILED" if job.last_error else "SUCCESS"
                        job.next_run_at = None

        jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: (j.status not in ("RUNNING", "SCHEDULED"), j.next_run_at or 0))
        return [j.to_dict() for j in jobs]

    def get_job(self, job_id: str) -> CronJob | None:
        return self._jobs.get(job_id)

    async def schedule(
        self,
        *,
        session_id: str,
        name: str,
        cron: str,
        interval_seconds: int | None = None,
        repeat: int,
        subscribe: bool,
        run_now: bool,
        action: str,
        params: dict,
        runner: callable,
    ) -> str:
        loop = self._loop
        if loop is not None:
            try:
                cur = asyncio.get_running_loop()
            except RuntimeError:
                cur = None
            if cur is not None and cur is not loop:
                fut = asyncio.run_coroutine_threadsafe(
                    self._schedule_inner(
                        name=name,
                        session_id=session_id,
                        cron=cron,
                        interval_seconds=interval_seconds,
                        repeat=repeat,
                        subscribe=subscribe,
                        run_now=run_now,
                        action=action,
                        params=params,
                        runner=runner,
                    ),
                    loop,
                )
                return await asyncio.wrap_future(fut)
        return await self._schedule_inner(
            name=name,
            session_id=session_id,
            cron=cron,
            interval_seconds=interval_seconds,
            repeat=repeat,
            subscribe=subscribe,
            run_now=run_now,
            action=action,
            params=params,
            runner=runner,
        )

    async def _schedule_inner(
        self,
        *,
        session_id: str,
        name: str,
        cron: str,
        interval_seconds: int | None,
        repeat: int,
        subscribe: bool,
        run_now: bool,
        action: str,
        params: dict,
        runner: callable,
    ) -> str:
        await self._ensure_scheduler_inner()
        job_id = uuid.uuid4().hex[:12]
        cron_str = (cron or "").strip()
        iv = int(interval_seconds) if interval_seconds is not None else None
        if iv is not None and cron_str:
            raise CronParseError("Provide either interval_seconds or cron")
        if iv is None and not cron_str:
            raise CronParseError("Provide interval_seconds or cron")

        job = CronJob(
            id=job_id,
            session_id=session_id,
            name=name,
            cron=cron_str,
            interval_seconds=iv,
            repeat=repeat,
            subscribe=subscribe,
            run_now=run_now,
            action=action,
            params=params,
        )
        self._jobs[job_id] = job
        self._runners[job_id] = runner
        self._schedule_aps(job, runner)
        self._aps_job_ids[job_id] = job_id
        self._emit(CronJobEvent(
            job_id=job.id,
            session_id=job.session_id,
            name=job.name,
            status=job.status,
            action=job.action,
            params=dict(job.params),
            runs_done=job.runs_done,
            started_at=job.last_started_at,
            finished_at=job.last_finished_at,
        ))
        return job_id

    async def cancel(self, job_id: str) -> bool:
        loop = self._loop
        if loop is not None:
            try:
                cur = asyncio.get_running_loop()
            except RuntimeError:
                cur = None
            if cur is not None and cur is not loop:
                fut = asyncio.run_coroutine_threadsafe(self._cancel_inner(job_id), loop)
                return await asyncio.wrap_future(fut)
        return await self._cancel_inner(job_id)

    async def _cancel_inner(self, job_id: str) -> bool:
        aps_id = self._aps_job_ids.pop(job_id, None) or job_id
        job = self._jobs.get(job_id)
        self._runners.pop(job_id, None)
        if self._scheduler is None:
            if job:
                job.status = "CANCELLED"
                job.next_run_at = None
                self._emit(CronJobEvent(
                    job_id=job.id,
                    session_id=job.session_id,
                    name=job.name,
                    status=job.status,
                    action=job.action,
                    params=dict(job.params),
                    runs_done=job.runs_done,
                    started_at=job.last_started_at,
                    finished_at=job.last_finished_at,
                ))
            return job is not None
        if aps_id and self._scheduler is not None:
            try:
                self._scheduler.remove_job(aps_id)
            except Exception:
                pass
        if job:
            job.status = "CANCELLED"
            job.next_run_at = None
            self._emit(CronJobEvent(
                job_id=job.id,
                session_id=job.session_id,
                name=job.name,
                status=job.status,
                action=job.action,
                params=dict(job.params),
                runs_done=job.runs_done,
                started_at=job.last_started_at,
                finished_at=job.last_finished_at,
            ))
        return aps_id is not None or job is not None

    def _schedule_aps(self, job: CronJob, runner: callable) -> str:
        base = datetime.now().astimezone()
        trigger = _build_trigger(cron_expr=job.cron, interval_seconds=job.interval_seconds, tzinfo=base.tzinfo)

        async def _run_once_async() -> None:
            run_status = "FAILED"
            try:
                lock = self._lock
                if lock is None:
                    lock = asyncio.Lock()
                    self._lock = lock
                async with lock:
                    if job.status == "CANCELLED":
                        return

                    job.status = "RUNNING"
                    job.last_started_at = time.time()
                    self._emit(CronJobEvent(
                        job_id=job.id,
                        session_id=job.session_id,
                        name=job.name,
                        status=job.status,
                        action=job.action,
                        params=dict(job.params),
                        runs_done=job.runs_done,
                        started_at=job.last_started_at,
                        finished_at=job.last_finished_at,
                    ))

                    try:
                        result = await runner(job.action, job.params)
                        job.last_result = str(result)
                        job.last_error = ""
                        run_status = "SUCCESS"
                    except Exception as e:
                        job.last_result = ""
                        job.last_error = f"{type(e).__name__}: {e}"
                        run_status = "FAILED"

                    job.runs_done += 1
                    run_seq = job.runs_done
                    job.last_finished_at = time.time()

                    done = job.repeat > 0 and job.runs_done >= job.repeat
                    if done:
                        job.status = run_status
                        job.next_run_at = None
                    else:
                        job.status = "SCHEDULED"
                        job.next_run_at = None

                    self._emit(CronJobEvent(
                        job_id=job.id,
                        session_id=job.session_id,
                        name=job.name,
                        status=run_status,
                        subscribe=job.subscribe,
                        action=job.action,
                        params=dict(job.params),
                        runs_done=job.runs_done,
                        started_at=job.last_started_at,
                        finished_at=job.last_finished_at,
                        result=job.last_result or "",
                        error=job.last_error or "",
                        tool_call_id=f"cron:{job.id}:{run_seq}",
                    ))

                    if done:
                        aps_id = self._aps_job_ids.pop(job.id, None) or job.id
                        self._runners.pop(job.id, None)
                        if self._scheduler is not None:
                            try:
                                self._scheduler.remove_job(aps_id)
                            except Exception:
                                pass
            except Exception as e:
                job.last_result = ""
                job.last_error = f"{type(e).__name__}: {e}"
                job.runs_done += 1
                run_seq = job.runs_done
                job.last_finished_at = time.time()
                done = job.repeat > 0 and job.runs_done >= job.repeat
                if done:
                    job.status = run_status
                    job.next_run_at = None
                else:
                    job.status = "SCHEDULED"
                    job.next_run_at = None
                self._emit(CronJobEvent(
                    job_id=job.id,
                    session_id=job.session_id,
                    name=job.name,
                    status=run_status,
                    subscribe=job.subscribe,
                    action=job.action,
                    params=dict(job.params),
                    runs_done=job.runs_done,
                    started_at=job.last_started_at,
                    finished_at=job.last_finished_at,
                    result=job.last_result or "",
                    error=job.last_error or "",
                    tool_call_id=f"cron:{job.id}:{run_seq}",
                ))
                if done:
                    aps_id = self._aps_job_ids.pop(job.id, None) or job.id
                    self._runners.pop(job.id, None)
                    if self._scheduler is not None:
                        try:
                            self._scheduler.remove_job(aps_id)
                        except Exception:
                            pass

        def _run_once() -> None:
            self._debug("job_fire", job=job)
            loop = self._loop
            if loop is None:
                try:
                    asyncio.create_task(_run_once_async())
                except Exception:
                    return
                return
            try:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(_run_once_async()))
            except Exception:
                try:
                    asyncio.create_task(_run_once_async())
                except Exception:
                    return

        next_run_time = None
        if job.run_now:
            next_run_time = datetime.now().astimezone()
            job.run_now = False

        add_kwargs = {
            "id": job.id,
            "trigger": trigger,
            "replace_existing": True,
            "misfire_grace_time": 300,
        }
        if next_run_time is not None:
            add_kwargs["next_run_time"] = next_run_time

        aps_job = self._scheduler.add_job(_run_once, **add_kwargs)  # type: ignore[union-attr]
        self._debug("job_scheduled", job=job)
        try:
            nr = getattr(aps_job, "next_run_time", None)
            if nr is None:
                nr = trigger.get_next_fire_time(None, base)
            if nr is not None:
                job.next_run_at = float(nr.timestamp())
        except Exception:
            pass
        return aps_job.id

    def _next_run_at(self, job: CronJob, after_ts: float) -> float:
        return _next_cron_time(after_ts, job.cron)
