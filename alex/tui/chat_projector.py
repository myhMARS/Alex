"""ChatProjector — bus events → widget updates and cron renderer management.

Extracted from ChatControllerMixin so the TUI controller no longer owns
cron stream handlers, renderer lifecycle, status bar refresh, or the
cron history read model assembly.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.widgets import Static

from alex.bus.events import (
    CronDebugEvent,
    CronJobEvent,
    SkillReflectErrorEvent,
    SkillReflectEvent,
    SkillLoaded,
    ThinkingUpdated,
    TokenEmitted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from alex.tui.presenter import AlexBubble, SystemBubble, UserBubble
from alex.tui.stream_renderer import StreamRenderer

if TYPE_CHECKING:
    from alex.tui.app import AlexApp


class ChatProjector:
    """Projects bus events into widget tree mutations.

    Owns the active cron renderer dict and the status bar / cron-history
    read model assembly.  The controller and app delegate to this object
    rather than holding the logic themselves.
    """

    def __init__(self, app: AlexApp) -> None:
        self._app = app
        self._active_renderers: dict[str, StreamRenderer] = {}
        self._user_inputs: dict[str, str] = {}
        self._pending_user_inputs: deque[str] = deque()
        self._cached_cron_jobs: list[dict] = []

    # ── helpers ─────────────────────────────────────────────────────────

    @property
    def cron_renderers(self) -> dict[str, StreamRenderer]:
        return self._active_renderers

    @property
    def _current_session_id(self) -> str:
        return self._app.chat_history.session_id

    def note_user_submission(self, user_input: str) -> None:
        self._pending_user_inputs.append(str(user_input))

    def _renderer(self, turn_id: str) -> StreamRenderer | None:
        return self._active_renderers.get(turn_id)

    def _start_renderer(self, *, turn_id: str, kind: str, user_input: str = "") -> StreamRenderer:
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        if kind == "user":
            chat_view.mount(UserBubble(user_input))
        bubble = AlexBubble(tool_output_expanded=self._app.tool_output_expanded)
        chat_view.mount(bubble)
        self.trim_chat_view(chat_view)
        chat_view.scroll_end()
        renderer = StreamRenderer(bubble)
        self._active_renderers[turn_id] = renderer
        if kind == "user":
            self._user_inputs[turn_id] = user_input
        return renderer

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
            prompt = str(rec.get("prompt") or "")
            if len(prompt) > 120:
                prompt = prompt[:120] + "..."
            lines.extend([
                f"- [{rec.get('execution_id', '')}] {rec.get('name', '')} ({rec.get('status', '')})",
                f"  job_id: {rec.get('job_id', '')}",
                f"  recurring: {rec.get('recurring', True)}",
                f"  durable: {rec.get('durable', False)}",
                f"  started: {cls.fmt_ts(rec.get('started_at'))}",
                f"  finished: {cls.fmt_ts(rec.get('finished_at'))}",
                f"  prompt: {prompt}",
                f"  result: {result}",
                "",
            ])
        return "\n".join(lines).rstrip()

    @classmethod
    def format_cron_jobs_page(cls, jobs: list[dict], query: str = "") -> str:
        header = f"当前 cron 任务 ({len(jobs)})"
        if query:
            header += f"\n筛选: {query}"
        header += "\n"
        if not jobs:
            return header + "\n  [无任务]\n"
        lines = [header]
        for job in jobs:
            prompt = str(job.get("prompt") or "")
            if len(prompt) > 120:
                prompt = prompt[:120] + "..."
            last_outcome = str(job.get("last_result") or job.get("last_error") or "")
            if len(last_outcome) > 120:
                last_outcome = last_outcome[:120] + "..."
            lines.extend([
                f"- [{job.get('id', '')}] {job.get('name', '')} ({job.get('status', '')})",
                f"  cron: {job.get('cron', '')}",
                f"  recurring: {job.get('recurring', True)}",
                f"  durable: {job.get('durable', False)}",
                f"  next_run: {cls.fmt_ts(job.get('next_run_at'))}",
                f"  last_started: {cls.fmt_ts(job.get('last_started_at'))}",
                f"  last_finished: {cls.fmt_ts(job.get('last_finished_at'))}",
                f"  prompt: {prompt}",
                f"  last_outcome: {last_outcome}",
                "",
            ])
        return "\n".join(lines).rstrip()

    # ── bus event handlers ──────────────────────────────────────────────

    async def on_cron_job_event(self, event: CronJobEvent) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        self._handle_cron_job_event(event)
        # 从 bus 获取最新的完整任务列表
        await self._refresh_cron_cache()
        self.refresh_status_bar()

    async def on_cron_debug_event(self, event: CronDebugEvent) -> None:
        if event.message:
            self._app.notif.show_toast(event.message, duration=3)

    async def on_skill_reflect_event(self, event: SkillReflectEvent) -> None:
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        if event.new or event.updated or event.deprecated:
            chat_view.mount(SystemBubble(f"\U0001f3af {event.toast}"))
        else:
            chat_view.mount(SystemBubble("\U0001f3af 反思完成，未发现需要更新的技能"))
        self._app.notif.show_toast(
            self._app.notif.format_reflect_toast(event), duration=3
        )

    async def on_skill_reflect_error_event(self, event: SkillReflectErrorEvent) -> None:
        self._app.notif.show_toast(f"反思失败：{event.error}", duration=4)

    # ── unified turn stream typed-event handlers ────────────────────────

    async def on_turn_started(self, event: TurnStarted) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.turn_id or self._renderer(event.turn_id) is not None:
            return
        user_input = ""
        if event.kind == "user" and self._pending_user_inputs:
            user_input = self._pending_user_inputs.popleft()
        self._start_renderer(turn_id=event.turn_id, kind=event.kind, user_input=user_input)

    async def on_skill_loaded(self, event: SkillLoaded) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._renderer(event.turn_id)
        if renderer:
            renderer.on_skill_loaded(event.skill_name, event.skill_pattern)

    async def on_thinking(self, event: ThinkingUpdated) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._renderer(event.turn_id)
        if renderer:
            renderer.on_thinking(str(event.delta or ""))

    async def on_token(self, event: TokenEmitted) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._renderer(event.turn_id)
        if renderer:
            renderer.on_token(str(event.delta or ""))

    async def on_tool_started(self, event: ToolStarted) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._renderer(event.turn_id)
        if renderer:
            tid = event.tool_id or f"{event.tool_name}:{time.monotonic_ns()}"
            renderer.on_tool_started(tid, event.tool_name, event.tool_input)

    async def on_tool_finished(self, event: ToolFinished) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._renderer(event.turn_id)
        if not renderer:
            return
        renderer.on_tool_finished(event.tool_id or "", str(event.output or ""))

    async def on_turn_completed(self, event: TurnCompleted) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._active_renderers.pop(event.turn_id, None)
        if renderer:
            turn = renderer.build_turn(
                user_input=self._user_inputs.pop(event.turn_id, ""),
                kind=event.kind,
            )
            if event.content:
                turn.response = event.content
            if event.thinking:
                turn.thinking = event.thinking
            renderer.on_batch(list(event.message_batch or []))
            renderer.finalize(turn)
            self._app.chat_history.add(turn, messages_delta=list(event.message_batch or []))
            if event.kind == "user":
                self._app.view_state.pending_feedback_turn_id = event.turn_id
                # 用了 skill 才显示反馈提示（用于评价 skill 效果）
                # 没用 skill 的对话由 AgentModule 自动触发反思
                if renderer.skills:
                    self._app.notif.show_feedback_prompt()
        else:
            self._user_inputs.pop(event.turn_id, None)
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        self.trim_chat_view(chat_view)
        chat_view.scroll_end()
        self.refresh_status_bar()

    async def on_turn_failed(self, event: TurnFailed) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        renderer = self._active_renderers.pop(event.turn_id, None)
        err = str(event.error or "")
        if renderer:
            turn = renderer.build_turn(
                user_input=self._user_inputs.pop(event.turn_id, ""),
                kind="cron" if event.source == "cron" else "user",
            )
            turn.response = f"Error: {err}"
            renderer.finalize(turn)
        else:
            self._user_inputs.pop(event.turn_id, None)
            chat_view = self._app.query_one("#chat-view", VerticalScroll)
            chat_view.mount(SystemBubble(f"turn error: {err}"))
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
        target_session_id = event.session_id or self._app.chat_history.session_id
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
        if target_session_id == self._app.chat_history.session_id:
            self._app.chat_history.add_cron_record(record)
            # session context 由 history 本地维护，不再需要通知 agent

    def _handle_cron_job_event(self, event: CronJobEvent) -> None:
        self.persist_cron_record(event)
        # 更新本地 cron jobs 缓存
        job_data = {
            "id": event.job_id,
            "name": event.name,
            "status": event.status,
            "prompt": event.prompt,
            "recurring": event.recurring,
            "durable": event.durable,
        }
        # 更新或添加
        found = False
        for i, j in enumerate(self._cached_cron_jobs):
            if j.get("id") == event.job_id:
                self._cached_cron_jobs[i].update(job_data)
                found = True
                break
        if not found:
            self._cached_cron_jobs.append(job_data)

        if event.status == "FAILED":
            self._app.notif.show_toast(f"任务失败：{event.name}", duration=3)
        elif event.status == "SUCCESS":
            self._app.notif.show_toast(f"任务完成：{event.name}", duration=2)

    # ── status bar ──────────────────────────────────────────────────────

    async def _refresh_cron_cache(self) -> None:
        """从 bus 获取最新的 cron 任务列表更新缓存。"""
        try:
            from alex.kernel.contracts.cron import ListCronJobs
            bus = self._app.message_bus
            if bus is not None:
                self._cached_cron_jobs = await bus.request(ListCronJobs())
        except Exception:
            pass  # bus 未就绪或 CronModule 未启动

    def refresh_status_bar(self) -> None:
        try:
            jobs = self._cached_cron_jobs
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
