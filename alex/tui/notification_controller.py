"""NotificationController — toast messages and feedback prompts.

Extracted from ChatControllerMixin so the TUI controller no longer owns
toast lifecycle, feedback widget state, or rating logic directly.
"""

from __future__ import annotations

import asyncio

from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from alex.tools.permissions import ToolApprovalRequest
from alex.tui.confirm_screen import PermissionConfirmScreen
from alex.tui.view_state import SessionViewState


class NotificationController:
    """Manages toast notifications, feedback prompts, and rating submission.

    Owns the toast widget (*_toast_widget*, *_toast_timer*) and the
    inline feedback widget (*_feedback_widget*).  Rating actions delegate
    here so the controller only needs to bind keys, not manage UI state.
    """

    def __init__(self, app: App, view_state: SessionViewState) -> None:
        self._app = app
        self._view = view_state
        self._toast_widget: Static | None = None
        self._toast_timer: object = None
        self._feedback_widget: Static | None = None
        self._confirm_lock: asyncio.Lock = asyncio.Lock()

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

    # ── permission confirm ──────────────────────────────────────────────

    async def confirm_permission(self, request: ToolApprovalRequest) -> tuple[bool, bool]:
        """Show a modal asking the user to approve *request*.

        Returns ``(granted, remember)``.  When the screen cannot be
        pushed (e.g. the app is shutting down) the request is denied.

        Concurrent permission requests are serialised via an internal
        lock so a cron-driven tool call cannot race with a user-turn
        modal — the second request waits for the first dialog to close.
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
                self._app.push_screen(
                    PermissionConfirmScreen(request),
                    _on_result,
                )
            except Exception:
                return (False, False)

            try:
                return await future
            except asyncio.CancelledError:
                return (False, False)
