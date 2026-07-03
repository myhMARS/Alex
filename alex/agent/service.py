"""Agent — 对话引擎核心，只依赖 bus。由 AgentModule 管理生命周期。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from alex.agent.chat_service import ChatAppService
from alex.bus import AsyncEventBus

if TYPE_CHECKING:
    from alex.llm.client import ChatClient

logger = logging.getLogger(__name__)


class Agent:
    """对话 agent — bus 是唯一外部依赖。

    职责：
    - 管理 ChatAppService（LLM 对话 + TurnProcessor）
    - 提供 chat_stream 供 AgentModule 委托调用
    - 提供 load_skill 供工具注册使用
    """

    def __init__(
        self,
        bus: AsyncEventBus,
        *,
        system_prompt: str = "You are a helpful AI assistant.",
        max_iterations: int = 15,
        callbacks: list | None = None,
        llm: ChatClient | None = None,
    ) -> None:
        self._bus = bus
        self._session_id: str = ""

        self._chat = ChatAppService(
            llm=llm, bus=bus, system_prompt=system_prompt,
            max_iterations=max_iterations, callbacks=callbacks or [],
        )

    # ── session ──────────────────────────────────────────────────────

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._chat.set_session_context(session_id, cron_history)

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── bus ──────────────────────────────────────────────────────────

    @property
    def bus(self):
        return self._bus

    def bind_event_bus(self, bus: AsyncEventBus) -> None:
        self._bus = bus
        self._chat.set_event_bus(bus)

    # ── lifecycle ────────────────────────────────────────────────────

    async def execute_cron_prompt(self, *, session_id: str, job_id: str, name: str, prompt: str, stream_id: str, wait_until_done: bool = True) -> str:
        return await self._chat.execute_cron_prompt(session_id=session_id, job_id=job_id, name=name, prompt=prompt, stream_id=stream_id, wait_until_done=wait_until_done)

    async def start_services(self) -> None:
        if not self._chat.has_llm():
            from alex.llm.client import ChatClient
            from alex.config import get_llm_config
            self._chat.set_llm(ChatClient(get_llm_config()))
        try:
            await self._bus.start()
        except Exception:
            pass

    async def shutdown(self) -> None:
        await self._chat.shutdown()

    # ── chat ────────────────────────────────────────────────────────

    async def chat_stream(self, user_message: str) -> None:
        """执行用户 turn — 事件通过 bus 广播。session_id 作为不可变参数传入。"""
        await self._chat.chat_stream(user_message, session_id=self._session_id)

    @property
    def last_turn_result(self):
        return self._chat.last_turn_result
