import asyncio
from datetime import datetime

import pytest

pytest.importorskip("apscheduler")

from alex.scheduler import CronManager
from alex.bus.events import CronJobEvent
from alex.tools.cron import TOOL_HINT, CronInput, create_cron_tool


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
    class _SchedulerStub:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def schedule_cron_job(self, **kwargs) -> str:
            self.calls.append(kwargs)
            return "job-1"

    scheduler = _SchedulerStub()
    tool = create_cron_tool(scheduler)
    result = await tool.coroutine(
        cron="*/5 * * * *",
        prompt="└─ ✓ 提醒用户：2分钟到了，请去跑测试！",
        recurring=True,
        durable=False,
    )

    assert result == "Scheduled: job-1"
    assert scheduler.calls[0]["prompt"] == "└─ ✓ 提醒用户：2分钟到了，请去跑测试！"


def test_cron_tool_prompt_guidance_restricts_wrapper_text():
    prompt_desc = CronInput.model_fields["prompt"].description or ""
    assert "actual task content" in TOOL_HINT
    assert "不要" in prompt_desc or "do not include" in prompt_desc
