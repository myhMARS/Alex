"""Unit tests for CronExecutor — runner normalisation and execute lifecycle."""

import asyncio
import time

import pytest

from alex.scheduler.cron_executor import CronExecutor
from alex.scheduler.manager import CronJob


class TestCronExecutorNormalizeRunner:
    """Tests for runner normalisation."""

    @pytest.mark.asyncio
    async def test_normalize_legacy_runner(self):
        """Legacy runner (5 positional args) is wrapped to accept 6."""
        calls: list[dict] = []

        async def legacy(session_id, job_id, name, prompt, stream_id):
            calls.append({
                "session_id": session_id,
                "job_id": job_id,
                "name": name,
                "prompt": prompt,
                "stream_id": stream_id,
            })
            return "done"

        normalized = CronExecutor.normalize_runner(legacy)
        result = await normalized("s1", "j1", "n1", "p1", "st1", True)
        assert result == "done"
        assert calls[0]["session_id"] == "s1"
        assert calls[0]["stream_id"] == "st1"

    @pytest.mark.asyncio
    async def test_normalize_wait_until_done_runner(self):
        """Runner accepting wait_until_done receives it."""
        flags: list[bool] = []

        async def modern(session_id, job_id, name, prompt, stream_id, *, wait_until_done=True):
            flags.append(wait_until_done)
            return "ok"

        normalized = CronExecutor.normalize_runner(modern)
        result = await normalized("s", "j", "n", "p", "st", False)
        assert result == "ok"
        assert flags == [False]


