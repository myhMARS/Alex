from __future__ import annotations

from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

from alex.tools.ports import CronScheduler


TOOL_HINT = (
    "Use `cron` to schedule a prompt-driven background task with a standard 5-field "
    "cron expression in local time. Prefer recurring=true for periodic jobs; use "
    "durable=true to keep the job across app restarts. The `prompt` must contain "
    "only the actual task content, not any reminder wrapper, elapsed-time wording, "
    "status announcement, or decorative/display text."
)


class CronInput(BaseModel):
    cron: str = Field(description="Standard 5-field crontab in local time, e.g. '*/5 * * * *'")
    prompt: str = Field(description="Task prompt to execute every time the cron trigger fires. Must contain only the actual task content; do not include reminder wrappers, elapsed-time wording, status text, or decorative/display text")
    recurring: bool = Field(default=True, description="If true, keep running on every matching schedule; if false, run once then delete")
    durable: bool = Field(default=False, description="If true, persist this job to ~/.alex/cron so it survives restarts")


def create_cron_tool(scheduler: CronScheduler) -> StructuredTool:
    async def _cron(
        cron: str = "",
        prompt: str = "",
        recurring: bool = True,
        durable: bool = False,
    ) -> str:
        cron_str = str(cron or "").strip()
        prompt_text = str(prompt or "").strip()
        if not cron_str:
            return "Error: cron is required"
        if not prompt_text:
            return "Error: prompt is required"

        job_id = await scheduler.schedule_cron_job(
            cron=cron_str,
            prompt=prompt_text,
            recurring=bool(recurring),
            durable=bool(durable),
        )
        return f"Scheduled: {job_id}"

    return StructuredTool.from_function(
        coroutine=_cron,
        name="cron",
        description=(
            "Schedule a prompt-driven background cron job. "
            "Uses a standard 5-field cron expression in local time and can be "
            "made durable across restarts. The prompt must be the real task "
            "content only, without reminder wrappers, elapsed-time phrasing, "
            "status text, or decorative/display text."
        ),
        args_schema=CronInput,
    )
