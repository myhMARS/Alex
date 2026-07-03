"""TUI controller — mixin with command handlers, session lifecycle, toggles.

所有与其他模块的通信都通过 bus 完成，不再依赖 AgentFacade。
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from textual import work
from textual.containers import VerticalScroll
from textual.widgets import Input, Static, Tree

from alex.kernel.contracts.cron import ListCronJobs
from alex.kernel.contracts.memory import ClearMemory, ReplaceMemory
from alex.kernel.contracts.session import ListSessions, LoadSession
from alex.kernel.contracts.skills import (
    DeleteSkill,
    DeprecateSkill,
    ListSkills,
    MergeSkills,
    ReflectSkills,
)
from alex.tui.chat_projector import ChatProjector
from alex.tui.view_models import ChatHistory
from alex.tui.presenter import AlexBubble, render_turn


class ChatControllerMixin:
    """Command dispatch, session lifecycle, and UI toggles for AlexApp.

    所有跨模块操作通过 self._bus 的 publish/request 完成。
    """

    # Injected by AlexApp host
    _bus: Any = None  # type: ignore[assignment]
    _history: ChatHistory = None  # type: ignore[assignment]
    _view_state: Any = None  # type: ignore[assignment]
    _projector: ChatProjector = None  # type: ignore[assignment]
    _notif: Any = None  # type: ignore[assignment]
    _thinking_expanded: bool = False
    _skills_expanded: bool = False
    _tool_output_expanded: bool = False
    _mcp_status_message: str = ""
    _mcp_servers: list[dict] = []
    _mcp_pool: Any = None
    _mcp_configs: list[Any] = []

    # Textual App methods (resolved via MRO at runtime)
    query_one: Any  # type: ignore[assignment]
    query: Any  # type: ignore[assignment]

    # ── page / overlay management ───────────────────────────────────────

    def _dismiss_overlay(self) -> None:
        self._dismiss_panels()
        self._notif.dismiss_toast()

    def _dismiss_panels(self) -> None:
        vs = self._view_state
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.remove_class("hidden")
        page_view.add_class("hidden")
        self.query_one("#page-title", Static).update("")
        self.query_one("#page-content", Static).update("")
        # Hide MCP tree
        existing = self.query("#mcp-tree")
        if existing:
            existing.first().display = False
        vs.showing_session_list = False
        vs.page_mode = None
        chat_view.scroll_end()

    def _show_page(self, title: str, content: str, *, mode: str) -> None:
        self._view_state.page_mode = mode
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.add_class("hidden")
        page_view.remove_class("hidden")
        # Hide MCP tree when showing a static page
        existing = self.query("#mcp-tree")
        if existing:
            existing.first().display = False
        self.query_one("#page-content", Static).display = True
        self.query_one("#page-title", Static).update(title)
        self.query_one("#page-content", Static).update(content)
        page_view.scroll_home()

    # ── help ────────────────────────────────────────────────────────────

    def _show_help(self) -> None:
        help_text = """  \U0001f4d6 Commands:
    /help             Show this help
    /skills           List all skills
    /cron [query]     Show current cron jobs
    /mcp              Show MCP connection status
    /output           Toggle full tool output
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
    Ctrl+K            Toggle skill blocks
    Ctrl+O            Toggle full tool output"""
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

    def action_toggle_tool_output(self) -> None:
        self._tool_output_expanded = not self._tool_output_expanded
        for bubble in self.query(AlexBubble):
            bubble.set_tool_output_expanded(self._tool_output_expanded)
        state = "已展开完整工具输出" if self._tool_output_expanded else "已收起工具输出"
        self._notif.show_toast(state, duration=2)

    # ── commands（全部通过 bus）──────────────────────────────────────────

    @work(exclusive=True)
    async def _run_force_reflection(self) -> None:
        self._notif.show_toast("正在反思…", duration=2)
        self._bus.publish(ReflectSkills(session_id=self._history.session_id))
        self._projector.refresh_status_bar()
        chat_view = self.query_one("#chat-view", VerticalScroll)
        self._projector.trim_chat_view(chat_view)
        chat_view.scroll_end()

    @work(exclusive=True)
    async def _handle_skills_cmd(self, args: str) -> None:
        """Handle /skills [del|dep] [id]"""
        if not args:
            try:
                all_skills = await self._bus.request(
                    ListSkills(session_id=self._history.session_id)
                )
            except Exception:
                all_skills = []
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
            try:
                name = await self._bus.request(
                    DeleteSkill(session_id=self._history.session_id, target=target)
                )
            except Exception:
                name = None
            if name:
                self._notif.show_toast(f"已删除技能：{name}", duration=2)
            else:
                self._notif.show_toast(f"未找到技能：{target}", duration=2)
        elif action in ("dep", "deprecate") and target:
            try:
                name = await self._bus.request(
                    DeprecateSkill(session_id=self._history.session_id, target=target)
                )
            except Exception:
                name = None
            if name:
                self._notif.show_toast(f"已废弃技能：{name}", duration=2)
            else:
                self._notif.show_toast(f"未找到技能：{target}", duration=2)
        else:
            self._notif.show_toast(f"未知命令: /skills {args}", duration=2)

    @work(exclusive=True)
    async def _handle_cron_cmd(self, args: str) -> None:
        """Show current cron jobs."""
        try:
            jobs = await self._bus.request(
                ListCronJobs(session_id=self._history.session_id)
            )
        except Exception:
            jobs = []
        q = (args or "").strip().lower()
        if q:
            jobs = [
                job for job in jobs
                if q in str(job.get("id", "")).lower()
                or q in str(job.get("name", "")).lower()
                or q in str(job.get("status", "")).lower()
                or q in str(job.get("cron", "")).lower()
                or q in str(job.get("prompt", "")).lower()
            ]
        content = ChatProjector.format_cron_jobs_page(jobs[:50], query=args)
        self._show_page("Cron 任务", content, mode="cron")

    def _handle_mcp_cmd(self) -> None:
        """Show MCP runtime status using a Tree widget for native navigation."""
        servers: list[dict] = list(getattr(self, "_mcp_servers", []) or [])
        pool = getattr(self, "_mcp_pool", None)
        connections = list(getattr(pool, "connections", []) or [])
        configs = list(getattr(self, "_mcp_configs", []) or [])
        status = str(getattr(self, "_mcp_status_message", "") or "未开始加载")

        # Switch to page view
        chat_view = self.query_one("#chat-view", VerticalScroll)
        page_view = self.query_one("#page-view", VerticalScroll)
        chat_view.add_class("hidden")
        page_view.remove_class("hidden")
        self._view_state.page_mode = "mcp"

        # Set title
        self.query_one("#page-title", Static).update(f"\U0001f50c MCP 状态 — {status}")

        # Hide static content, show / mount Tree
        self.query_one("#page-content", Static).display = False
        existing = self.query("#mcp-tree")
        tree: Tree
        if existing:
            tree = existing.first()
            tree.display = True
            tree.clear()
        else:
            tree = Tree(f"\U0001f50c {status}", id="mcp-tree")
            page_view.mount(tree)
        tree.show_root = True
        tree.focus()

        if not servers and not connections:
            tree.root.add("[dim]等待 MCP 模块状态更新...[/]")
            return

        # Build server list from event metadata, or fall back to _mcp_pool
        if servers:
            self._populate_tree_from_servers(tree, servers)
        elif connections:
            self._populate_tree_from_pool(tree, connections)

    def _populate_tree_from_servers(self, tree: Tree, servers: list[dict]) -> None:
        """Populate Tree from ToolsProvided event metadata."""
        for srv in servers:
            name = srv.get("name", "?")
            transport = srv.get("transport", "?")
            srv_status = srv.get("status", "?")
            tool_count = srv.get("tool_count")
            error = srv.get("error")
            enabled = srv.get("enabled", True)

            state = _server_state_label(srv_status, error, enabled)
            tool_str = str(tool_count) if tool_count is not None else "?"
            label = f"{name}  [{transport}]  {state}  [bold]tools:{tool_str}[/]"

            cmd = srv.get("command", "")
            url = srv.get("url", "")
            if cmd:
                label += f"\n    [dim]target: {cmd}[/]"
            if url:
                label += f"\n    [dim]target: {url}[/]"
            if error:
                label += f"\n    [$error]error: {error}[/]"

            srv_node = tree.root.add(label, expand=False)
            tools: list[dict] = srv.get("tools", []) or []
            for t in tools:
                self._add_tool_node(
                    srv_node,
                    name=t.get("name", "?"),
                    description=t.get("description", ""),
                    json_schema=t.get("json_schema", {}) or {},
                )
            if not tools and srv_status in ("connected", "CONNECTED"):
                srv_node.add_leaf("[dim](此 server 未注册任何工具)[/]")

    def _populate_tree_from_pool(self, tree: Tree, connections: list) -> None:
        """Populate Tree from _mcp_pool (backward compat for tests / direct access)."""
        for conn in connections:
            cfg = conn.config
            if conn.error == "disabled":
                state = "[bold $text-disabled]DISABLED[/]"
            elif conn.error:
                state = "[bold $error]ERROR[/]"
            else:
                state = "[bold $success]CONNECTED[/]"

            target = cfg.command if cfg.transport == "stdio" else cfg.url
            label = f"{cfg.name}  [{cfg.transport}]  {state}  [bold]tools:{len(conn.tools)}[/]"
            if target:
                label += f"\n    [dim]target: {target}[/]"
            if conn.error and conn.error != "disabled":
                label += f"\n    [$error]error: {conn.error}[/]"

            srv_node = tree.root.add(label, expand=False)
            for t in conn.tools:
                self._add_tool_node(
                    srv_node,
                    name=getattr(t, "name", t) if hasattr(t, "name") else str(t),
                    description=getattr(t, "description", "") if hasattr(t, "description") else "",
                    json_schema=getattr(t, "parameters", {}) if hasattr(t, "parameters") else {},
                )
            if not conn.tools:
                srv_node.add_leaf("[dim](此 server 未注册任何工具)[/]")

    def _add_tool_node(self, srv_node, *, name: str, description: str, json_schema: dict[str, Any]) -> None:
        """Add an expandable tool node with description and input schema details."""
        tool_node = srv_node.add(f"[bold $success]\U0001f527 {name}[/]", expand=False)
        if description:
            tool_node.add_leaf(f"[italic $text-muted]描述: {description}[/]")

        schema_text = _format_tool_schema(json_schema)
        if schema_text:
            schema_node = tool_node.add("[bold]输入参数[/]", expand=False)
            for line in schema_text.splitlines():
                schema_node.add_leaf(f"[dim]{line}[/]")
        else:
            tool_node.add_leaf("[dim]输入参数: 无[/]")


def _format_tool_schema(schema: dict[str, Any]) -> str:
    """Format a tool input schema for display in the MCP tree."""
    if not schema:
        return ""
    try:
        return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        return str(schema)


def _server_state_label(srv_status: str, error: str | None, enabled: bool) -> str:
    """Rich markup label for a server's connection state."""
    if not enabled:
        return "[bold $text-disabled]DISABLED[/]"
    if srv_status == "ERROR" or error:
        return "[bold $error]ERROR[/]"
    if srv_status in ("connected", "CONNECTED"):
        return "[bold $success]CONNECTED[/]"
    if srv_status == "connecting":
        return "[italic]connecting[/]"
    return srv_status.upper()

    # ── session management（通过 bus）──────────────────────────────────

    @work(exclusive=True)
    async def _show_session_list(self) -> None:
        """Show a list of saved sessions for the user to pick from."""
        try:
            sessions = await self._bus.request(
                ListSessions(session_id=self._history.session_id)
            )
        except Exception:
            sessions = []

        if not sessions:
            self._show_page("会话列表", "  [No saved sessions found]", mode="resume")
            return

        self._view_state.showing_session_list = True
        self._view_state.session_options = sessions

        lines = ["\U0001f4cb Saved sessions (type number to resume, or anything else to cancel):", ""]
        for i, s in enumerate(sessions, 1):
            created = getattr(s, "created_at", "") or ""
            try:
                dt = datetime.fromisoformat(created)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                time_str = created[:16] if created else "unknown"
            preview = getattr(s, "first_message", "") or "(empty)"
            msg_count = getattr(s, "message_count", 0)
            _sid = getattr(s, "session_id", "")
            lines.append(f"  {i}. [{time_str}] {preview}  ({msg_count} msgs)")

        self._show_page("会话列表", "\n".join(lines), mode="resume")

    def _handle_session_selection(self, user_input: str) -> None:
        """Handle user's session selection."""
        vs = self._view_state
        vs.showing_session_list = False

        try:
            idx = int(user_input) - 1
            if 0 <= idx < len(vs.session_options):
                session = vs.session_options[idx]
                sid = getattr(session, "session_id", None) or session.get("session_id") if isinstance(session, dict) else getattr(session, "session_id", "")
                self._resume_session(sid)
                return
        except (ValueError, AttributeError):
            pass

        self._dismiss_panels()
        self._notif.show_toast("已取消恢复会话", duration=2)

    @work(exclusive=True)
    async def _resume_session(self, session_id: str) -> None:
        """Resume a saved session — restore memory first, then render UI."""
        try:
            messages = await self._bus.request(
                LoadSession(session_id=session_id)
            )
        except Exception:
            messages = None

        if messages is None:
            self._notif.show_toast(f"会话 {session_id} 加载失败", duration=2)
            return

        # LoadSession 返回的是消息列表，构造 bundle 格式
        bundle = {
            "session_id": session_id,
            "messages": messages,
            "cron_history": [],
        }

        self._history = ChatHistory(session_id=session_id)
        self._history.restore_from_bundle(bundle)
        self._projector.cron_renderers.clear()
        self._view_state.reset()
        self._notif.dismiss_feedback()

        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._bus.request(ReplaceMemory(
                session_id=session_id,
                messages=self._history.loaded_messages,
            ))

            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.remove_children()
            for turn in self._history.turns:
                render_turn(chat_view, turn,
                            thinking_expanded=self._thinking_expanded,
                            skills_expanded=self._skills_expanded,
                            tool_output_expanded=self._tool_output_expanded)
            chat_view.scroll_end()
            self._dismiss_panels()
        finally:
            input_widget.disabled = False

    @work(exclusive=True)
    async def _clear_chat(self) -> None:
        """Clear chat history and view — memory first, then UI."""
        input_widget = self.query_one("#input-box", Input)
        input_widget.disabled = True
        try:
            await self._bus.request(ClearMemory(session_id=self._history.session_id))
            self._history.clear()
            self._projector.cron_renderers.clear()
            self._view_state.reset()
            self._notif.dismiss_feedback()
            chat_view = self.query_one("#chat-view", VerticalScroll)
            chat_view.remove_children()
            self._dismiss_panels()
        finally:
            input_widget.disabled = False

    @work(exclusive=True)
    async def _run_merge_skills(self) -> None:
        """Run LLM-based skill merging."""
        chat_view = self.query_one("#chat-view", VerticalScroll)

        try:
            all_skills = await self._bus.request(
                ListSkills(session_id=self._history.session_id)
            )
            before_count = len([s for s in all_skills if s["status"] != "DEPRECATED"])
        except Exception:
            before_count = 0

        status_widget = Static(f"  \U0001f527 Merging skills... ({before_count} skills, this may take a moment)")
        chat_view.mount(status_widget)
        chat_view.scroll_end()

        try:
            result = await self._bus.request(
                MergeSkills(session_id=self._history.session_id)
            )
            await status_widget.remove()

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
            await status_widget.remove()
            chat_view.mount(Static(f"  ✗ Merge failed: {e}"))

        chat_view.scroll_end()
