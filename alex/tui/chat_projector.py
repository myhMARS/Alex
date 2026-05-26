"""ChatProjector — bus events → widget updates and cron renderer management.

Extracted from ChatControllerMixin so the TUI controller no longer owns
cron stream handlers, renderer lifecycle, status bar refresh, or the
cron history read model assembly.
"""

from __future__ import annotations

import time
from datetime import datetime

from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from alex.bus.events import (
    CronBatch,
    CronDebugEvent,
    CronDone,
    CronError,
    CronJobEvent,
    SkillReflectErrorEvent,
    SkillReflectEvent,
    ThinkingUpdated,
    TokenEmitted,
    ToolFinished,
    ToolStarted,
)
from alex.tui.presenter import AlexBubble, SystemBubble
from alex.tui.stream_renderer import StreamRenderer
from alex.tui.view_state import SessionViewState


class ChatProjector:
    """Projects bus events into widget tree mutations.

    Owns the active cron renderer dict and the status bar / cron-history
    read model assembly.  The controller and app delegate to this object
    rather than holding the logic themselves.
    """

    def __init__(self, app: App) -> None:
        self._app = app
        self._cron_renderers: dict[str, StreamRenderer] = {}

    # ── helpers ─────────────────────────────────────────────────────────

    @property
    def cron_renderers(self) -> dict[str, StreamRenderer]:
        return self._cron_renderers

    @property
    def _history(self):
        return self._app._history

    @property
    def _agent(self):
        return self._app._agent

    @property
    def _view(self) -> SessionViewState:
        return self._app._view_state

    @property
    def _notifications(self):
        return self._app._notifications

    @property
    def _current_session_id(self) -> str:
        return self._history.session_id

    def _is_cron(self, stream_id: str) -> bool:
        return stream_id in self._cron_renderers

    @staticmethod
    def fmt_ts(ts: float | None) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def format_cron_page(cls, records: list[dict], query: str = "") -> str:
        header = f"当前会话已完成 cron 执行记录 ({len(records)})"
        if query:
            header += f"\n筛选: {query}"
        header += "\n"
        if not records:
            return header + "\n  [无已完成任务]\n"
        lines = [header]
        for rec in records:
            result = str(rec.get("result") or rec.get("error") or "")
            if len(result) > 120:
                result = result[:120] + "..."
            params = str(rec.get("params", {}))
            if len(params) > 120:
                params = params[:120] + "..."
            lines.extend([
                f"- [{rec.get('execution_id', '')}] {rec.get('name', '')} ({rec.get('status', '')})",
                f"  job_id: {rec.get('job_id', '')}",
                f"  action: {rec.get('action', '')}",
                f"  started: {cls.fmt_ts(rec.get('started_at'))}",
                f"  finished: {cls.fmt_ts(rec.get('finished_at'))}",
                f"  params: {params}",
                f"  result: {result}",
                "",
            ])
        return "\n".join(lines).rstrip()

    # ── bus event handlers ──────────────────────────────────────────────

    async def on_cron_job_event(self, event: CronJobEvent) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        self._handle_cron_job_event(event)
        self.refresh_status_bar()

    async def on_cron_debug_event(self, event: CronDebugEvent) -> None:
        if event.message:
            self._notifications.show_toast(event.message, duration=3)

    async def on_skill_reflect_event(self, event: SkillReflectEvent) -> None:
        if event.new or event.updated or event.deprecated:
            chat_view = self._app.query_one("#chat-view", VerticalScroll)
            chat_view.mount(SystemBubble(f"\U0001f3af {event.toast}"))
        self._notifications.show_toast(
            self._notifications.format_reflect_toast(event), duration=3
        )

    async def on_skill_reflect_error_event(self, event: SkillReflectErrorEvent) -> None:
        self._notifications.show_toast(f"反思失败：{event.error}", duration=4)

    # ── cron stream typed-event handlers ────────────────────────────────

    async def on_cron_tool_started(self, event: ToolStarted) -> None:
        if not event.is_cron or not event.stream_id:
            return
        if event.session_id and event.session_id != self._current_session_id:
            return
        sid = event.stream_id
        if sid in self._cron_renderers:
            return
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        bubble = AlexBubble()
        chat_view.mount(bubble)
        chat_view.scroll_end()
        renderer = StreamRenderer(bubble)
        self._cron_renderers[sid] = renderer
        tid = event.tool_id or sid
        renderer.on_tool_started(tid, event.tool_name, event.tool_input)

    async def on_cron_tool_finished(self, event: ToolFinished) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer:
            renderer.on_tool_finished(event.tool_id or "", str(event.output or ""))

    async def on_cron_thinking(self, event: ThinkingUpdated) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer:
            renderer.on_thinking(str(event.delta or ""))

    async def on_cron_token(self, event: TokenEmitted) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer:
            renderer.on_token(str(event.delta or ""))

    async def on_cron_batch(self, event: CronBatch) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer and isinstance(event.messages, list):
            renderer.on_batch(event.messages)

    async def on_cron_done(self, event: CronDone) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id:
            return
        renderer = self._cron_renderers.pop(event.stream_id, None)
        if not renderer:
            return
        turn = renderer.build_turn(kind="cron")
        if event.content:
            turn.response = event.content
        if event.thinking:
            turn.thinking = event.thinking
        renderer.finalize(turn)
        self._history.add(turn, messages_delta=renderer.message_batch)
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        self.trim_chat_view(chat_view)
        chat_view.scroll_end()
        self.refresh_status_bar()

    async def on_cron_error(self, event: CronError) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id:
            return
        renderer = self._cron_renderers.pop(event.stream_id, None)
        err = str(event.error or "")
        if renderer:
            turn = renderer.build_turn(kind="cron")
            turn.response = f"Error: {err}"
            renderer.finalize(turn)
        else:
            chat_view = self._app.query_one("#chat-view", VerticalScroll)
            chat_view.mount(SystemBubble(f"cron error: {err}"))
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        self.trim_chat_view(chat_view)
        chat_view.scroll_end()
        self.refresh_status_bar()

    # ── cron history read model ─────────────────────────────────────────

    def persist_cron_record(self, event: CronJobEvent) -> None:
        """Update in-memory cron history for the current session.

        Disk persistence is handled by SessionPersistence (store adapter)
        which subscribes to CronJobEvent directly.
        """
        if event.status not in ("SUCCESS", "FAILED"):
            return
        target_session_id = event.session_id or self._history.session_id
        record = {
            "execution_id": event.tool_call_id or f"cron:{event.job_id}:{event.runs_done}",
            "job_id": event.job_id,
            "name": event.name,
            "status": event.status,
            "action": event.action,
            "params": dict(event.params or {}),
            "runs_done": event.runs_done,
            "started_at": event.started_at,
            "finished_at": event.finished_at,
            "result": event.result,
            "error": event.error,
        }
        if target_session_id == self._history.session_id:
            self._history.add_cron_record(record)
            self._agent.set_session_context(
                self._history.session_id, self._history.cron_history
            )

    def _handle_cron_job_event(self, event: CronJobEvent) -> None:
        self.persist_cron_record(event)
        if event.status == "FAILED":
            self._notifications.show_toast(f"任务失败：{event.name}", duration=3)
        elif event.status == "SUCCESS":
            self._notifications.show_toast(f"任务完成：{event.name}", duration=2)

    # ── status bar ──────────────────────────────────────────────────────

    def refresh_status_bar(self) -> None:
        try:
            jobs = self._agent.list_cron_jobs()
        except Exception:
            jobs = []
        jobs = [j for j in jobs if str(j.get("status", "")) in ("RUNNING", "SCHEDULED")]

        lines: list[str] = []
        if not jobs:
            lines.append("  [无任务]")
        else:
            now = time.time()
            for j in jobs[:20]:
                name = str(j.get("name", ""))[:18]
                status = str(j.get("status", ""))
                icon = {"RUNNING": "⟳", "SCHEDULED": "⏱"}.get(status, "·")
                next_at = j.get("next_run_at")
                if isinstance(next_at, (int, float)) and next_at:
                    eta = max(0, int(next_at - now))
                    eta_s = f"{eta}s"
                else:
                    eta_s = "-"
                runs = int(j.get("runs_done", 0) or 0)
                lines.append(f"{icon} {name}  ({status})  next:{eta_s}  ran:{runs}")

        content = self._app.query_one("#status-content", Static)
        content.update("\n".join(lines))

    @staticmethod
    def trim_chat_view(chat_view: VerticalScroll) -> None:
        """Keep only the last 40 widgets to prevent layout slowdown."""
        children = list(chat_view.children)
        if len(children) > 40:
            for child in children[:-40]:
                child.remove()
