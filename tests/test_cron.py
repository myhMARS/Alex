import asyncio

import pytest

pytest.importorskip("apscheduler")

from alex.cron import CronManager


@pytest.mark.asyncio
async def test_cron_manager_runs_and_notifies():
    notes: list[dict] = []

    async def runner(action: str, params: dict) -> str:
        await asyncio.sleep(0)
        return f"{action}:{params.get('x', '')}"

    mgr = CronManager(lambda n: notes.append(n))
    job_id = await mgr.schedule(
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
        if any(n.get("type") == "cron_job_done" and n.get("job", {}).get("id") == job_id for n in notes):
            break
        await asyncio.sleep(0.01)

    jobs = mgr.list_jobs()
    assert any(j["id"] == job_id for j in jobs)
    assert any(n.get("type") == "cron_job_done" and n.get("job", {}).get("id") == job_id for n in notes)
