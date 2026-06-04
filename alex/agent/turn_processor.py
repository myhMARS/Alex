"""Unified turn processor — single FIFO queue for user and cron turns.

Uses a custom agent loop (OpenAI SDK). 不直接依赖 bus，
所有外部交互通过注入的 TurnServices 回调完成。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import json as _json

from alex import messages as msg
from alex.bus.events import (
    SkillLoaded,
    ThinkingUpdated,
    TokenEmitted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from alex.llm.client import (
    ChatClient,
    ContentDelta,
    StreamEnd,
    ThinkingDelta,
    ToolCallRequest,
)
from alex.kernel.dto.skill import SkillCard
from alex.kernel.dto.tool import ToolExecutionContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class TurnServices(Protocol):
    """TurnProcessor 所需的外部服务接口。

    由 AgentModule 实现，内部通过 bus request 完成实际调用。
    TurnProcessor 不直接依赖 bus。
    """

    async def get_memory_context(self, session_id: str) -> list[dict[str, Any]]: ...
    async def append_memory(self, session_id: str, messages: list[dict[str, Any]]) -> None: ...
    async def get_skill_by_name(self, skill_name: str) -> SkillCard | None: ...
    async def retrieve_skills(self, query: str, top_k: int = 3) -> list[SkillCard]: ...
    async def get_tool_catalog(self) -> list[ToolSpec]: ...
    async def execute_tool(self, ctx: Any, tool_name: str, tool_args: dict[str, Any]) -> ToolResult: ...


@dataclass
class TurnResult:
    """Outcome of a single conversation turn — returned after streaming completes."""

    turn_id: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    message_batch: list[dict[str, Any]] = field(default_factory=list)
    content: str = ""
    thinking: str = ""
    # Reserved for future use — populated during turn processing but not yet consumed by callers.
    loaded_skill_ids: list[str] = field(default_factory=list)
    # Reserved for future use — populated during turn processing but not yet consumed by callers.
    tool_names: list[str] = field(default_factory=list)
    last_query_matched: bool = False


@dataclass
class _QueuedTurn:
    kind: str
    source: str
    session_id: str
    turn_id: str = ""
    user_message: str = ""
    stream_id: str = ""
    result_future: asyncio.Future | None = None
    on_started: Callable[[str, str], Awaitable[None]] | None = None
    on_completed: Callable[[str, str, TurnResult], Awaitable[None]] | None = None
    on_failed: Callable[[str, str, Exception], Awaitable[None]] | None = None


class TurnProcessor:
    """Single-consumer FIFO processor for both user and cron turns.

    不直接依赖 bus。所有外部交互通过 TurnServices 回调完成，
    由 AgentModule 在构造时注入 bus 实现。
    """

    def __init__(
        self,
        llm: ChatClient | None,
        push_notification,
        services: TurnServices,
        get_system_prompt: Callable[[str], str] | None = None,
        max_iterations: int = 15,
        callbacks: list | None = None,
    ) -> None:
        self._llm: ChatClient | None = llm
        self._push_notification = push_notification
        self._services = services
        self._get_system_prompt = get_system_prompt or (lambda _: "")
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._queue: asyncio.Queue[_QueuedTurn] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._last_result: TurnResult | None = None

    def set_llm(self, llm: ChatClient) -> None:
        """Replace the LLM client (e.g. after deferred init in start_services)."""
        self._llm = llm

    @property
    def last_result(self) -> TurnResult | None:
        return self._last_result

    # ── public API ────────────────────────────────────────────────────────

    async def run_user_turn(self, user_message: str, *, session_id: str) -> None:
        """将用户 turn 入队并等待执行完成。"""
        logger.info("user turn enqueued sid=%s msg=%s", session_id, user_message[:50])
        turn_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future = loop.create_future()
        req = _QueuedTurn(
            kind="user", source="agent",
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            result_future=result_future,
        )
        self._ensure_worker()
        await self._queue.put(req)
        await result_future

    async def run_cron_turn(
        self,
        *,
        session_id: str,
        job_id: str,
        name: str,
        prompt: str,
        stream_id: str,
        wait_until_done: bool = True,
    ) -> str:
        logger.info("cron turn enqueued sid=%s job=%s", session_id, job_id)
        result_future = None
        if wait_until_done:
            loop = asyncio.get_running_loop()
            result_future = loop.create_future()
        req = _QueuedTurn(
            kind="cron",
            source="cron",
            session_id=session_id,
            turn_id=uuid.uuid4().hex[:12],
            user_message=prompt,
            stream_id=stream_id,
            result_future=result_future,
            on_started=self._build_cron_started_hook(job_id=job_id, name=name, prompt=prompt, stream_id=stream_id),
            on_completed=self._build_cron_completed_hook(stream_id=stream_id),
            on_failed=self._build_cron_failed_hook(stream_id=stream_id),
        )
        self._ensure_worker()
        await self._queue.put(req)
        if not wait_until_done:
            return "ENQUEUED"
        return await result_future

    async def shutdown(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None

    # ── queue management ──────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="alex-turn-processor")

    async def _worker(self) -> None:
        while True:
            req = await self._queue.get()
            try:
                await self._process_turn(req)
            except Exception as e:
                # 确保 future 不会永远挂起
                if req.result_future is not None and not req.result_future.done():
                    req.result_future.set_exception(e)
                logger.warning("Worker turn failed unexpectedly", exc_info=True)
            finally:
                self._queue.task_done()

    async def _process_turn(self, req: _QueuedTurn) -> None:
        sid = req.session_id
        turn_id = req.turn_id or uuid.uuid4().hex[:12]
        logger.info("processing turn kind=%s sid=%s turn_id=%s", req.kind, sid, turn_id)
        emit = self._push_notification
        started = TurnStarted(session_id=sid, turn_id=turn_id, source=req.source, kind=req.kind)
        await emit(started)
        if req.on_started is not None:
            await req.on_started(sid, turn_id)

        try:
            result = await self._run_agent_loop(
                sid=sid,
                turn_id=turn_id,
                user_message=req.user_message,
                emit=emit,
                stream_id=req.stream_id,
            )
            if req.kind == "user":
                self._last_result = result
            await emit(TurnCompleted(
                session_id=sid, turn_id=turn_id,
                source=req.source, kind=req.kind,
                messages=result.messages,
                message_batch=result.message_batch,
                content=result.content,
                thinking=result.thinking,
            ))
            if req.on_completed is not None:
                await req.on_completed(sid, turn_id, result)
            if req.result_future is not None and not req.result_future.done():
                req.result_future.set_result(result.content)
        except Exception as e:
            logger.warning("%s turn failed", req.kind.capitalize(), exc_info=True)
            await emit(TurnFailed(
                session_id=sid, turn_id=turn_id, source=req.source, error=str(e),
            ))
            if req.on_failed is not None:
                await req.on_failed(sid, turn_id, e)
            if req.result_future is not None and not req.result_future.done():
                req.result_future.set_exception(e)

    # ── external service calls (delegated to TurnServices) ──────────────

    async def _get_memory_context(self, sid: str) -> list[dict[str, Any]]:
        return await self._services.get_memory_context(sid)

    async def _append_memory(self, sid: str, messages: list[dict[str, Any]]) -> None:
        await self._services.append_memory(sid, messages)

    async def _get_skill_by_name(self, skill_name: str) -> SkillCard | None:
        return await self._services.get_skill_by_name(skill_name)

    async def _retrieve_skills(self, query: str, top_k: int = 3) -> list[SkillCard]:
        return await self._services.retrieve_skills(query, top_k)

    async def _get_tool_catalog(self) -> list[ToolSpec]:
        return await self._services.get_tool_catalog()

    async def _execute_tool(self, ctx: Any, tool_name: str, tool_args: dict[str, Any]) -> ToolResult:
        return await self._services.execute_tool(ctx, tool_name, tool_args)

    async def _run_agent_loop(
        self,
        *,
        sid: str,
        turn_id: str,
        user_message: str,
        emit,
        stream_id: str,
    ) -> TurnResult:
        """Run the ReAct-style agent loop using ChatClient + tool registry.

        Replaces LangGraph's ``create_agent`` internals with a simple while-loop:
        1. Call LLM with current messages + tools
        2. If LLM returns tool calls → execute them → add results → goto 1
        3. If LLM returns final text → done
        """
        prev_msgs = await self._get_memory_context(sid)
        _ensure_reasoning_roundtrip(prev_msgs)
        messages: list[dict[str, Any]] = [*prev_msgs, msg.user_message(user_message)]

        collected_content = ""
        collected_thinking = ""
        loaded_skill_ids: list[str] = []
        tool_names: list[str] = []
        intermediate_msgs: list[dict[str, Any]] = []

        tools = await self._get_tool_catalog()
        tool_schemas = [t.to_openai_schema() for t in tools] if tools else None
        logger.info("agent loop start sid=%s iteration_max=%d tools=%d", sid, self._max_iterations, len(tools) if tools else 0)

        llm = self._llm

        iteration = -1
        for iteration in range(self._max_iterations):
            # Build the system prompt with skills injected for this turn
            system_prompt = await self._get_system_prompt_for_iteration(user_message)

            collected_content = ""
            collected_thinking = ""
            tool_calls: list[dict[str, Any]] = []

            # ── stream LLM response ──────────────────────────────────
            async for event in llm.stream_chat(
                messages,
                tools=tool_schemas,
                system_prompt=system_prompt,
            ):
                if isinstance(event, ContentDelta):
                    collected_content += event.content
                    await emit(TokenEmitted(
                        session_id=sid, turn_id=turn_id,
                        delta=event.content, stream_id=stream_id or "",
                    ))

                elif isinstance(event, ThinkingDelta):
                    collected_thinking += event.content
                    await emit(ThinkingUpdated(
                        session_id=sid, turn_id=turn_id,
                        delta=event.content, stream_id=stream_id or "",
                    ))

                elif isinstance(event, ToolCallRequest):
                    tool_calls = event.tool_calls

                elif isinstance(event, StreamEnd):
                    collected_content = event.content
                    collected_thinking = event.thinking
                    if event.tool_calls:
                        tool_calls = event.tool_calls

            # ── no tool calls → final answer ─────────────────────────
            if not tool_calls:
                assistant_msg = msg.assistant_message(
                    collected_content,
                    reasoning_content=collected_thinking,
                )
                messages.append(assistant_msg)
                intermediate_msgs.append(assistant_msg)
                break

            # ── execute tools ────────────────────────────────────────
            assistant_msg = msg.assistant_message(
                collected_content,
                tool_calls=tool_calls,
                reasoning_content=collected_thinking,
            )
            messages.append(assistant_msg)
            intermediate_msgs.append(assistant_msg)

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args_str = fn.get("arguments", "{}")
                try:
                    tool_args = _json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                except Exception:
                    tool_args = {}

                tool_names.append(tool_name)

                # Emit skill-loaded for load_skill calls
                if tool_name == "load_skill" and not stream_id:
                    skill_name = tool_args.get("skill_name", "") if isinstance(tool_args, dict) else ""
                    skill = await self._get_skill_by_name(skill_name)
                    if skill:
                        loaded_skill_ids.append(skill.id)
                        await emit(SkillLoaded(
                            session_id=sid, turn_id=turn_id,
                            skill_name=skill.name, skill_pattern=skill.pattern,
                        ))

                run_id = tc.get("id", uuid.uuid4().hex[:12])

                ctx = ToolExecutionContext(
                    session_id=sid, turn_id=turn_id,
                    source="cron" if stream_id else "user",
                )
                tool_result = await self._execute_tool(ctx, tool_name, tool_args)
                logger.info("tool executed name=%s ok=%s", tool_name, tool_result.ok)

                # Convert ToolResult to string for backward compatibility
                result_str = tool_result.output if tool_result.ok else tool_result.error

                tool_msg = msg.tool_message(result_str, tool_call_id=run_id)
                messages.append(tool_msg)
                intermediate_msgs.append(tool_msg)

        else:
            # max_iterations reached — 强制让 LLM 给出最终回复（不带 tools）
            logger.warning("max_iterations reached, forcing final answer sid=%s", sid)
            collected_content = ""
            collected_thinking = ""
            try:
                async for event in llm.stream_chat(
                    messages,
                    tools=None,  # 不允许再调工具
                    system_prompt=await self._get_system_prompt_for_iteration(user_message),
                ):
                    if isinstance(event, ContentDelta):
                        collected_content += event.content
                        await emit(TokenEmitted(
                            session_id=sid, turn_id=turn_id,
                            delta=event.content, stream_id=stream_id or "",
                        ))
                    elif isinstance(event, ThinkingDelta):
                        collected_thinking += event.content
                        await emit(ThinkingUpdated(
                            session_id=sid, turn_id=turn_id,
                            delta=event.content, stream_id=stream_id or "",
                        ))
                    elif isinstance(event, StreamEnd):
                        collected_content = event.content
                        collected_thinking = event.thinking
            except Exception:
                logger.warning("forced final answer failed", exc_info=True)
                collected_content = collected_content or "（达到最大工具调用次数，未能完成回复）"

            assistant_msg = msg.assistant_message(
                collected_content,
                reasoning_content=collected_thinking,
            )
            messages.append(assistant_msg)
            intermediate_msgs.append(assistant_msg)

        # ── persist and return ────────────────────────────────────────
        logger.info("agent loop done sid=%s iterations=%d content_len=%d", sid, iteration + 1, len(collected_content))
        batch: list[dict[str, Any]] = [msg.user_message(user_message), *intermediate_msgs]
        await self._append_memory(sid, batch)
        full_history = await self._get_memory_context(sid)
        return TurnResult(
            turn_id=turn_id,
            messages=full_history,
            message_batch=batch,
            content=collected_content,
            thinking=collected_thinking,
            loaded_skill_ids=loaded_skill_ids,
            tool_names=tool_names,
            last_query_matched=len(loaded_skill_ids) > 0,
        )

    async def _get_system_prompt_for_iteration(self, user_message: str) -> str:
        """Return the system prompt (with skills injected) for *user_message*.

        Computed per-turn from PromptAssembler so every LLM call carries the
        correct skill-augmented prompt without shared mutable state.
        """
        return await self._get_system_prompt(user_message)

    # ── cron hooks ────────────────────────────────────────────────────────

    def _build_cron_started_hook(
        self, *, job_id: str, name: str, prompt: str, stream_id: str,
    ) -> Callable[[str, str], Awaitable[None]]:
        async def _hook(session_id: str, turn_id: str) -> None:
            await self._push_notification(ToolStarted(
                session_id=session_id, turn_id=turn_id,
                tool_id=stream_id, tool_name="cron",
                tool_input={"job_id": job_id, "name": name, "prompt": prompt},
                stream_id=stream_id,
            ))
            await self._push_notification(ToolFinished(
                session_id=session_id, turn_id=turn_id,
                tool_id=stream_id, output=prompt,
                stream_id=stream_id,
            ))
        return _hook

    def _build_cron_completed_hook(
        self, *, stream_id: str,
    ) -> Callable[[str, str, TurnResult], Awaitable[None]]:
        async def _hook(session_id: str, _: str, result: TurnResult) -> None:
            pass  # CronBatch/CronDone removed — no subscribers
        return _hook

    def _build_cron_failed_hook(
        self, *, stream_id: str,
    ) -> Callable[[str, str, Exception], Awaitable[None]]:
        async def _hook(session_id: str, _: str, error: Exception) -> None:
            pass  # CronError removed — no subscribers
        return _hook


def _ensure_reasoning_roundtrip(messages: list[dict[str, Any]]) -> None:
    """Ensure every assistant message has a reasoning_content key for round-trip."""
    for m in messages:
        if msg.is_assistant(m) and "reasoning_content" not in m:
            m["reasoning_content"] = ""
