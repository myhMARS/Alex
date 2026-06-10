import asyncio
import os
from datetime import datetime

import pytest

pytest.importorskip("apscheduler")

from alex.scheduler import CronManager
from alex.bus.events import CronJobEvent
from alex.tools.cron import (
    TOOL_HINT,
    CronCancelInput,
    CronInput,
    create_cron_cancel_tool,
    create_cron_tool,
)


@pytest.mark.asyncio
async def test_cron_manager_runs_and_notifies():
    notes: list = []

    async def runner(session_id: str, job_id: str, name: str, prompt: str, stream_id: str) -> str:
        await asyncio.sleep(0)
        return f"{name}:{prompt}:{stream_id}"

    mgr = CronManager(lambda n: notes.append(n))
    job_id = await mgr.schedule(
        session_id="test-session",
        cron="* * * * *",
        prompt="summarize recent updates",
        recurring=False,
        durable=False,
        runner=runner,
    )
    job = mgr.get_job(job_id)
    assert job is not None
    assert mgr._scheduler is not None
    mgr._scheduler.modify_job(job_id, next_run_time=datetime.now().astimezone())

    end = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < end:
        if any(
            isinstance(n, CronJobEvent) and n.job_id == job_id and n.status in ("SUCCESS", "FAILED")
            for n in notes
        ):
            break
        await asyncio.sleep(0.01)

    jobs = mgr.list_jobs()
    assert any(j["id"] == job_id for j in jobs)
    assert any(isinstance(n, CronJobEvent) and n.job_id == job_id and n.prompt == "summarize recent updates" for n in notes)
    finished_events = [
        n for n in notes
        if isinstance(n, CronJobEvent) and n.job_id == job_id and n.status in ("SUCCESS", "FAILED")
    ]
    assert finished_events


@pytest.mark.asyncio
async def test_one_shot_cron_finishes_after_enqueue():
    notes: list = []
    wait_flags: list[bool] = []
    blocked = asyncio.Event()

    async def runner(
        session_id: str,
        job_id: str,
        name: str,
        prompt: str,
        stream_id: str,
        *,
        wait_until_done: bool = True,
    ) -> str:
        wait_flags.append(wait_until_done)
        if wait_until_done:
            await blocked.wait()
        return "ENQUEUED"

    mgr = CronManager(lambda n: notes.append(n))
    job_id = await mgr.schedule(
        session_id="test-session",
        cron="* * * * *",
        prompt="one shot enqueue",
        recurring=False,
        durable=False,
        runner=runner,
    )
    job = mgr.get_job(job_id)
    assert job is not None
    assert mgr._scheduler is not None
    mgr._scheduler.modify_job(job_id, next_run_time=datetime.now().astimezone())

    end = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < end:
        if any(
            isinstance(n, CronJobEvent) and n.job_id == job_id and n.status == "SUCCESS"
            for n in notes
        ):
            break
        await asyncio.sleep(0.01)

    assert wait_flags == [False]
    assert any(isinstance(n, CronJobEvent) and n.job_id == job_id and n.status == "SUCCESS" for n in notes)


@pytest.mark.asyncio
async def test_cron_manager_restores_durable_jobs(tmp_path):
    notes: list = []

    async def runner(session_id: str, job_id: str, name: str, prompt: str, stream_id: str) -> str:
        await asyncio.sleep(0)
        return prompt.upper()

    mgr = CronManager(lambda n: notes.append(n), storage_dir=tmp_path)
    job_id = await mgr.schedule(
        session_id="restore-session",
        cron="* * * * *",
        prompt="daily check",
        recurring=True,
        durable=True,
        runner=runner,
    )
    await mgr.shutdown()

    restored = CronManager(lambda n: notes.append(n), storage_dir=tmp_path)
    await restored.restore_durable_jobs(runner=runner)
    job = restored.get_job(job_id)

    assert job is not None
    assert job.prompt == "daily check"
    assert job.durable is True


@pytest.mark.asyncio
async def test_cron_manager_persists_durable_jobs_atomically(tmp_path, monkeypatch):
    notes: list = []
    replace_calls: list[tuple[str, str]] = []
    original_replace = os.replace

    def _replace(src: str, dst: str) -> None:
        replace_calls.append((str(src), str(dst)))
        original_replace(src, dst)

    async def runner(session_id: str, job_id: str, name: str, prompt: str, stream_id: str) -> str:
        await asyncio.sleep(0)
        return prompt

    monkeypatch.setattr("alex.scheduler.cron_store.os.replace", _replace)
    mgr = CronManager(lambda n: notes.append(n), storage_dir=tmp_path)
    job_id = await mgr.schedule(
        session_id="atomic-session",
        cron="* * * * *",
        prompt="persist me",
        recurring=True,
        durable=True,
        runner=runner,
    )

    assert replace_calls
    assert str(tmp_path / f"{job_id}.json") in {dst for _, dst in replace_calls}
    assert (tmp_path / f"{job_id}.json").exists()


