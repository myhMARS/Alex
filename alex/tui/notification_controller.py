"""NotificationController — toast messages, feedback prompts, and permission confirmation.

Extracted from ChatControllerMixin so the TUI controller no longer owns
toast lifecycle, feedback widget state, or rating logic directly.

权限确认流程（完全通过 bus 事件）：
1. 订阅 ToolApprovalRequested 事件
2. 收到后弹出确认弹窗
3. 用户操作后发布 ToolApprovalResolved 事件到 bus
4. 工具模块从 bus 获取确认结果
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual.app import App
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Static

from alex.kernel.contracts.tools import ToolApprovalRequested, ToolApprovalResolved
from alex.kernel.dto.approval import ToolApprovalRequest
from alex.tui.confirm_screen import PermissionConfirmScreen
from alex.tui.view_state import SessionViewState

logger = logging.getLogger(__name__)


class NotificationController:
    """Manages toast notifications, feedback prompts, and permission confirmation.

    Owns the toast widget (*_toast_widget*, *_toast_timer*) and the
    inline feedback widget (*_feedback_widget*).  Rating actions delegate
    here so the controller only needs to bind keys, not manage UI state.

    权限确认通过 bus 事件驱动：
    - 订阅 ToolApprovalRequested → 弹出确认弹窗
    - 用户确认/拒绝后 → 发布 ToolApprovalResolved 到 bus
    """

    def __init__(self, app: App, view_state: SessionViewState) -> None:
        self._app = app
        self._view = view_state
        self._bus: Any = None
        self._toast_widget: Static | None = None
        self._toast_timer: Timer | None = None
        self._feedback_widget: Static | None = None
        self._confirm_lock: asyncio.Lock = asyncio.Lock()

    async def bind_bus(self, bus: Any) -> None:
        """绑定 bus 并订阅权限请求事件。"""
        self._bus = bus
        await bus.subscribe(ToolApprovalRequested, self._on_approval_requested)

    async def _on_approval_requested(self, event: ToolApprovalRequested) -> None:
        """收到权限请求事件 → 在 app worker 中弹出确认弹窗。

        bus dispatch loop 中不能直接 await 弹窗（会阻塞），也不能用
        asyncio.create_task（会丢失 Textual app context）。
        使用 app.run_worker 确保在正确的上下文中执行。
        """
        logger.info("approval popup tool=%s perm=%s req_id=%s", event.tool_name, event.permission, event.req_id)
        self._app.run_worker(self._handle_approval(event), exclusive=False)

    async def _handle_approval(self, event: ToolApprovalRequested) -> None:
        """在独立 task 中处理权限确认弹窗。"""
        request = ToolApprovalRequest(
            tool_name=event.tool_name,
            permission=event.permission,
            summary=event.preview,
        )

        granted, remember = await self._show_confirm_modal(request)
        logger.info("approval result req_id=%s granted=%s remember=%s", event.req_id, granted, remember)

        if self._bus:
            self._bus.publish(ToolApprovalResolved(
                req_id=event.req_id,
                granted=granted,
                remember=remember,
            ))

    # ── toast ───────────────────────────────────────────────────────────

    def dismiss_toast(self) -> None:
        if self._toast_widget:
            self._toast_widget.set_class(True, "alex-toast-hidden")
        if self._toast_timer:
            self._toast_timer.stop()
            self._toast_timer = None

    def show_toast(self, message: str, duration: float = 2) -> None:
        if self._toast_widget is None:
            self._toast_widget = Static("", classes="alex-toast alex-toast-hidden")
            self._app.mount(self._toast_widget)
        self._toast_widget.update(message)
        self._toast_widget.set_class(False, "alex-toast-hidden")
        self._toast_timer = self._app.set_timer(duration, self.dismiss_toast)

    @staticmethod
    def format_reflect_toast(evt) -> str:
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

    # ── feedback ────────────────────────────────────────────────────────

    def show_feedback_prompt(self) -> None:
        """Show inline feedback prompt after a response."""
        self._view.last_response_rated = False
        if self._feedback_widget:
            self._feedback_widget.remove()
        chat_view = self._app.query_one("#chat-view", VerticalScroll)
        prompt = Static(
            "\U0001f44d Ctrl+G Good  \U0001f44e Ctrl+B Bad  ⏎ skip",
            classes="feedback-prompt",
        )
        self._feedback_widget = prompt
        chat_view.mount(prompt)

    def dismiss_feedback(self) -> None:
        """Dismiss the feedback prompt without rating (implicit skip)."""
        self._view.last_response_rated = True
        if self._feedback_widget:
            self._feedback_widget.remove()
            self._feedback_widget = None

    def rate_response(self, good: bool, agent, pending_turn_id: str) -> None:
        """Submit user rating for the last response."""
        if self._view.last_response_rated:
            return
        self._view.last_response_rated = True

        agent.provide_feedback(good, turn_id=pending_turn_id)
        self._view.pending_feedback_turn_id = ""
        if not good:
            self.show_toast("已标记为不满意，正在反思…", duration=2)

        if self._feedback_widget:
            self._feedback_widget.remove()
        label = "✓ Rated as helpful" if good else "✗ Rated as unhelpful"
        self._feedback_widget = Static(label, classes="feedback-done")
        self._app.query_one("#chat-view", VerticalScroll).mount(self._feedback_widget)

    # ── permission confirm（通过 bus 事件驱动）──────────────────────────

    async def _show_confirm_modal(self, request: ToolApprovalRequest) -> tuple[bool, bool]:
        """内部方法：弹出权限确认弹窗并等待用户操作。

        并发权限请求通过锁串行化，避免多个弹窗同时出现。
        """
        async with self._confirm_lock:
            future: asyncio.Future[tuple[bool, bool]] = asyncio.get_event_loop().create_future()

            def _on_result(result: tuple[bool, bool] | None) -> None:
                if future.done():
                    return
                if result is None:
                    future.set_result((False, False))
                else:
                    future.set_result(result)

            try:
                await self._app.push_screen(
                    PermissionConfirmScreen(request),
                    _on_result,
                )
            except Exception:
                return False, False

            try:
                return await future
            except asyncio.CancelledError:
                return False, False
