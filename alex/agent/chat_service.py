"""ChatAppService — owns the user-turn chat lifecycle.

Extracted from Agent so the facade stays thin.  Handles chat_stream,
tool execution, graph management, and prompt refresh.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool as LCBaseTool, StructuredTool
from pydantic import BaseModel, Field

from alex.agent.cron_handler import CronTurnHandler
from alex.agent.orchestrator import TurnOrchestrator
from alex.agent.prompt import PromptAssembler
from alex.bus import AsyncEventBus
from alex.bus.events import CronJobEvent, UserTurnRequested
from alex.memory.base import MemoryBase
from alex.skill.models import SkillManager
from alex.tools.executor import ToolExecutor
from alex.tools.registry import ToolRegistry


class LoadSkillInput(BaseModel):
    skill_name: str = Field(description="Name of the skill to load from the directory")


class CronHistoryInput(BaseModel):
    query: str = Field(default="", description="Optional job id, execution id, status, or partial task name")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of history entries to return")


class ChatAppService:
    """Application service for user-turn chat streaming and tool execution.

    Owns the graph, orchestrator, cron handler, and tool registry.
    Depends on PromptAssembler for system prompt and FeedbackRecorder
    for post-turn episode recording.
    """

    def __init__(
        self,
        llm: BaseChatModel,
        memory: MemoryBase,
        skill_manager: SkillManager,
        system_prompt: str,
        max_iterations: int = 5,
        callbacks: list[BaseCallbackHandler] | None = None,
        event_bus: AsyncEventBus | None = None,
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
        self._turn_lock = asyncio.Lock()

        self._tool_registry = ToolRegistry()
        self._tool_executor = ToolExecutor(self._tool_registry)
        self._prompt = PromptAssembler(system_prompt, skill_manager)

        self._orchestrator = TurnOrchestrator(
            llm, memory, skill_manager,
            self.push_notification, self._turn_lock,
            max_iterations, callbacks,
        )
        self._cron_handler = CronTurnHandler(
            llm, memory, self._tool_executor,
            self.push_notification, self._turn_lock,
            max_iterations, callbacks,
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
        self._tool_registry.register(tool)
        self._graph = self._build_graph()

    def register_tools_batch(self, tools: list[LCBaseTool]) -> None:
        for t in tools:
            self._tool_registry.register(t)
        self._graph = self._build_graph()

    def register_builtin_tools(
        self,
        load_skill_fn: callable,
        cron_history_fn: callable,
    ) -> None:
        """Register the built-in load_skill and cron_history tools."""
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
            coroutine=cron_history_fn,
            name="cron_history",
            description=(
                "Query completed cron executions from the current chat session. "
                "Returns status, start/end time, params, and result/error."
            ),
            args_schema=CronHistoryInput,
        ))

    def unregister_tool(self, name: str) -> None:
        self._tool_registry.unregister(name)
        self._graph = self._build_graph()

    def get_tool(self, name: str) -> LCBaseTool | None:
        return self._tool_registry.get(name)

    # ── session context ───────────────────────────────────────────────────

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._cron_history = list(cron_history or [])
        self._orchestrator.set_session_id(session_id)
        self._cron_handler.set_session_id(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── bus ────────────────────────────────────────────────────────────────

    def set_event_bus(self, bus: AsyncEventBus | None) -> None:
        self._bus = bus

    def push_notification(self, event) -> None:
        if self._bus is not None:
            self._bus.publish(event)
        if isinstance(event, CronJobEvent) and event.subscribe:
            try:
                asyncio.create_task(self._cron_handler.handle(event, self._graph))
            except RuntimeError:
                pass

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

    # ── chat stream ───────────────────────────────────────────────────────

    @property
    def last_turn_result(self):
        return self._orchestrator.last_result

    @property
    def orchestrator(self) -> TurnOrchestrator:
        return self._orchestrator

    @property
    def prompt(self) -> PromptAssembler:
        return self._prompt

    async def chat_stream(self, user_message: str) -> AsyncIterator:
        self.push_notification(UserTurnRequested(
            session_id=self._session_id, user_text=user_message,
        ))

        if self._prompt.ensure_skills_prompt(user_message):
            self._graph = self._build_graph()

        async for event in self._orchestrator.run(user_message, self._graph):
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
            blocks.append(
                "\n".join([
                    f"[{rec.get('execution_id', '')}] {rec.get('name', '')} ({rec.get('status', '')})",
                    f"job_id: {rec.get('job_id', '')}",
                    f"action: {rec.get('action', '')}",
                    f"started_at: {started_s}",
                    f"finished_at: {finished_s}",
                    f"params: {json.dumps(rec.get('params', {}), ensure_ascii=False)}",
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
        str(rec.get("action", "")),
    ]
    return any(q in item.lower() for item in haystacks if item)