@pytest.mark.asyncio
async def test_cron_manager_restores_durable_jobs_into_current_session(tmp_path):
    notes: list = []
    seen_session_ids: list[str] = []

    async def runner(session_id: str, job_id: str, name: str, prompt: str, stream_id: str) -> str:
        seen_session_ids.append(session_id)
        await asyncio.sleep(0)
        return prompt.upper()

    mgr = CronManager(lambda n: notes.append(n), storage_dir=tmp_path)
    job_id = await mgr.schedule(
        session_id="old-session",
        cron="* * * * *",
        prompt="daily check",
        recurring=False,
        durable=True,
        runner=runner,
    )
    await mgr.shutdown()

    restored = CronManager(lambda n: notes.append(n), storage_dir=tmp_path)
    await restored.restore_durable_jobs(runner=runner, session_id="current-session")
    await restored._ensure_scheduler()
    job = restored.get_job(job_id)

    assert job is not None
    assert job.session_id == "current-session"

    assert restored._scheduler is not None
    restored._scheduler.modify_job(job_id, next_run_time=datetime.now().astimezone())

    end = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < end:
        if any(
            isinstance(n, CronJobEvent) and n.job_id == job_id and n.status in ("SUCCESS", "FAILED")
            for n in notes
        ):
            break
        await asyncio.sleep(0.01)

    assert seen_session_ids == ["current-session"]


@pytest.mark.asyncio
async def test_cron_tool_schedules_raw_prompt():
    from alex.bus import AsyncEventBus
    from alex.scheduler.module import CronModule

    bus = AsyncEventBus()
    await bus.start()

    class _MockManager:
        def __init__(self):
            self.calls = []
        async def schedule(self, **kwargs):
            self.calls.append(kwargs)
            return "job-1"
        def list_jobs(self):
            return []
        async def restore_durable_jobs(self, *, runner, session_id=""):
            pass

    mgr = _MockManager()
    cron_mod = CronModule(cron_manager=mgr)
    await cron_mod.start(bus)

    tool = create_cron_tool(bus)
    result = await tool.coroutine(
        cron="*/5 * * * *",
        prompt="└─ ✓ 提醒用户：2分钟到了，请去跑测试！",
        recurring=True,
        durable=False,
    )

    assert result == "Scheduled: job-1"
    assert mgr.calls[0]["prompt"] == "└─ ✓ 提醒用户：2分钟到了，请去跑测试！"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cron_cancel_tool_deletes_job():
    from alex.bus import AsyncEventBus
    from alex.scheduler.module import CronModule

    bus = AsyncEventBus()
    await bus.start()

    class _MockManager:
        def __init__(self):
            self.cancelled = []
        async def cancel(self, job_id):
            self.cancelled.append(job_id)
            return True
        def list_jobs(self):
            return []
        async def restore_durable_jobs(self, *, runner, session_id=""):
            pass

    mgr = _MockManager()
    cron_mod = CronModule(cron_manager=mgr)
    await cron_mod.start(bus)

    tool = create_cron_cancel_tool(bus)
    result = await tool.coroutine(job_id="job-1")

    assert result == "Cancelled: job-1"
    assert mgr.cancelled == ["job-1"]
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cron_cancel_tool_requires_job_id():
    from alex.bus import AsyncEventBus
    from alex.scheduler.module import CronModule

    bus = AsyncEventBus()
    await bus.start()
    cron_mod = CronModule()
    await cron_mod.start(bus)

    tool = create_cron_cancel_tool(bus)
    result = await tool.coroutine(job_id="")

    assert result == "Error: job_id is required"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cron_cancel_tool_reports_missing_job():
    from alex.bus import AsyncEventBus
    from alex.scheduler.module import CronModule

    bus = AsyncEventBus()
    await bus.start()

    class _MockManager:
        async def cancel(self, job_id):
            return False
        def list_jobs(self):
            return []
        async def restore_durable_jobs(self, *, runner, session_id=""):
            pass

    cron_mod = CronModule(cron_manager=_MockManager())
    await cron_mod.start(bus)

    tool = create_cron_cancel_tool(bus)
    result = await tool.coroutine(job_id="missing-job")

    assert result == "Error: cron job not found: missing-job"
    await bus.shutdown()

