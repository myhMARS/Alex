import asyncio

import pytest

pytest.importorskip("apscheduler")

from alex.cron import CronManager
from alex.events import CronJobEvent


@pytest.mark.asyncio
async def test_cron_manager_runs_and_notifies():
    notes: list = []

    async def runner(action: str, params: dict) -> str:
        await asyncio.sleep(0)
        return f"{action}:{params.get('x', '')}"

    mgr = CronManager(lambda n: notes.append(n))
    job_id = await mgr.schedule(
        session_id="test-session",
        name="t",
        cron="* * * * *",
        repeat=1,
        subscribe=False,
        run_now=True,
        action="noop",
        params={"x": 1},
        runner=runner,
    )

    end = asyncio.get_running_loop().time() + 0.5
    while asyncio.get_running_loop().time() < end:
        if any(
            isinstance(n, CronJobEvent) and n.job_id == job_id
            for n in notes
        ):
            break
        await asyncio.sleep(0.01)

    jobs = mgr.list_jobs()
    assert any(j["id"] == job_id for j in jobs)
    assert any(isinstance(n, CronJobEvent) and n.job_id == job_id for n in notes)