class TestCronExecutorExecute:
    """Tests for the execute-once lifecycle."""

    @pytest.mark.asyncio
    async def test_execute_success_path(self):
        """Happy path: runner succeeds, job state updated, events emitted."""
        events: list = []
        persisted: list[CronJob] = []
        deleted: list[str] = []
        completed: list[str] = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            return f"result-{prompt}"

        def persist(job): persisted.append(job)
        def delete_persisted(jid): deleted.append(jid)
        def emit(job, *, status, tool_call_id=""): events.append((status, tool_call_id))
        def debug(msg): pass
        def on_complete(jid): completed.append(jid)

        executor = CronExecutor()
        job = CronJob(
            id="j-success", session_id="s1", name="test", cron="* * * * *",
            prompt="hello", recurring=False, durable=True,
        )
        normalized = executor.normalize_runner(runner)

        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=on_complete,
        )

        assert job.runs_done == 1
        assert job.last_result == "result-hello"
        assert job.last_error == ""
        assert job.status == "SUCCESS"
        statuses = [s for s, _ in events]
        assert "RUNNING" in statuses
        assert "SUCCESS" in statuses
        assert "j-success" in deleted
        assert "j-success" in completed

    @pytest.mark.asyncio
    async def test_execute_failure_path(self):
        """Runner raises → job state reflects failure."""
        events: list = []
        persisted: list[CronJob] = []
        deleted: list[str] = []
        completed: list[str] = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            raise ValueError("boom")

        def persist(job): persisted.append(job)
        def delete_persisted(jid): deleted.append(jid)
        def emit(job, *, status, tool_call_id=""): events.append(status)
        def debug(msg): pass
        def on_complete(jid): completed.append(jid)

        executor = CronExecutor()
        job = CronJob(
            id="j-fail", session_id="s1", name="failing", cron="* * * * *",
            prompt="x", recurring=False, durable=True,
        )
        normalized = executor.normalize_runner(runner)

        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=on_complete,
        )

        assert job.runs_done == 1
        assert "ValueError: boom" in job.last_error
        assert job.status == "FAILED"
        assert "RUNNING" in events
        assert "FAILED" in events

    @pytest.mark.asyncio
    async def test_execute_recurring_job_stays_scheduled(self):
        """Recurring job returns to SCHEDULED after run."""
        events: list = []
        persisted: list[CronJob] = []
        deleted: list[str] = []
        completed: list[str] = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            return "ok"

        def persist(job): persisted.append(job)
        def delete_persisted(jid): deleted.append(jid)
        def emit(job, *, status, tool_call_id=""): events.append(status)
        def debug(msg): pass
        def on_complete(jid): completed.append(jid)

        executor = CronExecutor()
        job = CronJob(
            id="j-recur", session_id="s1", name="recurring", cron="*/5 * * * *",
            prompt="x", recurring=True, durable=True,
        )
        normalized = executor.normalize_runner(runner)

        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=on_complete,
        )

        assert job.status == "SCHEDULED"
        assert "j-recur" not in deleted
        assert "j-recur" not in completed  # recurring → no on_complete
        # The *run outcome* event (SUCCESS) is emitted; job.status is SCHEDULED
        assert "SUCCESS" in events

    @pytest.mark.asyncio
    async def test_execute_handles_cancelled_error(self):
        """CancelledError mid-run marks job CANCELLED."""
        events: list = []
        deleted: list[str] = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            raise asyncio.CancelledError()

        def persist(job): pass
        def delete_persisted(jid): deleted.append(jid)
        def emit(job, *, status, tool_call_id=""): events.append(status)
        def debug(msg): pass

        executor = CronExecutor()
        job = CronJob(
            id="j-cancel", session_id="s1", name="x", cron="* * * * *",
            prompt="x", recurring=True, durable=True,
        )
        normalized = executor.normalize_runner(runner)

        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=None,
        )

        assert job.status == "CANCELLED"
        assert "j-cancel" in deleted

    @pytest.mark.asyncio
    async def test_execute_ignores_cancelled_job(self):
        """Job already CANCELLED before execution starts → no-op."""
        events: list = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            pytest.fail("should not be called")

        def persist(job): pass
        def delete_persisted(jid): pass
        def emit(job, *, status, tool_call_id=""): events.append(status)
        def debug(msg): pass

        executor = CronExecutor()
        job = CronJob(
            id="j-precancelled", session_id="s1", name="x", cron="* * * * *",
            prompt="x", recurring=True, status="CANCELLED",
        )
        normalized = executor.normalize_runner(runner)

        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=None,
        )

        # No events emitted (job was already cancelled)
        assert events == []

    @pytest.mark.asyncio
    async def test_cancel_task_stops_running_job(self):
        """cancel_task cancels the underlying asyncio Task."""
        blocked = asyncio.Event()
        events: list = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            events.append("started")
            await blocked.wait()
            events.append("finished")  # should not reach
            return "ok"

        def persist(job): pass
        def delete_persisted(jid): pass
        def emit(job, *, status, tool_call_id=""): pass
        def debug(msg): pass

        executor = CronExecutor()
        job = CronJob(
            id="j-running", session_id="s1", name="x", cron="* * * * *",
            prompt="x", recurring=True, durable=True,
        )
        normalized = executor.normalize_runner(runner)

        # Start execution in background
        task = asyncio.create_task(executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=None,
        ))

        # Wait for runner to be called
        await asyncio.sleep(0.05)
        assert "started" in events

        # Cancel via executor
        executor.cancel_task("j-running")
        blocked.set()

        await asyncio.sleep(0.05)
        assert "finished" not in events  # runner was cancelled
        assert job.status == "CANCELLED"

        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_execute_stream_id_format(self):
        """stream_id follows the cron:<job_id>:<seq> pattern."""
        stream_ids: list[str] = []

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            stream_ids.append(stream_id)
            return "ok"

        def persist(job): pass
        def delete_persisted(jid): pass
        def emit(job, *, status, tool_call_id=""): pass
        def debug(msg): pass
        def on_complete(jid): pass

        executor = CronExecutor()
        job = CronJob(
            id="abc123", session_id="s1", name="test", cron="* * * * *",
            prompt="x", recurring=False, runs_done=2,
        )
        normalized = executor.normalize_runner(runner)

        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit, debug=debug, on_complete=on_complete,
        )

        assert stream_ids == ["cron:abc123:3"]  # runs_done + 1 = 3

    @pytest.mark.asyncio
    async def test_execute_outer_exception_safety_net(self):
        """Even if emit_job_event raises, outer except catches and completes."""
        call_count = [0]

        async def runner(session_id, job_id, name, prompt, stream_id, wait_until_done):
            return "ok"

        def persist(job): pass
        def delete_persisted(jid): pass

        def emit_broken(job, *, status, tool_call_id=""):
            call_count[0] += 1
            if call_count[0] >= 2:  # second call (finalise) raises
                raise RuntimeError("emit explosion")

        def debug(msg): pass

        executor = CronExecutor()
        job = CronJob(
            id="j-safe", session_id="s1", name="x", cron="* * * * *",
            prompt="x", recurring=False, durable=False,
        )
        normalized = executor.normalize_runner(runner)

        # Should not raise despite emit failure
        await executor.execute(
            job, normalized,
            persist=persist, delete_persisted=delete_persisted,
            emit_job_event=emit_broken, debug=debug, on_complete=None,
        )

        assert job.runs_done == 1
