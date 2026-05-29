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
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool as LCBaseTool

from alex.agent.chat_service import ChatAppService
from alex.agent.composition import (
    create_default_llm,
    create_default_memory,
    create_default_skill_service,
)
from alex.agent.cron_service import CronService
from alex.agent.feedback_service import FeedbackAppService
from alex.agent.session_service import SessionService
from alex.agent.skill_admin_service import SkillAdminAppService
from alex.bus import AsyncEventBus
from alex.memory.base import MemoryBase
from alex.skill import SkillService
from alex.tools.permissions import PermissionPolicy

logger = logging.getLogger(__name__)


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
        self._llm = llm or create_default_llm()
        self._system_prompt = system_prompt or "You are a helpful AI assistant."
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._memory = memory or create_default_memory()
        self._skills = skill_manager or create_default_skill_service()
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
            cron_jobs_fn=self._create_cron_jobs_fn(),
        )
        if tools:
            self._chat.register_tools_batch(tools)

        self._cron = CronService(self._push_notification_from_scheduler)

    # ── built-in tool factories ──────────────────────────────────────────

    def _create_load_skill_fn(self):
        skill_admin = self._skill_admin
        async def _load_skill(skill_name: str) -> str:
            return await skill_admin.load_skill(skill_name)
        return _load_skill

    def _filter_cron_jobs(self, query: str = "", limit: int = 10) -> list[dict]:
        jobs = list(self.list_cron_jobs())
        q = (query or "").strip().lower()
        if q:
            jobs = [
                job for job in jobs
                if q in str(job.get("id", "")).lower()
                or q in str(job.get("name", "")).lower()
                or q in str(job.get("status", "")).lower()
                or q in str(job.get("cron", "")).lower()
                or q in str(job.get("prompt", "")).lower()
            ]
        return jobs[: max(1, min(int(limit), 50))]

    @staticmethod
    def _fmt_ts(ts: float | None) -> str:
        if not ts:
            return "-"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    def format_cron_jobs(self, query: str = "", limit: int = 10) -> str:
        jobs = self._filter_cron_jobs(query=query, limit=limit)
        if not jobs:
            return "No cron jobs found."
        blocks: list[str] = []
        for job in jobs:
            prompt = str(job.get("prompt") or "")
            if len(prompt) > 160:
                prompt = prompt[:160] + "..."
            last_outcome = str(job.get("last_result") or job.get("last_error") or "")
            if len(last_outcome) > 160:
                last_outcome = last_outcome[:160] + "..."
            blocks.append(
                "\n".join([
                    f"- [{job.get('id', '')}] {job.get('name', '')}",
                    f"  status: {job.get('status', '')}",
                    f"  cron: {job.get('cron', '')}",
                    f"  recurring: {job.get('recurring', True)}",
                    f"  durable: {job.get('durable', False)}",
                    f"  next_run: {self._fmt_ts(job.get('next_run_at'))}",
                    f"  last_started: {self._fmt_ts(job.get('last_started_at'))}",
                    f"  last_finished: {self._fmt_ts(job.get('last_finished_at'))}",
                    f"  prompt: {prompt}",
                    f"  last_outcome: {last_outcome}",
                ])
            )
        return "\n\n".join(blocks)

    def _create_cron_jobs_fn(self):
        async def _cron_jobs(query: str = "", limit: int = 10) -> str:
            return self.format_cron_jobs(query=query, limit=limit)
        return _cron_jobs

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
        await self._cron.start_services(
            runner=self.execute_cron_prompt,
            session_id=self.session_id,
        )

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

    def _publish_bus_event(self, event) -> None:
        if self._bus is not None:
            self._bus.publish(event)

    def _push_notification_from_scheduler(self, event) -> None:
        """Bridge sync scheduler callbacks to the shared bus publisher."""
        self._publish_bus_event(event)

    async def push_notification(self, event) -> None:
        """Publish *event* to the bus — the single event publishing path."""
        self._publish_bus_event(event)

    async def execute_tool_action(self, session_id: str, action: str, params: dict) -> str:
        return await self._chat.execute_tool_action(session_id, action, params)

    async def execute_cron_prompt(
        self,
        session_id: str,
        job_id: str,
        name: str,
        prompt: str,
        stream_id: str,
        wait_until_done: bool = True,
    ) -> str:
        return await self._chat.execute_cron_prompt(
            session_id=session_id,
            job_id=job_id,
            name=name,
            prompt=prompt,
            stream_id=stream_id,
            wait_until_done=wait_until_done,
        )

    async def schedule_cron_job(
        self,
        *,
        cron: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
    ) -> str:
        return await self._cron.schedule(
            session_id=self.session_id,
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            durable=durable,
            runner=self.execute_cron_prompt,
        )

    def list_cron_jobs(self) -> list[dict]:
        return self._cron.list_jobs()

    async def cancel_cron_job(self, job_id: str) -> bool:
        return await self._cron.cancel(job_id)

    async def shutdown(self) -> None:
        await self._cron.shutdown()
        await self._chat.shutdown()

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
