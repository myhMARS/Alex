"""Agent facade — wires together prompt, orchestration, cron, and feedback."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool as LCBaseTool, StructuredTool
from pydantic import BaseModel, Field

from alex.agent.cron_handler import CronTurnHandler
from alex.agent.feedback import FeedbackRecorder
from alex.agent.orchestrator import TurnOrchestrator
from alex.agent.prompt import PromptAssembler
from alex.agent.session_service import SessionService
from alex.bus import AsyncEventBus
from alex.bus.events import CronJobEvent, UserTurnRequested
from alex.config import get_llm_config
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.agent.cron_service import CronService
from alex.skill.models import SkillManager
from alex.tools.executor import ToolExecutor
from alex.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class LoadSkillInput(BaseModel):
    skill_name: str = Field(description="Name of the skill to load from the directory")


class CronHistoryInput(BaseModel):
    query: str = Field(default="", description="Optional job id, execution id, status, or partial task name")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of history entries to return")


class Agent:
    """Conversational agent with tool-use, memory, skills, and streaming.

    Serves as a facade that wires together PromptAssembler, TurnOrchestrator,
    CronTurnHandler, and FeedbackRecorder — each owning a distinct part of
    the turn lifecycle.
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        max_iterations: int = 5,
        tools: list[LCBaseTool] | None = None,
        callbacks: list[BaseCallbackHandler] | None = None,
        memory: MemoryBase | None = None,
        skill_manager: SkillManager | None = None,
        llm: BaseChatModel | None = None,
        event_bus: AsyncEventBus | None = None,
    ) -> None:
        self._llm = llm or LLMFactory.create(get_llm_config())
        self._system_prompt = system_prompt or "You are a helpful AI assistant."
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._memory = memory or BufferMemory()
        self._skills = skill_manager or SkillManager()
        self._tool_registry = ToolRegistry()
        self._tool_executor = ToolExecutor(self._tool_registry)
        self._session_id: str = ""
        self._cron_history: list[dict] = []
        self._bus = event_bus
        self._turn_lock = asyncio.Lock()
        self._reflecting = False
        self._turn_skill_ids: dict[str, list[str]] = {}  # turn_id → skill_ids

        # Sub-components
        self._session = SessionService()
        self._prompt = PromptAssembler(self._system_prompt, self._skills)
        self._feedback = FeedbackRecorder(self._memory, self._skills, self._llm, self.push_notification)
        self._orchestrator = TurnOrchestrator(
            self._llm, self._memory, self._skills,
            self.push_notification, self._turn_lock,
            max_iterations, self._callbacks,
        )
        self._cron_handler = CronTurnHandler(
            self._llm, self._memory, self._tool_executor,
            self.push_notification, self._turn_lock,
            max_iterations, self._callbacks,
        )

        # Cron service uses push_notification as its callback
        self._cron = CronService(self.push_notification)

        # Register built-in tools
        self._tool_registry.register(self._create_load_skill_tool())
        self._tool_registry.register(self._create_cron_history_tool())
        if tools:
            for t in tools:
                self._tool_registry.register(t)
        self._graph = self._build_graph()

    # ── graph management ──────────────────────────────────────────────────

    def _build_graph(self):
        tools = self._tool_registry.list() or None
        return create_agent(
            model=self._llm,
            tools=tools,
            system_prompt=self._prompt.augmented_prompt,
        )

    def _create_load_skill_tool(self) -> StructuredTool:
        async def _load_skill(skill_name: str) -> str:
            skill = self._skills.get_skill_by_name(skill_name)
            if skill:
                return f"[Skill: {skill.name}]\n\nWhen to apply: {skill.pattern}\n\nExecution methodology:\n{skill.instruction}"
            names = [s.name for s in self._skills.list_all() if s.status != "DEPRECATED"]
            return f"Skill '{skill_name}' not found. Available: {', '.join(names)}"

        return StructuredTool.from_function(
            coroutine=_load_skill,
            name="load_skill",
            description=(
                "Load the full execution methodology for a skill from the skill directory. "
                "Use this when a skill's pattern matches the user's request and you need "
                "the step-by-step execution guide to properly handle this type of task."
            ),
            args_schema=LoadSkillInput,
        )

    def _create_cron_history_tool(self) -> StructuredTool:
        async def _cron_history(query: str = "", limit: int = 10) -> str:
            return self.format_cron_history(query=query, limit=limit)

        return StructuredTool.from_function(
            coroutine=_cron_history,
            name="cron_history",
            description=(
                "Query completed cron executions from the current chat session. "
                "Returns status, start/end time, params, and result/error."
            ),
            args_schema=CronHistoryInput,
        )

    # ── public API ───────────────────────────────────────────────────────

    @property
    def tools(self) -> list[LCBaseTool]:
        return self._tool_registry.list()

    def register_tool(self, tool: LCBaseTool) -> None:
        self._tool_registry.register(tool)
        self._graph = self._build_graph()

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._cron.bind_event_loop(loop)

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._cron_history = list(cron_history or [])
        self._orchestrator.set_session_id(session_id)
        self._cron_handler.set_session_id(session_id)
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
        records = list(self._cron_history)
        q = (query or "").strip().lower()
        if q:
            def _match(rec: dict) -> bool:
                haystacks = [
                    str(rec.get("execution_id", "")),
                    str(rec.get("job_id", "")),
                    str(rec.get("name", "")),
                    str(rec.get("status", "")),
                    str(rec.get("action", "")),
                ]
                return any(q in item.lower() for item in haystacks if item)
            records = [rec for rec in records if _match(rec)]
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

    async def start_services(self) -> None:
        await self._cron.start_services()

    def unregister_tool(self, name: str) -> None:
        self._tool_registry.unregister(name)
        self._graph = self._build_graph()

    def get_tool(self, name: str) -> LCBaseTool | None:
        return self._tool_registry.get(name)

    async def clear_history(self) -> None:
        await self._memory.clear(session_id=self._session_id)

    @property
    def history(self) -> list:
        return self._memory.get_context_sync(session_id=self._session_id)

    @property
    def bus(self) -> AsyncEventBus | None:
        return self._bus

    def bind_event_bus(self, bus: AsyncEventBus) -> None:
        self._bus = bus

    def push_notification(self, event) -> None:
        """Publish a typed event to the event bus.

        CronJobEvents with subscribe=True automatically trigger an LLM
        streaming reply via the CronTurnHandler.
        """
        if self._bus is not None:
            self._bus.publish(event)
        if isinstance(event, CronJobEvent) and event.subscribe:
            try:
                asyncio.create_task(self._cron_handler.handle(event, self._graph))
            except RuntimeError:
                pass

    async def execute_tool_action(self, session_id: str, action: str, params: dict) -> str:
        """Execute a tool by name — public API for cron / scheduler.

        session_id is the owning session for this cron job, not the
        current foreground session.
        """
        action = (action or "").strip()
        params = params or {}

        if action == "notify":
            return str(params.get("message", ""))

        result = await self._tool_executor.execute(session_id, action, params)
        if result.startswith("Error:"):
            raise ValueError(result)
        return result

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
        """Schedule a new cron job — public API for the cron tool."""
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
        """User feedback — records skill usage and triggers reflection on negative.

        When turn_id is provided, looks up skill IDs from the turn→skills
        mapping.  Otherwise falls back to the most recent turn's skill IDs.
        """
        if turn_id and turn_id in self._turn_skill_ids:
            skill_ids = self._turn_skill_ids.pop(turn_id)
        else:
            result = self._orchestrator.last_result
            skill_ids = result.loaded_skill_ids if result else []
        self._feedback.provide_feedback(positive, skill_ids)
        if not positive:
            try:
                asyncio.get_running_loop().create_task(self._feedback.reflect())
            except RuntimeError:
                pass

    # ── reflection / skills (public) ──────────────────────────────────────

    async def reflect(self) -> dict:
        """Force skill reflection. Returns {new, updated, deprecated, names}."""
        return await self._feedback.reflect()

    def list_skills(self) -> list[dict]:
        """List all skills with metadata for display."""
        all_skills = self._skills.list_all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "use_count": s.use_count,
                "success_count": s.success_count,
                "failure_count": s.failure_count,
                "pattern": s.pattern,
                "instruction": s.instruction,
                "tags": s.tags,
            }
            for s in all_skills
        ]

    def delete_skill(self, target: str) -> str | None:
        """Delete a skill by name or id prefix. Returns skill name or None."""
        found = None
        for s in self._skills.list_all():
            if s.id.startswith(target) or s.name.lower() == target.lower():
                found = s
                break
        if found:
            self._skills.remove_skill(found.id)
            return found.name
        return None

    def deprecate_skill(self, target: str) -> str | None:
        """Deprecate a skill by name or id prefix. Returns skill name or None."""
        found = None
        for s in self._skills.list_all():
            if s.id.startswith(target) or s.name.lower() == target.lower():
                found = s
                break
        if found:
            self._skills.deprecate_skill(found.id)
            return found.name
        return None

    async def merge_skills(self) -> dict:
        """LLM-based skill deduplication. Returns {merged, deprecated, remaining}."""
        return await self._skills.merge_skills(self._llm)

    # ── history restore (public) ──────────────────────────────────────────

    async def restore_history(self, messages: list) -> None:
        """Clear memory and replay a standard message sequence."""
        await self._session.restore_history(messages, self._memory, self._session_id)

    # ── streaming chat ───────────────────────────────────────────────────

    @property
    def last_turn_result(self):
        """Return the last TurnResult, or None if no turn has completed."""
        return self._orchestrator.last_result

    async def chat_stream(self, user_message: str) -> AsyncIterator:
        """Streaming chat — delegates to TurnOrchestrator.

        Yields typed UI events (TokenEmitted, ThinkingUpdated, ToolStarted,
        ToolFinished, SkillLoaded).  Post-turn processing runs after the
        orchestrator exhausts; the consumer reads last_turn_result for
        finalization metadata.
        """
        # Publish command for observability
        self.push_notification(UserTurnRequested(
            session_id=self._session_id, user_text=user_message,
        ))

        # Ensure skills prompt is up-to-date
        if self._prompt.ensure_skills_prompt(user_message):
            self._graph = self._build_graph()

        # Run the turn via orchestrator
        async for event in self._orchestrator.run(user_message, self._graph):
            yield event

        # Post-turn processing — orchestrator has published TurnCompleted
        result = self._orchestrator.last_result
        if result:
            if result.turn_id and result.loaded_skill_ids:
                self._turn_skill_ids[result.turn_id] = list(result.loaded_skill_ids)
            loaded_names = [
                s.name for sid in result.loaded_skill_ids
                if (s := self._skills.get_skill(sid))
            ]
            self._feedback.record_episode(
                user_message, loaded_names, result.tool_names, result.content,
            )
            await self._feedback.maybe_reflect(result.last_query_matched)
