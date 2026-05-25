"""TUI controller — mixin with command handlers, feedback, session, cron, toast."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from langchain_core.messages import BaseMessage

from textual import work
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from alex.bus.events import (
    CronDebugEvent,
    CronJobEvent,
    SkillReflectErrorEvent,
    SkillReflectEvent,
    ThinkingUpdated,
    TokenEmitted,
    ToolStarted,
    ToolFinished,
    CronBatch,
    CronDone,
    CronError,
)
from alex.tui.view_models import ChatHistory, ChatTurn
from alex.tui.presenter import AlexBubble, SystemBubble, UserBubble, render_turn
from alex.tui.stream_renderer import StreamRenderer


class ChatControllerMixin:
    """Handler methods for AlexApp — commands, feedback, sessions, cron, toast.

    Designed as a mixin for textual.app.App subclasses.  Assumes the
    following attributes are set by the concrete App class:

      _agent: AgentFacade
      _bus: AsyncEventBus | None
      _history: ChatHistory
      _cron_renderers: dict[str, StreamRenderer]
      _showing_session_list: bool
      _session_options: list
      _page_mode: str | None
      _last_response_rated: bool
      _pending_feedback_turn_id: str
      _feedback_widget: Static | None
      _toast_widget: Static | None
      _toast_timer: object
      _thinking_expanded: bool
      _skills_expanded: bool
    """

    # ── bus event handlers ──────────────────────────────────────────────────

    @property
    def _current_session_id(self) -> str:
        return self._history.session_id

    async def _on_cron_job_event(self, event: CronJobEvent) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        self._handle_cron_job_event(event)
        self._refresh_status_bar()

    async def _on_cron_debug_event(self, event: CronDebugEvent) -> None:
        if event.message:
            self._show_toast(event.message, duration=3)

    async def _on_skill_reflect_event(self, event: SkillReflectEvent) -> None:
        if event.new or event.updated or event.deprecated:
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.mount(SystemBubble(f"\U0001f3af {event.toast}"))
        self._show_toast(self._format_reflect_toast(event), duration=3)

    async def _on_skill_reflect_error_event(self, event: SkillReflectErrorEvent) -> None:
        self._show_toast(f"反思失败：{event.error}", duration=4)

    # ── cron stream typed-event handlers ───────────────────────────────────

    def _is_cron(self, stream_id: str) -> bool:
        return stream_id in self._cron_renderers

    async def _on_cron_tool_started(self, event: ToolStarted) -> None:
        if not event.is_cron or not event.stream_id:
            return
        if event.session_id and event.session_id != self._current_session_id:
            return
        sid = event.stream_id
        if sid in self._cron_renderers:
            return
        chat_view = self.query_one("#chat-view", VerticalScroll)
        bubble = AlexBubble()
        chat_view.mount(bubble)
        chat_view.scroll_end()
        renderer = StreamRenderer(bubble)
        self._cron_renderers[sid] = renderer
        tid = event.tool_id or sid
        renderer.on_tool_started(tid, event.tool_name, event.tool_input)

    async def _on_cron_tool_finished(self, event: ToolFinished) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer:
            renderer.on_tool_finished(event.tool_id or "", str(event.output or ""))

    async def _on_cron_thinking(self, event: ThinkingUpdated) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer:
            renderer.on_thinking(str(event.delta or ""))

    async def _on_cron_token(self, event: TokenEmitted) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer:
            renderer.on_token(str(event.delta or ""))

    async def _on_cron_batch(self, event: CronBatch) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id or not self._is_cron(event.stream_id):
            return
        renderer = self._cron_renderers.get(event.stream_id)
        if renderer and isinstance(event.messages, list):
            renderer.on_batch(event.messages)

    async def _on_cron_done(self, event: CronDone) -> None:
        if event.session_id and event.session_id != self._current_session_id:
            return
        if not event.stream_id:
            return
        renderer = self._cron_renderers.pop(event.stream_id, None)
        if not renderer:
            return
        turn = renderer.build_turn(kind="cron")
        # Event payload takes precedence over incremental accumulation
        if event.content:
            turn.response = event.content
        if event.thinking:
            turn.thinking = event.thinking
        renderer.finalize(turn)
        self._history.add(turn, messages_delta=renderer.message_batch)
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._trim_chat_view(chat_view)
        chat_view.scroll_end()
        self._refresh_status_bar()

    async def _on_cron_error(self, event: CronError) -> None:
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
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.mount(SystemBubble(f"cron error: {err}"))
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._trim_chat_view(chat_view)
        chat_view.scroll_end()
        self._refresh_status_bar()

    # ── feedback ────────────────────────────────────────────────────────────

    def _show_feedback_prompt(self) -> None:
        """Show inline feedback prompt after a response."""
        self._last_response_rated = False
        if self._feedback_widget:
            self._feedback_widget.remove()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        prompt = Static(
            "\U0001f44d Ctrl+G Good  \U0001f44e Ctrl+B Bad  ⏎ skip",
            classes="feedback-prompt",
        )
        self._feedback_widget = prompt
        chat_view.mount(prompt)

    def _dismiss_feedback(self) -> None:
        """Dismiss the feedback prompt without rating (implicit skip)."""
        self._last_response_rated = True
        if self._feedback_widget:
            self._feedback_widget.remove()
            self._feedback_widget = None

    def _rate_response(self, good: bool) -> None:
        """Submit user rating for the last response."""
        if self._last_response_rated:
            return
        self._last_response_rated = True

        self._agent.provide_feedback(good, turn_id=self._pending_feedback_turn_id)
        self._pending_feedback_turn_id = ""
        if not good:
            self._show_toast("已标记为不满意，正在反思…", duration=2)

        # Replace prompt with confirmation
        if self._feedback_widget:
            self._feedback_widget.remove()
        label = "✓ Rated as helpful" if good else "✗ Rated as unhelpful"
        self._feedback_widget = Static(label, classes="feedback-done")
        self.query_one("#chat-view", VerticalScroll).mount(self._feedback_widget)

    def action_rate_good(self) -> None:
        self._rate_response(True)

    def action_rate_bad(self) -> None:
        self._rate_response(False)

    # ── toggles ─────────────────────────────────────────────────────────────

    def action_toggle_thinking(self) -> None:
        """Toggle all thinking blocks expanded/collapsed."""
        self._thinking_expanded = not self._thinking_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_thinking_expanded(self._thinking_expanded)

    def action_toggle_skills(self) -> None:
        """Toggle all skill blocks expanded/collapsed."""
        self._skills_expanded = not self._skills_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_skills_expanded(self._skills_expanded)

    # ── commands ────────────────────────────────────────────────────────────

    def _dismiss_overlay(self) -> None:
        """Remove overlay blocks (help, skills list, session list) and toast."""
        self._dismiss_panels()
        self._dismiss_toast()

    def _dismiss_panels(self) -> None:
        """Remove overlay blocks (help, skills list, session list)."""
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.remove_class("hidden")
        page_view.add_class("hidden")
        self.query_one("#page-title", Static).update("")
        self.query_one("#page-content", Static).update("")
        self._showing_session_list = False
        self._page_mode = None
        chat_view.scroll_end()

    def _show_page(self, title: str, content: str, *, mode: str) -> None:
        self._page_mode = mode
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.add_class("hidden")
        page_view.remove_class("hidden")
        self.query_one("#page-title", Static).update(title)
        self.query_one("#page-content", Static).update(content)
        page_view.scroll_home()

    @staticmethod
    def _fmt_ts(ts: float | None) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def _format_cron_page(self, records: list[dict], query: str = "") -> str:
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
                f"  started: {self._fmt_ts(rec.get('started_at'))}",
                f"  finished: {self._fmt_ts(rec.get('finished_at'))}",
                f"  params: {params}",
                f"  result: {result}",
                "",
            ])
        return "\n".join(lines).rstrip()

    def _persist_cron_record(self, event: CronJobEvent) -> None:
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
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)

    def _handle_cron_job_event(self, event: CronJobEvent) -> None:
        self._persist_cron_record(event)
        if event.status == "FAILED":
            self._show_toast(f"任务失败：{event.name}", duration=3)
        elif event.status == "SUCCESS":
            self._show_toast(f"任务完成：{event.name}", duration=2)

    def _refresh_status_bar(self) -> None:
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

        content = self.query_one("#status-content", Static)
        content.update("\n".join(lines))

    def _trim_chat_view(self, chat_view: VerticalScroll) -> None:
        """Keep only the last 40 widgets to prevent layout slowdown."""
        children = list(chat_view.children)
        if len(children) > 40:
            for child in children[:-40]:
                child.remove()

    def _show_help(self) -> None:
        """Show help with all commands and keyboard shortcuts."""
        help_text = """  \U0001f4d6 Commands:
    /help             Show this help
    /skills           List all skills
    /cron [query]     Query completed cron executions in this session
    /skills del <id>  Delete a skill by name or id prefix
    /skills dep <id>  Deprecate a skill by name or id prefix
    /merge-skills     LLM-based skill deduplication
    /reflect          Force skill reflection now
    /resume           Resume a saved session
    /clear            Clear current chat
    /quit             Exit Alex
    :q                Return to chat
    /x                Dismiss toast

  ⌨️  Shortcuts:
    Ctrl+G / Ctrl+B   Rate last response (Good / Bad)
    Ctrl+T            Toggle thinking blocks
    Ctrl+K            Toggle skill blocks"""
        self._show_page("帮助", help_text, mode="help")

    @work(exclusive=True)
    async def _run_force_reflection(self) -> None:
        self._show_toast("正在反思…", duration=2)
        await self._agent.reflect()
        self._refresh_status_bar()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._trim_chat_view(chat_view)
        chat_view.scroll_end()

    def _handle_skills_cmd(self, args: str) -> None:
        """Handle /skills [del|dep] [id]"""
        if not args:
            all_skills = self._agent.list_skills()
            if not all_skills:
                content = "  [No skills]"
            else:
                lines = ["  \U0001f3af Skills:"]
                for s in sorted(all_skills, key=lambda s: (s["status"], -s["use_count"])):
                    status_icon = {"ACTIVE": "✅", "CANDIDATE": "\U0001f535", "DEPRECATED": "⚪"}.get(s["status"], "?")
                    lines.append(
                        f"  {status_icon} [{s['status']}] {s['name']}"
                        f"  | used:{s['use_count']} ok:{s['success_count']} fail:{s['failure_count']}"
                        f"  | id:{s['id'][:8]}"
                    )
                content = "\n".join(lines)
            self._show_page("技能列表", content, mode="skills")
            return

        parts = args.split(None, 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""

        if action in ("del", "delete") and target:
            name = self._agent.delete_skill(target)
            if name:
                self._show_toast(f"已删除技能：{name}", duration=2)
            else:
                self._show_toast(f"未找到技能：{target}", duration=2)
        elif action in ("dep", "deprecate") and target:
            name = self._agent.deprecate_skill(target)
            if name:
                self._show_toast(f"已废弃技能：{name}", duration=2)
            else:
                self._show_toast(f"未找到技能：{target}", duration=2)
        else:
            self._show_toast(f"未知命令: /skills {args}", duration=2)

    def _handle_cron_cmd(self, args: str) -> None:
        """Show completed cron execution history for the current session."""
        records = self._agent.list_session_cron_history(query=args, limit=50)
        content = self._format_cron_page(records, query=args)
        self._show_page("Cron 历史", content, mode="cron")

    # ── session management ──────────────────────────────────────────────────

    def _show_session_list(self) -> None:
        """Show a list of saved sessions for the user to pick from."""
        sessions = self._agent.list_sessions()

        if not sessions:
            self._show_page("会话列表", "  [No saved sessions found]", mode="resume")
            return

        self._showing_session_list = True
        self._session_options = sessions

        lines = ["\U0001f4cb Saved sessions (type number to resume, or anything else to cancel):", ""]
        for i, s in enumerate(sessions, 1):
            created = s.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = created[:16] if created else "unknown"
            preview = s.get("first_message", "") or "(empty)"
            lines.append(f"  {i}. [{time_str}] {preview}  ({s.get('message_count', 0)} msgs)")

        self._show_page("会话列表", "\n".join(lines), mode="resume")

    def _handle_session_selection(self, user_input: str) -> None:
        """Handle user's session selection."""
        self._showing_session_list = False

        # Check if input is a valid number
        try:
            idx = int(user_input) - 1
            if 0 <= idx < len(self._session_options):
                session = self._session_options[idx]
                self._resume_session(session["session_id"])
                return
        except (ValueError, AttributeError):
            pass

        self._dismiss_panels()
        self._show_toast("已取消恢复会话", duration=2)

    @work(exclusive=True)
    async def _resume_session(self, session_id: str) -> None:
        """Resume a saved session — restore memory first, then render UI.

        Uses @work(exclusive=True) to serialize with other lifecycle ops
        (clear, merge).  Input is disabled during the operation to prevent
        the user sending a message before agent memory is ready.
        """
        # Load the session and restore agent memory first
        bundle = self._agent.load_session(session_id)
        if bundle is None:
            self._show_toast(f"会话 {session_id} 加载失败", duration=2)
            return
        self._history = ChatHistory(session_id=session_id)
        self._history.restore_from_bundle(bundle)
        self._cron_renderers.clear()
        self._pending_feedback_turn_id = ""
        self._last_response_rated = True
        self._dismiss_feedback()

        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._agent.restore_history(self._history.loaded_messages)
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)

            # Now render UI with the restored state
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.remove_children()
            for turn in self._history.turns:
                render_turn(chat_view, turn,
                            thinking_expanded=self._thinking_expanded,
                            skills_expanded=self._skills_expanded)
            chat_view.scroll_end()
            self._dismiss_panels()
        finally:
            input_widget.disabled = False

    @work(exclusive=True)
    async def _clear_chat(self) -> None:
        """Clear chat history and view — memory first, then UI.

        Serialized via @work(exclusive=True); input is disabled during the
        operation so the user cannot send a message against stale state.
        """
        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._agent.clear_history()
            self._history.clear()
            self._cron_renderers.clear()
            self._pending_feedback_turn_id = ""
            self._last_response_rated = True
            self._dismiss_feedback()
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.remove_children()
            self._dismiss_panels()
        finally:
            input_widget.disabled = False

    @work(exclusive=True)
    async def _run_merge_skills(self) -> None:
        """Run LLM-based skill merging."""
        chat_view = self.query_one("#chat-view", VerticalScroll)

        # Show status
        before_count = len([s for s in self._agent.list_skills() if s["status"] != "DEPRECATED"])
        status_widget = Static(f"  \U0001f527 Merging skills... ({before_count} skills, this may take a moment)")
        chat_view.mount(status_widget)
        chat_view.scroll_end()

        try:
            result = await self._agent.merge_skills()
            status_widget.remove()

            # Show result
            msg = (
                f"  ✓ Skill merge complete:\n"
                f"    Merged: {result.get('merged', 0)} redundant skills removed\n"
                f"    Deprecated: {result.get('deprecated', 0)} skills deprecated\n"
                f"    Remaining: {result.get('remaining', '?')} active skills"
            )
            if result.get("error"):
                msg += f"\n    ⚠ {result['error']}"
            chat_view.mount(Static(msg))
        except Exception as e:
            status_widget.remove()
            chat_view.mount(Static(f"  ✗ Merge failed: {e}"))

        chat_view.scroll_end()

    # ── toast ───────────────────────────────────────────────────────────────

    def _dismiss_toast(self) -> None:
        """Hide the toast notification."""
        if self._toast_widget:
            self._toast_widget.set_class(True, "toast-hidden")
        if self._toast_timer:
            self._toast_timer.stop()
            self._toast_timer = None

    def _show_toast(self, message: str, duration: float = 2) -> None:
        """Show a small notification toast, auto-dismiss after `duration` seconds."""
        if self._toast_widget is None:
            self._toast_widget = Static("", classes="toast toast-hidden")
            self.mount(self._toast_widget)
        self._toast_widget.update(message)
        self._toast_widget.set_class(False, "toast-hidden")
        self._toast_timer = self.set_timer(duration, self._dismiss_toast)

    def _format_reflect_toast(self, evt: SkillReflectEvent) -> str:
        base = f"反思完成：新增 {evt.new}，更新 {evt.updated}，废弃 {evt.deprecated}"
        if evt.names:
            shown = ", ".join([str(n) for n in evt.names[:3] if n])
            more = "…" if len(evt.names) > 3 else ""
            base += f"（新技能：{shown}{more}）"
        if evt.updated_names:
            shown = ", ".join([str(n) for n in evt.updated_names[:3] if n])
            more = "…" if len(evt.updated_names) > 3 else ""
            base += f"（更新：{shown}{more}）"
        return base
