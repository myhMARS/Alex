from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from alex.bus.events import CronDebugEvent, CronJobEvent
from alex.config import is_cron_debug_enabled

CRON_DIR = Path.home() / ".alex" / "cron"
NormalizedCronRunner = Callable[[str, str, str, str, str, bool], Awaitable[str]]


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
    if len(parts) != 5:
        raise CronParseError("Cron must have exactly 5 fields: minute hour day month day_of_week")
    return CronTrigger.from_crontab(cron_expr, timezone=tzinfo)


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


def _derive_job_name(prompt: str) -> str:
    first = (prompt or "").strip().splitlines()[0] if (prompt or "").strip() else "cron job"
    return first[:24] or "cron job"


@dataclass
class CronJob:
    id: str
    session_id: str
    name: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = False
    status: str = "SCHEDULED"
    runs_done: int = 0
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_error: str = ""
    last_result: str = ""
    next_run_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronJob":
        return cls(
            id=str(data.get("id", "")).strip() or uuid.uuid4().hex[:12],
            session_id=str(data.get("session_id", "")),
            name=str(data.get("name", "")) or _derive_job_name(str(data.get("prompt", ""))),
            cron=str(data.get("cron", "")).strip(),
            prompt=str(data.get("prompt", "")),
            recurring=bool(data.get("recurring", True)),
            durable=bool(data.get("durable", False)),
            status=str(data.get("status", "SCHEDULED") or "SCHEDULED"),
            runs_done=int(data.get("runs_done", 0) or 0),
            last_started_at=data.get("last_started_at"),
            last_finished_at=data.get("last_finished_at"),
            last_error=str(data.get("last_error", "")),
            last_result=str(data.get("last_result", "")),
            next_run_at=data.get("next_run_at"),
        )


