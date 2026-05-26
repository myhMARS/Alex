"""TUI controller — mixin with command handlers, session lifecycle, toggles.

Delegates bus→widget projection to ChatProjector and toast/feedback to
NotificationController.  The controller now focuses on command dispatch,
page/session management, and key-binding actions.
"""

from __future__ import annotations

from textual import work
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from alex.tui.view_models import ChatHistory
from alex.tui.presenter import AlexBubble, render_turn


class ChatControllerMixin:
    """Command dispatch, session lifecycle, and UI toggles for AlexApp.

    Designed as a mixin for textual.app.App subclasses.  Assumes the
    following attributes are set by the concrete App:

      _agent: AgentFacade
      _history: ChatHistory
      _view_state: SessionViewState
      _projector: ChatProjector
      _notifications: NotificationController
      _thinking_expanded: bool
      _skills_expanded: bool
    """

    # ── page / overlay management ───────────────────────────────────────

    def _dismiss_overlay(self) -> None:
        """Remove overlay blocks (help, skills list, session list) and toast."""
        self._dismiss_panels()
        self._notifications.dismiss_toast()

    def _dismiss_panels(self) -> None:
        """Remove overlay blocks (help, skills list, session list)."""
        vs = self._view_state
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.remove_class("hidden")
        page_view.add_class("hidden")
        self.query_one("#page-title", Static).update("")
        self.query_one("#page-content", Static).update("")
        vs.showing_session_list = False
        vs.page_mode = None
        chat_view.scroll_end()

    def _show_page(self, title: str, content: str, *, mode: str) -> None:
        self._view_state.page_mode = mode
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.add_class("hidden")
        page_view.remove_class("hidden")
        self.query_one("#page-title", Static).update(title)
        self.query_one("#page-content", Static).update(content)
        page_view.scroll_home()

    # ── help ────────────────────────────────────────────────────────────

    def _show_help(self) -> None:
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

    # ── toggles ─────────────────────────────────────────────────────────

    def action_toggle_thinking(self) -> None:
        self._thinking_expanded = not self._thinking_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_thinking_expanded(self._thinking_expanded)

    def action_toggle_skills(self) -> None:
        self._skills_expanded = not self._skills_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_skills_expanded(self._skills_expanded)

    # ── commands ────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _run_force_reflection(self) -> None:
        self._notifications.show_toast("正在反思…", duration=2)
        await self._agent.reflect()
        self._projector.refresh_status_bar()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._projector.trim_chat_view(chat_view)
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
                self._notifications.show_toast(f"已删除技能：{name}", duration=2)
            else:
                self._notifications.show_toast(f"未找到技能：{target}", duration=2)
        elif action in ("dep", "deprecate") and target:
            name = self._agent.deprecate_skill(target)
            if name:
                self._notifications.show_toast(f"已废弃技能：{name}", duration=2)
            else:
                self._notifications.show_toast(f"未找到技能：{target}", duration=2)
        else:
            self._notifications.show_toast(f"未知命令: /skills {args}", duration=2)

    def _handle_cron_cmd(self, args: str) -> None:
        """Show completed cron execution history for the current session."""
        from alex.tui.chat_projector import ChatProjector
        records = self._agent.list_session_cron_history(query=args, limit=50)
        content = ChatProjector.format_cron_page(records, query=args)
        self._show_page("Cron 历史", content, mode="cron")

    # ── session management ──────────────────────────────────────────────

    def _show_session_list(self) -> None:
        """Show a list of saved sessions for the user to pick from."""
        sessions = self._agent.list_sessions()

        if not sessions:
            self._show_page("会话列表", "  [No saved sessions found]", mode="resume")
            return

        self._view_state.showing_session_list = True
        self._view_state.session_options = sessions

        lines = ["\U0001f4cb Saved sessions (type number to resume, or anything else to cancel):", ""]
        for i, s in enumerate(sessions, 1):
            created = s.get("created_at", "")
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(created)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = created[:16] if created else "unknown"
            preview = s.get("first_message", "") or "(empty)"
            lines.append(f"  {i}. [{time_str}] {preview}  ({s.get('message_count', 0)} msgs)")

        self._show_page("会话列表", "\n".join(lines), mode="resume")

    def _handle_session_selection(self, user_input: str) -> None:
        """Handle user's session selection."""
        vs = self._view_state
        vs.showing_session_list = False

        try:
            idx = int(user_input) - 1
            if 0 <= idx < len(vs.session_options):
                session = vs.session_options[idx]
                self._resume_session(session["session_id"])
                return
        except (ValueError, AttributeError):
            pass

        self._dismiss_panels()
        self._notifications.show_toast("已取消恢复会话", duration=2)

    @work(exclusive=True)
    async def _resume_session(self, session_id: str) -> None:
        """Resume a saved session — restore memory first, then render UI.

        Uses @work(exclusive=True) to serialize with other lifecycle ops
        (clear, merge).  Input is disabled during the operation to prevent
        the user sending a message before agent memory is ready.
        """
        bundle = self._agent.load_session(session_id)
        if bundle is None:
            self._notifications.show_toast(f"会话 {session_id} 加载失败", duration=2)
            return
        self._history = ChatHistory(session_id=session_id)
        self._history.restore_from_bundle(bundle)
        self._projector.cron_renderers.clear()
        self._view_state.reset()
        self._notifications.dismiss_feedback()

        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._agent.restore_history(self._history.loaded_messages)
            self._agent.set_session_context(self._history.session_id, self._history.cron_history)

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
            self._projector.cron_renderers.clear()
            self._view_state.reset()
            self._notifications.dismiss_feedback()
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

        before_count = len([s for s in self._agent.list_skills() if s["status"] != "DEPRECATED"])
        status_widget = Static(f"  \U0001f527 Merging skills... ({before_count} skills, this may take a moment)")
        chat_view.mount(status_widget)
        chat_view.scroll_end()

        try:
            result = await self._agent.merge_skills()
            status_widget.remove()

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
