"""ChatAppService — owns the user-turn chat lifecycle.

All cross-module operations go through the :class:`MessageBus` using
kernel contracts.  The service holds NO direct references to memory,
skills, or tools objects — everything is bus-mediated.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from alex.agent.turn_processor import TurnProcessor
from alex.bus import AsyncEventBus
from alex.llm.client import ChatClient
from alex.kernel.bus import MessageBus
from alex.kernel.contracts.memory import AppendMessages, GetContext, ReplaceMemory
from alex.kernel.contracts.skills import LoadSkill, RetrieveSkills
from alex.kernel.contracts.tools import ExecuteTool, GetToolCatalog, RegisterTool, UnregisterTool
from alex.kernel.dto.skill import SkillCard
from alex.kernel.dto.tool import ToolExecutionContext, ToolResult, ToolSpec

_logger = logging.getLogger(__name__)


class _BusTurnServices:
    """TurnServices 的 bus 实现 — 将 TurnProcessor 的服务调用转为 bus request。"""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus

    async def get_memory_context(self, session_id: str) -> list[dict[str, Any]]:
        return await self._bus.request(GetContext(session_id=session_id))

    async def append_memory(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        await self._bus.request(AppendMessages(session_id=session_id, messages=messages))

    async def get_skill_by_name(self, skill_name: str) -> SkillCard | None:
        try:
            return await self._bus.request(LoadSkill(skill_name=skill_name))
        except Exception:
            _logger.debug("LoadSkill failed for '%s'", skill_name, exc_info=True)
            return None

    async def retrieve_skills(self, query: str, top_k: int = 3) -> list[SkillCard]:
        return await self._bus.request(RetrieveSkills(query=query, top_k=top_k))

    async def get_tool_catalog(self) -> list[ToolSpec]:
        return await self._bus.request(GetToolCatalog())

    async def execute_tool(self, ctx: Any, tool_name: str, tool_args: dict[str, Any]) -> ToolResult:
        from alex.kernel.errors import CapabilityTimeout, HandlerError, CapabilityUnavailable
        req = ExecuteTool(
            session_id=getattr(ctx, "session_id", ""),
            turn_id=getattr(ctx, "turn_id", "") or "",
            name=tool_name,
            args=tool_args,
            ctx=ctx,
        )
        try:
            return await self._bus.request(req, timeout=req.timeout)
        except CapabilityTimeout:
            return ToolResult(
                name=tool_name,
                error=f"Error: tool '{tool_name}' timed out after {req.timeout}s",
            )
        except CapabilityUnavailable:
            return ToolResult(
                name=tool_name,
                error=f"Error: tool '{tool_name}' unavailable (no handler registered)",
            )
        except HandlerError as e:
            return ToolResult(
                name=tool_name,
                error=f"Error: tool '{tool_name}' failed: {e}",
            )
        except Exception as e:
            return ToolResult(
                name=tool_name,
                error=f"Error: tool '{tool_name}' raised {type(e).__name__}: {e}",
            )


class _PromptAssembler:
    """Builds the system prompt with skill instructions injected via bus."""

    def __init__(self, system_prompt: str, bus: MessageBus) -> None:
        self._system_prompt = system_prompt
        self._bus = bus
        self._current = system_prompt

    @property
    def augmented_prompt(self) -> str:
        return self._current

    async def ensure_skills_prompt(self, query: str) -> bool:
        try:
            skills = await self._bus.request(RetrieveSkills(query=query, top_k=3))
        except Exception:
            return False
        if not skills:
            return False
        from alex.prompts import get_skills_section
        text = get_skills_section(skills=[{"name": s.name, "pattern": s.pattern} for s in skills])
        augmented = self._system_prompt + text if text else self._system_prompt
        if augmented != self._current:
            self._current = augmented
            return True
        return False


class ChatAppService:
    """Application service for user-turn chat streaming and tool execution.

    All external operations (memory, skills, tool registration, tool
    execution) go through the bus.  No direct references to other modules.
    """

    def __init__(
        self,
        llm: ChatClient | None,
        bus: AsyncEventBus,
        system_prompt: str,
        max_iterations: int = 5,
        callbacks: list | None = None,
    ) -> None:
        self._llm: ChatClient | None = llm
        self._bus: MessageBus = bus
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._session_id: str = ""
        self._cron_history: list[dict] = []
        self._prompt = _PromptAssembler(system_prompt, bus)

        self._turn_processor = TurnProcessor(
            llm=llm,
            push_notification=self.push_notification,
            services=_BusTurnServices(bus),
            get_system_prompt=lambda: self._prompt.augmented_prompt,
            max_iterations=max_iterations,
            callbacks=self._callbacks,
        )

    # ── bus ────────────────────────────────────────────────────────────

    def set_event_bus(self, bus: AsyncEventBus | None) -> None:
        if bus is not None:
            self._bus = bus

    async def push_notification(self, event) -> None:
        self._bus.publish(event)

    # ── tool registration (all via bus) ────────────────────────────────

    async def register_tool(
        self, *, name: str, description: str,
        json_schema: dict[str, Any] | None = None,
        callable_ref: Any = None, metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a tool via the bus — tools module stores it."""
        await self._bus.request(RegisterTool(
            name=name,
            description=description,
            json_schema=json_schema or {},
            callable_ref=callable_ref,
            metadata=metadata or {},
        ))

    async def unregister_tool(self, name: str) -> None:
        """Remove a tool via the bus."""
        await self._bus.request(UnregisterTool(name=name))

    # ── tool execution (via bus) ───────────────────────────────────────

    async def execute_tool_action(self, session_id: str, action: str, params: dict) -> str:
        action = (action or "").strip()
        params = params or {}
        if action == "notify":
            return str(params.get("message", ""))
        ctx = ToolExecutionContext(session_id=session_id, source="cron")
        result: ToolResult = await self._bus.request(ExecuteTool(
            session_id=session_id, name=action, args=params, ctx=ctx,
        ))
        if result.error:
            raise ValueError(result.error)
        return result.output

    # ── session context ───────────────────────────────────────────────

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        self._session_id = session_id
        self._cron_history = list(cron_history or [])
        self._turn_processor.set_session_id(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── cron ──────────────────────────────────────────────────────────

    async def _ensure_session_loaded(self, session_id: str) -> None:
        if not session_id:
            return
        history = await self._bus.request(GetContext(session_id=session_id))
        if history:
            return
        from alex.store.session import load_session
        saved = load_session(session_id)
        if saved:
            await self._bus.request(ReplaceMemory(session_id=session_id, messages=saved))

    async def execute_cron_prompt(
        self, *, session_id: str, job_id: str, name: str,
        prompt: str, stream_id: str, wait_until_done: bool = True,
    ) -> str:
        await self._ensure_session_loaded(session_id)
        return await self._turn_processor.run_cron_turn(
            session_id=session_id, job_id=job_id, name=name,
            prompt=prompt, stream_id=stream_id, wait_until_done=wait_until_done,
        )

    # ── LLM ───────────────────────────────────────────────────────────

    def has_llm(self) -> bool:
        return self._llm is not None

    def set_llm(self, llm: ChatClient) -> None:
        self._llm = llm
        self._turn_processor.set_llm(llm)

    async def shutdown(self) -> None:
        await self._turn_processor.shutdown()

    # ── chat stream ───────────────────────────────────────────────────

    @property
    def last_turn_result(self):
        return self._turn_processor.last_result

    async def chat_stream(self, user_message: str) -> None:
        """执行用户 turn — 事件通过 bus 广播。"""
        try:
            await self._bus.start()
        except Exception:
            pass
        try:
            await self._prompt.ensure_skills_prompt(user_message)
        except Exception:
            _logger.debug("ensure_skills_prompt failed", exc_info=True)
        await self._turn_processor.run_user_turn(
            user_message, session_id=self._session_id,
        )

    # ── cron history ──────────────────────────────────────────────────

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
