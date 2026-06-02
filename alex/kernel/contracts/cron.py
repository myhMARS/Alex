"""Cron / scheduler contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alex.kernel.bus import Command, Event, Request


@dataclass
class ScheduleCron(Request[str]):
    """Schedule a new cron job.

    Returns ``str`` (job_id).
    """
    cron: str = ""
    prompt: str = ""
    recurring: bool = True
    durable: bool = False


@dataclass
class CancelCron(Request[bool]):
    """Cancel a scheduled cron job.

    Returns ``bool`` (True if cancelled).
    """
    job_id: str = ""


@dataclass
class ListCronJobs(Request[list[dict]]):
    """List all cron jobs.

    Returns ``list[dict]``.
    """


@dataclass
class CronTurnRequested(Command):
    """Published by cron when a scheduled job fires — agent subscribes."""
    trigger: dict[str, Any] = field(default_factory=dict)


@dataclass
class CronJobEvent(Event):
    """Published when a cron job changes status (UI + store notification)."""
    job_id: str = ""
    name: str = ""
    status: str = ""
    prompt: str = ""
    recurring: bool = True
    durable: bool = False
    runs_done: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""
    error: str = ""
    tool_call_id: str = ""
