from __future__ import annotations

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool


TOOL_HINT = "Use `cron` to schedule background jobs (web_search/web_fetch/time/notify) using either interval_seconds or a crontab expression (5 fields, or 6 fields with seconds); enable subscribe=true to let the agent reply to each run result."


class CronInput(BaseModel):
    name: str = Field(default="job", description="Job name shown in the status bar")
    interval_seconds: int | None = Field(default=None, ge=1, description="Run every N seconds (alternative to cron)")
    cron: str = Field(default="", description="Crontab (5 fields like '*/5 * * * *', or 6 fields with seconds like '*/10 * * * * *')")
    repeat: int = Field(default=1, ge=0, description="How many times to run. 0 means run forever.")
    subscribe: bool = Field(default=False, description="If true, post each run result back into the agent chat session")
    run_now: bool = Field(default=False, description="If true, run once immediately, then continue by interval/cron")
    action: str = Field(description="Action: web_search | web_fetch | time | notify | cancel")
    params: dict = Field(default_factory=dict, description="Action params or cancel target: {'id': '...'}")


def create_cron_tool(agent) -> StructuredTool:
    async def _cron(
        name: str = "job",
        interval_seconds: int | None = None,
        cron: str = "",
        repeat: int = 1,
        subscribe: bool = False,
        run_now: bool = False,
        action: str = "",
        params: dict | None = None,
    ) -> str:
        action = (action or "").strip()
        params = params or {}

        if action == "cancel":
            target = str(params.get("id", "")).strip()
            if not target:
                return "Error: cancel requires params.id"
            ok = await agent.cancel_cron_job(target)
            return "Cancelled" if ok else f"Not found: {target}"

        cron_str = str(cron or "").strip()
        iv = int(interval_seconds) if interval_seconds is not None else None
        if iv is not None and cron_str:
            return "Error: provide either interval_seconds or cron, not both"
        if iv is None and not cron_str:
            return "Error: provide interval_seconds or cron"

        job_id = await agent._cron.schedule(
            session_id=agent.session_id,
            name=name,
            cron=cron_str,
            interval_seconds=iv,
            repeat=int(repeat),
            subscribe=bool(subscribe),
            run_now=bool(run_now),
            action=action,
            params=params,
            runner=agent._run_cron_action,
        )
        return f"Scheduled: {job_id}"

    return StructuredTool.from_function(
        coroutine=_cron,
        name="cron",
        description=(
            "Schedule a background recurring job. "
            "The job runs independently and posts status updates to the agent. "
            "Use action='cancel' with params={'id': ...} to cancel a job."
        ),
        args_schema=CronInput,
    )
