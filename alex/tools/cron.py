"""Cron 工具 — 通过 bus request 与 CronModule 交互。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from alex.kernel.contracts.cron import CancelCron, ScheduleCron
from alex.tools.models import AlexTool


TOOL_HINT = (
    "Use `cron` to schedule a prompt-driven background task with a standard 5-field "
    "cron expression in local time. Prefer recurring=true for periodic jobs; use "
    "durable=true to keep the job across app restarts. The `prompt` must contain "
    "only the actual task content, not any reminder wrapper, elapsed-time wording, "
    "status announcement, or decorative/display text."
)
TOOL_HINT_CANCEL = (
    "Use `cron_cancel` to delete or cancel an existing scheduled cron job by its "
    "`job_id`. Prefer calling `cron_jobs` first when you need to inspect current "
    "job ids before deleting one."
)


class CronInput(BaseModel):
    cron: str = Field(description="Standard 5-field crontab in local time, e.g. '*/5 * * * *'")
    prompt: str = Field(description="Task prompt to execute every time the cron trigger fires. Must contain only the actual task content; do not include reminder wrappers, elapsed-time wording, status text, or decorative/display text")
    recurring: bool = Field(default=True, description="If true, keep running on every matching schedule; if false, run once then delete")
    durable: bool = Field(default=False, description="If true, persist this job to ~/.alex/cron so it survives restarts")


class CronCancelInput(BaseModel):
    job_id: str = Field(description="Cron job id to cancel and delete")


def create_cron_tool(bus: Any) -> AlexTool:
    """创建 cron 调度工具 — 通过 bus request(ScheduleCron) 与 CronModule 交互。"""

    async def _cron(
        cron: str = "",
        prompt: str = "",
        recurring: bool = True,
        durable: bool = False,
        _session_id: str = "",
    ) -> str:
        cron_str = str(cron or "").strip()
        prompt_text = str(prompt or "").strip()
        if not cron_str:
            return "Error: cron is required"
        if not prompt_text:
            return "Error: prompt is required"

        job_id = await bus.request(ScheduleCron(
            session_id=_session_id,
            cron=cron_str,
            prompt=prompt_text,
            recurring=bool(recurring),
            durable=bool(durable),
        ))
        return f"Scheduled: {job_id}"

    return AlexTool.from_function(
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


def create_cron_cancel_tool(bus: Any) -> AlexTool:
    """创建 cron 取消工具 — 通过 bus request(CancelCron) 与 CronModule 交互。"""

    async def _cron_cancel(job_id: str = "") -> str:
        target = str(job_id or "").strip()
        if not target:
            return "Error: job_id is required"

        cancelled = await bus.request(CancelCron(job_id=target))
        if not cancelled:
            return f"Error: cron job not found: {target}"
        return f"Cancelled: {target}"

    return AlexTool.from_function(
        coroutine=_cron_cancel,
        name="cron_cancel",
        description=(
            "Cancel and delete an existing scheduled cron job by job id. "
            "Use `cron_jobs` first if you need to inspect available job ids."
        ),
        args_schema=CronCancelInput,
    )
