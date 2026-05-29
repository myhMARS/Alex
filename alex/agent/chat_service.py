"""ChatAppService — owns the user-turn chat lifecycle.

Extracted from Agent so the facade stays thin.  Handles chat_stream,
tool execution, graph management, and prompt refresh.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool as LCBaseTool, StructuredTool
from pydantic import BaseModel, Field

from alex.agent.prompt import PromptAssembler
from alex.agent.turn_processor import TurnProcessor
from alex.bus import AsyncEventBus
from alex.bus.events import UserTurnRequested
from alex.memory.base import MemoryBase
from alex.skill import SkillService
from alex.tools.executor import ToolExecutor
from alex.tools.permissions import (
    PermissionPolicy,
    gate_tool_with_policy,
    gate_tools_with_policy,
)
from alex.tools.registry import ToolRegistry


class LoadSkillInput(BaseModel):
    skill_name: str = Field(description="Name of the skill to load from the directory")


class CronJobsInput(BaseModel):
    query: str = Field(default="", description="Optional job id, status, cron expression, or partial prompt/name")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of cron jobs to return")


class ChatAppService:
    """Application service for user-turn chat streaming and tool execution.

    Owns the graph, unified turn processor, and tool registry.
    Depends on PromptAssembler for system prompt composition and exposes
    the turn result consumed by the feedback service for post-turn
    episode recording.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        memory: MemoryBase,
        skill_manager: SkillService,
        system_prompt: str,
        max_iterations: int = 5,
        callbacks: list[BaseCallbackHandler] | None = None,
        event_bus: AsyncEventBus | None = None,
        permissions: PermissionPolicy | None = None,
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._skills = skill_manager
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._session_id: str = ""
        self._cron_history: list[dict] = []
        self._bus = event_bus
        self._tool_registry = ToolRegistry()
        self._permissions = permissions or PermissionPolicy.from_env()
        self._tool_executor = ToolExecutor(self._tool_registry, permissions=self._permissions)
        self._prompt = PromptAssembler(system_prompt, skill_manager)

        self._turn_processor = TurnProcessor(
            llm=llm,
            memory=memory,
            skill_manager=skill_manager,
            push_notification=self.push_notification,
            get_bus=lambda: self._bus,
            graph_getter=lambda: self._graph,
            max_iterations=max_iterations,
            callbacks=callbacks,
        )

        self._graph = self._build_graph()

    # ── graph management ──────────────────────────────────────────────────

    def _build_graph(self):
        tools = self._tool_registry.list() or None
        return create_agent(
            model=self._llm,
            tools=tools,
            system_prompt=self._prompt.augmented_prompt,
        )

    def rebuild_graph(self) -> None:
        self._graph = self._build_graph()

    # ── tools ─────────────────────────────────────────────────────────────

    @property
    def tools(self) -> list[LCBaseTool]:
        return self._tool_registry.list()

    def register_tool(self, tool: LCBaseTool) -> None:
        gate_tool_with_policy(tool, self._permissions)
        self._tool_registry.register(tool)
        self._graph = self._build_graph()

    def register_tools_batch(self, tools: list[LCBaseTool]) -> None:
        gate_tools_with_policy(tools, self._permissions)
        for t in tools:
            self._tool_registry.register(t)
        self._graph = self._build_graph()

    def register_builtin_tools(
        self,
        load_skill_fn: callable,
        cron_jobs_fn: callable,
    ) -> None:
        """Register the built-in load_skill and cron_jobs tools."""
        self._tool_registry.register(StructuredTool.from_function(
            coroutine=load_skill_fn,
            name="load_skill",
            description=(
                "Load the full execution methodology for a skill from the skill directory. "
                "Use this when a skill's pattern matches the user's request and you need "
                "the step-by-step execution guide to properly handle this type of task."
            ),
            args_schema=LoadSkillInput,
        ))
        self._tool_registry.register(StructuredTool.from_function(
            coroutine=cron_jobs_fn,
            name="cron_jobs",
            description=(
                "List current cron jobs, including durable jobs restored from disk. "
                "Returns job id, schedule, status, prompt, and next run time."
            ),
            args_schema=CronJobsInput,
        ))

    def unregister_tool(self, name: str) -> None:
        self._tool_registry.unregister(name)
        self._graph = self._build_graph()

    def get_tool(self, name: str) -> LCBaseTool | None:
        return self._tool_registry.get(name)

    @property
    def permissions(self) -> PermissionPolicy:
        return self._permissions

    def set_permissions(self, policy: PermissionPolicy) -> None:
        """Replace the permission policy and propagate it to the executor.

        Already-registered tools have their gating wrappers updated in
        place so the new policy applies to subsequent invocations
        without rebuilding the graph.
        """
        self._permissions = policy
        self._tool_executor.set_permissions(policy)
        for tool in self._tool_registry.list():
            gate_tool_with_policy(tool, policy)

    # ── session context ───────────────────────────────────────────────────

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._cron_history = list(cron_history or [])
        self._turn_processor.set_session_id(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── bus ────────────────────────────────────────────────────────────────

    def set_event_bus(self, bus: AsyncEventBus | None) -> None:
        self._bus = bus

    async def push_notification(self, event) -> None:
        """Publish *event* to the bus — the single publishing path."""
        if self._bus is not None:
            self._bus.publish(event)

    async def shutdown(self) -> None:
        await self._turn_processor.shutdown()

    async def _ensure_session_loaded(self, session_id: str) -> None:
        if not session_id:
            return
        if await self._memory.get_context(session_id=session_id):
            return
        from alex.store.session import load_session

        saved = load_session(session_id)
        if saved:
            await self._memory.replace(session_id, saved)

    # ── tool execution (public for cron) ───────────────────────────────────

    async def execute_tool_action(self, session_id: str, action: str, params: dict) -> str:
        action = (action or "").strip()
        params = params or {}

        if action == "notify":
            return str(params.get("message", ""))

        from alex.tools.ports import ToolExecutionContext

        ctx = ToolExecutionContext(session_id=session_id, source="cron")
        result = await self._tool_executor.execute(ctx, action, params)
        if result.startswith("Error:"):
            raise ValueError(result)
        return result

    async def execute_cron_prompt(
        self,
        *,
        session_id: str,
        job_id: str,
        name: str,
        prompt: str,
        stream_id: str,
        wait_until_done: bool = True,
    ) -> str:
        await self._ensure_session_loaded(session_id)
        return await self._turn_processor.run_cron_turn(
            session_id=session_id,
            job_id=job_id,
            name=name,
            prompt=prompt,
            stream_id=stream_id,
            wait_until_done=wait_until_done,
        )

    # ── chat stream ───────────────────────────────────────────────────────

    @property
    def last_turn_result(self):
        return self._turn_processor.last_result

    @property
    def prompt(self) -> PromptAssembler:
        return self._prompt

    async def chat_stream(self, user_message: str) -> AsyncIterator:
        await self.push_notification(UserTurnRequested(
            session_id=self._session_id, user_text=user_message,
        ))

        if self._prompt.ensure_skills_prompt(user_message):
            self._graph = self._build_graph()

        async for event in self._turn_processor.stream_user_turn(
            user_message,
            session_id=self._session_id,
        ):
            yield event

    # ── cron history ──────────────────────────────────────────────────────

    def list_session_cron_history(self, query: str = "", limit: int = 20) -> list[dict]:
        records = list(self._cron_history)
        q = (query or "").strip().lower()
        if q:
            records = [rec for rec in records if _cron_record_matches(rec, q)]
        records.sort(key=lambda rec: float(rec.get("finished_at") or rec.get("started_at") or 0), reverse=True)
        return records[: max(1, min(int(limit), 50))]

    def format_cron_history(self, query: str = "", limit: int = 10) -> str:
        records = self.list_session_cron_history(query=query, limit=limit)
        if not records:
            return "No completed cron executions in the current session."
        blocks: list[str] = []
        for rec in records:
            started_at = rec.get("started_at")
            finished_at = rec.get("finished_at")
            started_s = datetime.fromtimestamp(started_at).isoformat(sep=" ", timespec="seconds") if started_at else "-"
            finished_s = datetime.fromtimestamp(finished_at).isoformat(sep=" ", timespec="seconds") if finished_at else "-"
            result = rec.get("result") or rec.get("error") or ""
            prompt = rec.get("prompt") or ""
            blocks.append(
                "\n".join([
                    f"[{rec.get('execution_id', '')}] {rec.get('name', '')} ({rec.get('status', '')})",
                    f"job_id: {rec.get('job_id', '')}",
                    f"durable: {rec.get('durable', False)}",
                    f"recurring: {rec.get('recurring', True)}",
                    f"started_at: {started_s}",
                    f"finished_at: {finished_s}",
                    f"prompt: {prompt}",
                    f"result: {result}",
                ])
            )
        return "\n\n".join(blocks)

    @property
    def cron_history(self) -> list[dict]:
        return self._cron_history


def _cron_record_matches(rec: dict, q: str) -> bool:
    haystacks = [
        str(rec.get("execution_id", "")),
        str(rec.get("job_id", "")),
        str(rec.get("name", "")),
        str(rec.get("status", "")),
        str(rec.get("prompt", "")),
    ]
    return any(q in item.lower() for item in haystacks if item)