class CronManager:
    def __init__(self, notify: callable, storage_dir: Path | None = None) -> None:
        self._notify_cb = notify
        self._storage_dir = storage_dir or CRON_DIR
        self._loop: asyncio.AbstractEventLoop | None = None
        self._jobs: dict[str, CronJob] = {}
        self._runners: dict[str, callable] = {}
        self._aps_job_ids: dict[str, object] = {}
        self._scheduler = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}

    async def _run_on_bound_loop(self, coro):
        loop = self._loop
        if loop is None:
            return await coro
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        if cur is None or cur is loop:
            return await coro
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(fut)

    @staticmethod
    def _normalize_runner(runner: callable) -> NormalizedCronRunner:
        try:
            params = inspect.signature(runner).parameters
        except (TypeError, ValueError):
            params = {}

        if "wait_until_done" in params:
            async def _wrapped(
                session_id: str,
                job_id: str,
                name: str,
                prompt: str,
                stream_id: str,
                wait_until_done: bool,
            ) -> str:
                return await runner(
                    session_id,
                    job_id,
                    name,
                    prompt,
                    stream_id,
                    wait_until_done=wait_until_done,
                )
            return _wrapped

        async def _legacy(
            session_id: str,
            job_id: str,
            name: str,
            prompt: str,
            stream_id: str,
            wait_until_done: bool,
        ) -> str:
            _ = wait_until_done
            return await runner(session_id, job_id, name, prompt, stream_id)

        return _legacy

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

    def _debug(self, message: str) -> None:
        if not is_cron_debug_enabled():
            return
        self._emit(CronDebugEvent(message=message))

    def _job_path(self, job_id: str) -> Path:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        return self._storage_dir / f"{job_id}.json"

    def _persist_job(self, job: CronJob) -> None:
        if not job.durable:
            return
        path = self._job_path(job.id)
        payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(prefix=".alex.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _delete_persisted_job(self, job_id: str) -> None:
        path = self._job_path(job_id)
        if path.exists():
            path.unlink()

    def _emit_job_event(self, job: CronJob, *, status: str, tool_call_id: str = "") -> None:
        self._emit(CronJobEvent(
            job_id=job.id,
            session_id=job.session_id,
            name=job.name,
            status=status,
            prompt=job.prompt,
            recurring=job.recurring,
            durable=job.durable,
            runs_done=job.runs_done,
            started_at=job.last_started_at,
            finished_at=job.last_finished_at,
            result=job.last_result or "",
            error=job.last_error or "",
            tool_call_id=tool_call_id,
        ))

    async def restore_durable_jobs(self, *, runner: callable, session_id: str = "") -> None:
        normalized_runner = self._normalize_runner(runner)
        if not self._storage_dir.exists():
            return
        for path in sorted(self._storage_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = CronJob.from_dict(data)
            except Exception:
                continue
            if not job.id or not job.cron or not job.prompt:
                continue
            if not job.recurring and job.runs_done > 0:
                self._delete_persisted_job(job.id)
                continue
            if job.status == "RUNNING":
                job.status = "SCHEDULED"
            if session_id:
                job.session_id = session_id
            self._jobs[job.id] = job
            self._runners[job.id] = normalized_runner
            self._persist_job(job)

    async def _ensure_scheduler(self) -> None:
        await self._run_on_bound_loop(self._ensure_scheduler_inner())

    async def _ensure_scheduler_inner(self) -> None:
        if self._scheduler is not None:
            return
        from apscheduler.executors.asyncio import AsyncIOExecutor
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        loop = self._loop or asyncio.get_running_loop()
        self._loop = loop
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
            if not job.recurring and job.runs_done >= 1:
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

    def list_jobs(self) -> list[dict[str, Any]]:
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
                if aps_job is None and job.status == "RUNNING" and not job.recurring and job.runs_done >= 1:
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
        cron: str,
        prompt: str,
        recurring: bool,
        durable: bool,
        runner: callable,
    ) -> str:
        return await self._run_on_bound_loop(self._schedule_inner(
            session_id=session_id,
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            durable=durable,
            runner=runner,
        ))

    async def _schedule_inner(
        self,
        *,
        session_id: str,
        cron: str,
        prompt: str,
        recurring: bool,
        durable: bool,
        runner: callable,
    ) -> str:
        await self._ensure_scheduler_inner()
        cron_str = str(cron or "").strip()
        prompt_text = str(prompt or "").strip()
        if not cron_str:
            raise CronParseError("Provide cron")
        if not prompt_text:
            raise CronParseError("Provide prompt")
        _next_cron_time(time.time(), cron_str)
        normalized_runner = self._normalize_runner(runner)

        job_id = uuid.uuid4().hex[:12]
        job = CronJob(
            id=job_id,
            session_id=session_id,
            name=_derive_job_name(prompt_text),
            cron=cron_str,
            prompt=prompt_text,
            recurring=bool(recurring),
            durable=bool(durable),
        )
        self._jobs[job_id] = job
        self._runners[job_id] = normalized_runner
        self._persist_job(job)
        self._schedule_aps(job, normalized_runner)
        self._aps_job_ids[job_id] = job_id
        self._emit_job_event(job, status=job.status)
        return job_id

    async def cancel(self, job_id: str) -> bool:
        return await self._run_on_bound_loop(self._cancel_inner(job_id))

    async def _cancel_inner(self, job_id: str) -> bool:
        aps_id = self._aps_job_ids.pop(job_id, None)
        job = self._jobs.get(job_id)
        self._runners.pop(job_id, None)
        task = self._running_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
        removed_aps = False
        if aps_id is None and self._scheduler is not None:
            try:
                removed_aps = self._scheduler.get_job(job_id) is not None
            except Exception:
                removed_aps = False
            if removed_aps:
                aps_id = job_id
        if aps_id and self._scheduler is not None:
            try:
                self._scheduler.remove_job(aps_id)
                removed_aps = True
            except Exception:
                pass
        if job:
            job.status = "CANCELLED"
            job.next_run_at = None
            self._delete_persisted_job(job.id)
            self._emit_job_event(job, status=job.status)
        return removed_aps or job is not None

    def _schedule_aps(self, job: CronJob, runner: NormalizedCronRunner) -> str:
        base = datetime.now().astimezone()
        trigger = _build_cron_trigger(job.cron, base.tzinfo)

        async def _run_once_async() -> None:
            self._running_tasks[job.id] = asyncio.current_task()
            run_status = "FAILED"
            run_seq = job.runs_done + 1
            stream_id = f"cron:{job.id}:{run_seq}"
            try:
                sid = job.session_id or ""
                if sid not in self._session_locks:
                    self._session_locks[sid] = asyncio.Lock()
                async with self._session_locks[sid]:
                    if job.status == "CANCELLED":
                        return

                    job.status = "RUNNING"
                    job.last_started_at = time.time()
                    self._persist_job(job)
                    self._emit_job_event(job, status=job.status)

                    try:
                        result = await runner(
                            job.session_id,
                            job.id,
                            job.name,
                            job.prompt,
                            stream_id,
                            job.recurring,
                        )
                        if job.status == "CANCELLED":
                            return
                        job.last_result = str(result)
                        job.last_error = ""
                        run_status = "SUCCESS"
                    except asyncio.CancelledError:
                        job.status = "CANCELLED"
                        job.next_run_at = None
                        self._delete_persisted_job(job.id)
                        return
                    except Exception as e:
                        if job.status == "CANCELLED":
                            return
                        job.last_result = ""
                        job.last_error = f"{type(e).__name__}: {e}"
                        run_status = "FAILED"

                    job.runs_done += 1
                    job.last_finished_at = time.time()

                    done = not job.recurring
                    if done:
                        job.status = run_status
                        job.next_run_at = None
                    else:
                        job.status = "SCHEDULED"
                        job.next_run_at = None

                    if done:
                        self._delete_persisted_job(job.id)
                    else:
                        self._persist_job(job)

                    self._emit_job_event(job, status=run_status, tool_call_id=stream_id)

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
                job.last_finished_at = time.time()
                if not job.recurring:
                    job.status = run_status
                    self._delete_persisted_job(job.id)
                else:
                    job.status = "SCHEDULED"
                    self._persist_job(job)
                self._emit_job_event(job, status=run_status, tool_call_id=stream_id)
            finally:
                self._running_tasks.pop(job.id, None)

        def _run_once() -> None:
            self._debug(f"job_fire job_id={job.id}")
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

        aps_job = self._scheduler.add_job(  # type: ignore[union-attr]
            _run_once,
            id=job.id,
            trigger=trigger,
            replace_existing=True,
            misfire_grace_time=300,
        )
        self._debug(f"job_scheduled job_id={job.id}")
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
