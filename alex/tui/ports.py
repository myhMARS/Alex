"""TUI ports — structural subtyping contracts for type-safe mixin composition.

TUI 直接通过 bus 与其他模块通信，不再依赖 AgentFacade 中间层。
"""

from __future__ import annotations

from typing import Any, Protocol

from alex.tui.chat_projector import ChatProjector
from alex.tui.notification_controller import NotificationController
from alex.tui.view_models import ChatHistory
from alex.tui.view_state import SessionViewState


class _ControllerHost(Protocol):
    """Interface ChatControllerMixin expects from its host Textual App.

    AlexApp satisfies every attribute; the Protocol exists so the mixin
    never reaches into a concrete App via duck typing without a contract.
    """

    # Alex-specific attributes set up by AlexApp.__init__
    _bus: Any  # MessageBus
    _history: ChatHistory
    _view_state: SessionViewState
    _projector: ChatProjector
    _notif: NotificationController
    _thinking_expanded: bool
    _skills_expanded: bool
    _tool_output_expanded: bool

    # Optional MCP state
    _mcp_status_message: str
    _mcp_pool: Any  # MCPClientPool | None — avoid import for optional dep

    # Mixin methods called within the mixin itself via self
    def _show_page(self, title: str, content: str, *, mode: str) -> None: ...
    def _dismiss_panels(self) -> None: ...
    def _resume_session(self, session_id: str) -> None: ...

    # Textual App methods used by the mixin
    def query_one(self, selector: str, expect_type: type) -> Any: ...
    def query(self, selector: type | str) -> Any: ...
