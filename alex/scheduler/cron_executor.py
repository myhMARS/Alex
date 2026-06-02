"""CronExecutor — runner normalisation and execute-once lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Callable
from alex.scheduler.manager import CronJob, NormalizedCronRunner


class CronExecutor:
    """Encapsulates runner normalisation and the per-job execution lifecycle.

    Owns the session-level locks and the running-task registry so that
    concurrent cron firings for the same session are serialised and
    cancellations are tracked correctly.
    """

    def __init__(self) -> None:
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}

    # ── runner normalisation ──────────────────────────────────────────

    @staticmethod
    def normalize_runner(runner: Callable[..., Any]) -> NormalizedCronRunner:
        """Wrap *runner* so it always receives the six standard positional args.

        If the underlying runner accepts ``wait_until_done`` as a keyword,
        it is forwarded; otherwise the parameter is silently dropped so
        legacy runners (5 positional args) continue to work.
        """
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

    # ── execution ─────────────────────────────────────────────────────

    async def execute(
        self,
        job: CronJob,
        runner: NormalizedCronRunner,
        *,
        persist: Callable[[CronJob], None],
        delete_persisted: Callable[[str], None],
        emit_job_event: Callable[..., None],
        debug: Callable[[str], None],
        on_complete: Callable[[str], None] | None = None,
    ) -> None:
        """Execute *runner* for *job* with full lifecycle management.

        Parameters
        ----------
        job:
            The cron job to execute (mutated in-place).
        runner:
            A normalised runner callable (see :meth:`normalize_runner`).
        persist:
            Called to persist *job* to durable storage.
        delete_persisted:
            Called to remove *job* from durable storage.
        emit_job_event:
            Called as ``emit_job_event(job, status=..., tool_call_id=...)``.
        debug:
            Called with a debug message string.
        on_complete:
            If set, called with *job.id* when a non-recurring job finishes
            so the scheduler can clean up APScheduler / runner registries.
        """
        task = asyncio.current_task()
        if task is not None:
            self._running_tasks[job.id] = task
        run_status = "FAILED"
        run_seq = job.runs_done + 1
        stream_id = f"cron:{job.id}:{run_seq}"
        finalised = False  # guards against double-increment in outer except

        debug(f"executor_start job_id={job.id} run_seq={run_seq}")

        try:
            sid = job.session_id or ""
            if sid not in self._session_locks:
                self._session_locks[sid] = asyncio.Lock()

            async with self._session_locks[sid]:
                if job.status == "CANCELLED":
                    return

                # ── mark RUNNING ──────────────────────────────────
                job.status = "RUNNING"
                job.last_started_at = time.time()
                persist(job)
                emit_job_event(job, status=job.status)

                # ── invoke runner ──────────────────────────────────
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
                    delete_persisted(job.id)
                    return
                except Exception as e:
                    if job.status == "CANCELLED":
                        return
                    job.last_result = ""
                    job.last_error = f"{type(e).__name__}: {e}"
                    run_status = "FAILED"

                # ── finalise ───────────────────────────────────────
                job.runs_done += 1
                job.last_finished_at = time.time()
                finalised = True

                done = not job.recurring
                if done:
                    job.status = run_status
                    job.next_run_at = None
                else:
                    job.status = "SCHEDULED"
                    job.next_run_at = None

                if done:
                    delete_persisted(job.id)
                else:
                    persist(job)

                emit_job_event(job, status=run_status, tool_call_id=stream_id)

                if done and on_complete is not None:
                    on_complete(job.id)

        except Exception as e:
            # ── outer safety net ───────────────────────────────────
            job.last_result = ""
            job.last_error = f"{type(e).__name__}: {e}"
            if not finalised:
                job.runs_done += 1
                job.last_finished_at = time.time()
            if not job.recurring:
                job.status = run_status
                delete_persisted(job.id)
            else:
                job.status = "SCHEDULED"
                persist(job)
            try:
                emit_job_event(job, status=run_status, tool_call_id=stream_id)
            except Exception:
                pass  # best-effort — state is already persisted
        finally:
            _ = self._running_tasks.pop(job.id, None)

    # ── helpers for CronManager ──────────────────────────────────────

    def cancel_task(self, job_id: str) -> None:
        """Cancel a running task for *job_id* if it exists."""
        task = self._running_tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
