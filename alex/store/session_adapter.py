"""Session persistence adapter — wraps alex.session for the store module.

Listens for TurnCompleted events on the bus and auto-persists sessions,
so TUI never calls save directly.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import BaseMessage

from alex.store import session as _session

logger = logging.getLogger(__name__)


class SessionPersistence:
    """Store-side adapter for session save/load/delete.

    Call subscribe(bus) once during startup to enable event-driven auto-save.
    """

    @staticmethod
    def save(session_id: str, messages: list[BaseMessage]) -> None:
        existing = _session.load_session_raw(session_id) or {}
        cron_history = list(existing.get("cron_history", []) or [])
        _session.save_session_bundle(session_id, messages, cron_history)

    @staticmethod
    def load(session_id: str) -> dict[str, Any] | None:
        return _session.load_session_bundle(session_id)

    @staticmethod
    def append_cron_record(session_id: str, record: dict[str, Any]) -> None:
        _session.append_cron_history(session_id, record)

    @staticmethod
    def list_sessions() -> list[dict[str, Any]]:
        metas = _session.list_sessions()
        return [
            {
                "session_id": m.session_id,
                "created_at": m.created_at,
                "first_message": m.first_message,
                "message_count": m.message_count,
            }
            for m in metas
        ]

    @staticmethod
    def delete(session_id: str) -> bool:
        return _session.delete_session(session_id)

    @staticmethod
    async def subscribe(bus) -> None:
        """Register bus handlers so session persistence is event-driven."""
        from alex.bus.events import TurnCompleted, CronJobEvent

        async def _on_turn_completed(event: TurnCompleted) -> None:
            if not event.session_id:
                return
            try:
                SessionPersistence.save(event.session_id, event.messages)
            except Exception:
                logger.warning("Auto-save failed for session %s", event.session_id, exc_info=True)

        async def _on_cron_job_event(event: CronJobEvent) -> None:
            if event.status not in ("SUCCESS", "FAILED") or not event.session_id:
                return
            record = {
                "execution_id": event.tool_call_id or f"cron:{event.job_id}:{event.runs_done}",
                "job_id": event.job_id,
                "name": event.name,
                "status": event.status,
                "prompt": event.prompt,
                "durable": event.durable,
                "recurring": event.recurring,
                "runs_done": event.runs_done,
                "started_at": event.started_at,
                "finished_at": event.finished_at,
                "result": event.result,
                "error": event.error,
            }
            try:
                _session.append_cron_history(event.session_id, record)
            except Exception:
                logger.warning("Cron history persist failed for session %s", event.session_id, exc_info=True)

        await bus.subscribe(TurnCompleted, _on_turn_completed)
        await bus.subscribe(CronJobEvent, _on_cron_job_event)
