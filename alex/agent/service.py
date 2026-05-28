"""Agent facade — thin composition root that wires application services.

The Agent is now a thin facade.  Business logic lives in:
  - ChatAppService      (chat_stream, tool execution, graph)
  - SessionService      (session persistence boundary)
  - CronService         (cron scheduling / cancellation)
  - FeedbackAppService  (feedback recording, reflection)
  - SkillAdminAppService (skill CRUD, merging)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool as LCBaseTool

from alex.agent.chat_service import ChatAppService
from alex.agent.cron_service import CronService
from alex.agent.feedback_service import FeedbackAppService
from alex.agent.session_service import SessionService
from alex.agent.skill_admin_service import SkillAdminAppService
from alex.bus import AsyncEventBus
from alex.bus.events import CronJobEvent
from alex.config import get_llm_config
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.skill import SkillService
from alex.skill.repository import SkillStore
from alex.skill.reflector import Reflector
from alex.skill.matcher import SkillRetriever
from alex.skill.evolution import EvolutionEngine
from alex.tools.permissions import PermissionPolicy

logger = logging.getLogger(__name__)


def _create_default_skill_service() -> SkillService:
    """Create a SkillService with default lazy-constructed dependencies."""
    store = SkillStore()
    reflector = Reflector()
    retriever = SkillRetriever(store)
    evolution = EvolutionEngine()
    return SkillService(store=store, reflector=reflector, retriever=retriever, evolution=evolution)


class Agent:
    """Conversational agent with tool-use, memory, skills, and streaming.

    Thin facade that wires together ChatAppService, SessionService,
    CronService, FeedbackAppService, and SkillAdminAppService.
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        max_iterations: int = 5,
        tools: list[LCBaseTool] | None = None,
        callbacks: list[BaseCallbackHandler] | None = None,
        memory: MemoryBase | None = None,
        skill_manager: SkillService | None = None,
        llm: BaseChatModel | None = None,
        event_bus: AsyncEventBus | None = None,
        permissions: PermissionPolicy | None = None,
    ) -> None:
        self._llm = llm or LLMFactory.create(get_llm_config())
        self._system_prompt = system_prompt or "You are a helpful AI assistant."
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._memory = memory or BufferMemory()
        self._skills = skill_manager or _create_default_skill_service()
        self._session_id: str = ""
        self._bus = event_bus
        self._turn_skill_ids: dict[str, list[str]] = {}

        # ── Application services (order matters: _skill_admin before _chat) ─

        self._session = SessionService()

        self._skill_admin = SkillAdminAppService(
            skill_manager=self._skills,
            llm=self._llm,
        )

        self._feedback = FeedbackAppService(
            memory=self._memory,
            skill_manager=self._skills,
            llm=self._llm,
            push_notification=self.push_notification,
        )

        self._chat = ChatAppService(
            llm=self._llm,
            memory=self._memory,
            skill_manager=self._skills,
            system_prompt=self._system_prompt,
            max_iterations=max_iterations,
            callbacks=self._callbacks,
            event_bus=event_bus,
            permissions=permissions,
        )
        self._chat.register_builtin_tools(
            load_skill_fn=self._create_load_skill_fn(),
            cron_history_fn=self._create_cron_history_fn(),
        )
        if tools:
            self._chat.register_tools_batch(tools)

        self._cron = CronService(self.push_notification)

    # ── built-in tool factories ──────────────────────────────────────────

    def _create_load_skill_fn(self):
        skill_admin = self._skill_admin
        async def _load_skill(skill_name: str) -> str:
            return await skill_admin.load_skill(skill_name)
        return _load_skill

    def _create_cron_history_fn(self):
        format_fn = self._chat.format_cron_history
        async def _cron_history(query: str = "", limit: int = 10) -> str:
            return format_fn(query=query, limit=limit)
        return _cron_history

    # ── public API ───────────────────────────────────────────────────────

    @property
    def tools(self) -> list[LCBaseTool]:
        return self._chat.tools

    def register_tool(self, tool: LCBaseTool) -> None:
        self._chat.register_tool(tool)

    @property
    def permissions(self) -> PermissionPolicy:
        return self._chat.permissions

    def set_permissions(self, policy: PermissionPolicy) -> None:
        self._chat.set_permissions(policy)

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._cron.bind_event_loop(loop)

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._chat.set_session_context(session_id, cron_history)
        self._feedback.set_session_id(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def list_sessions(self) -> list[dict]:
        return self._session.list_sessions()

    def load_session(self, session_id: str) -> dict | None:
        return self._session.load_session(session_id)

    async def subscribe_store(self, bus) -> None:
        await self._session.subscribe_store(bus)

    def list_session_cron_history(self, query: str = "", limit: int = 20) -> list[dict]:
        return self._chat.list_session_cron_history(query=query, limit=limit)

    def format_cron_history(self, query: str = "", limit: int = 10) -> str:
        return self._chat.format_cron_history(query=query, limit=limit)

    async def start_services(self) -> None:
        await self._cron.start_services()

    def unregister_tool(self, name: str) -> None:
        self._chat.unregister_tool(name)

    def get_tool(self, name: str) -> LCBaseTool | None:
        return self._chat.get_tool(name)

    async def clear_history(self) -> None:
        await self._memory.clear(session_id=self._session_id)
        self._feedback.reset_session_state(self._session_id)

    @property
    def history(self) -> list:
        return self._memory.get_context_sync(session_id=self._session_id)

    @property
    def bus(self) -> AsyncEventBus | None:
        return self._bus

    def bind_event_bus(self, bus: AsyncEventBus) -> None:
        self._bus = bus
        self._chat.set_event_bus(bus)

    def push_notification(self, event) -> None:
        """Publish *event* to the bus and, for subscribed cron jobs,
        trigger the cron reply handler.

        This is the single entry point for all event publishing —
        there is no separate code path hiding behind chat_service.
        """
        # 1. Always publish to the event bus for observers (TUI, store, etc.)
        if self._bus is not None:
            self._bus.publish(event)

        # 2. Subscribed cron jobs additionally kick off an LLM reply turn
        if isinstance(event, CronJobEvent) and event.subscribe:
            self._chat.dispatch_cron_reply(event)

    async def execute_tool_action(self, session_id: str, action: str, params: dict) -> str:
        return await self._chat.execute_tool_action(session_id, action, params)

    async def schedule_cron_job(
        self,
        *,
        name: str,
        cron: str = "",
        interval_seconds: int | None = None,
        repeat: int = 1,
        subscribe: bool = False,
        run_now: bool = False,
        action: str = "",
        params: dict | None = None,
    ) -> str:
        return await self._cron.schedule(
            session_id=self.session_id,
            name=name,
            cron=cron,
            interval_seconds=interval_seconds,
            repeat=repeat,
            subscribe=subscribe,
            run_now=run_now,
            action=action,
            params=params or {},
            runner=self.execute_tool_action,
        )

    def list_cron_jobs(self) -> list[dict]:
        return self._cron.list_jobs()

    async def cancel_cron_job(self, job_id: str) -> bool:
        return await self._cron.cancel(job_id)

    async def shutdown(self) -> None:
        await self._cron.shutdown()

    @property
    def is_reflecting(self) -> bool:
        return self._feedback.is_reflecting

    def provide_feedback(self, positive: bool, turn_id: str = "") -> None:
        if turn_id and turn_id in self._turn_skill_ids:
            skill_ids = self._turn_skill_ids.pop(turn_id)
        else:
            result = self._chat.last_turn_result
            skill_ids = result.loaded_skill_ids if result else []
        self._feedback.provide_feedback(positive, skill_ids)
        if not positive:
            try:
                asyncio.get_running_loop().create_task(self._feedback.reflect())
            except RuntimeError:
                pass

    # ── reflection / skills (public) ──────────────────────────────────────

    async def reflect(self) -> dict:
        return await self._feedback.reflect()

    def list_skills(self) -> list[dict]:
        return self._skill_admin.list_skills()

    def delete_skill(self, target: str) -> str | None:
        return self._skill_admin.delete_skill(target)

    def deprecate_skill(self, target: str) -> str | None:
        return self._skill_admin.deprecate_skill(target)

    async def merge_skills(self) -> dict:
        return await self._skill_admin.merge_skills()

    # ── history restore (public) ──────────────────────────────────────────

    async def restore_history(self, messages: list) -> None:
        await self._session.restore_history(messages, self._memory, self._session_id)

    # ── streaming chat ───────────────────────────────────────────────────

    @property
    def last_turn_result(self):
        return self._chat.last_turn_result

    async def chat_stream(self, user_message: str) -> AsyncIterator:
        async for event in self._chat.chat_stream(user_message):
            yield event

        result = self._chat.last_turn_result
        if result:
            if result.turn_id and result.loaded_skill_ids:
                self._turn_skill_ids[result.turn_id] = list(result.loaded_skill_ids)
            loaded_names = [
                self._skill_admin.get_skill_name(sid)
                for sid in result.loaded_skill_ids
            ]
            self._feedback.record_episode(
                user_message, loaded_names, result.tool_names, result.content,
            )
            await self._feedback.maybe_reflect(result.last_query_matched)
