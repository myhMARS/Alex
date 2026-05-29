"""Unified turn processor — single FIFO queue for user and cron turns."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from alex.bus.events import (
    CronBatch,
    CronDone,
    CronError,
    SkillLoaded,
    ThinkingUpdated,
    TokenEmitted,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from alex.memory.base import MemoryBase
from alex.skill import SkillService

logger = logging.getLogger(__name__)

_QUEUE_END = object()


@dataclass
class TurnResult:
    """Outcome of a single conversation turn — returned after streaming completes."""

    turn_id: str = ""
    messages: list[BaseMessage] = field(default_factory=list)
    message_batch: list[BaseMessage] = field(default_factory=list)
    content: str = ""
    thinking: str = ""
    loaded_skill_ids: list[str] = field(default_factory=list)
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
    event_queue: asyncio.Queue | None = None
    result_future: asyncio.Future | None = None
    on_started: Callable[[str, str], Awaitable[None]] | None = None
    on_completed: Callable[[str, str, TurnResult], Awaitable[None]] | None = None
    on_failed: Callable[[str, str, Exception], Awaitable[None]] | None = None


@dataclass
class _QueuedTurnError:
    error: Exception


class TurnProcessor:
    """Single-consumer FIFO processor for both user and cron turns."""

    def __init__(
        self,
        llm: BaseChatModel,
        memory: MemoryBase,
        skill_manager: SkillService,
        push_notification,
        get_bus: Callable[[], Any | None],
        graph_getter: Callable[[], Any],
        max_iterations: int = 5,
        callbacks: list[BaseCallbackHandler] | None = None,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._skills = skill_manager
        self._push_notification = push_notification
        self._get_bus = get_bus
        self._graph_getter = graph_getter
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._session_id = session_id
        self._queue: asyncio.Queue[_QueuedTurn] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._last_result: TurnResult | None = None
        self._user_turn_queues: dict[str, asyncio.Queue] = {}
        self._user_bus_subscribed = False

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def last_result(self) -> TurnResult | None:
        return self._last_result

    async def stream_user_turn(self, user_message: str, *, session_id: str) -> AsyncIterator:
        turn_id = uuid.uuid4().hex[:12]
        queue = await self._subscribe_user_turn(turn_id)
        await self._enqueue_user_turn(
            turn_id=turn_id,
            user_message=user_message,
            session_id=session_id or self._session_id,
            queue=queue,
        )
        try:
            async for event in self._consume_user_turn(turn_id, queue):
                yield event
        finally:
            self._user_turn_queues.pop(turn_id, None)

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
        result_future = None
        if wait_until_done:
            loop = asyncio.get_running_loop()
            result_future = loop.create_future()
        req = _QueuedTurn(
            kind="cron",
            source="cron",
            session_id=session_id or self._session_id,
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
        self._user_turn_queues.clear()
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="alex-turn-processor")

    async def _worker(self) -> None:
        while True:
            req = await self._queue.get()
            try:
                await self._process_turn(req)
            finally:
                self._queue.task_done()

    async def _subscribe_user_turn(self, turn_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._user_turn_queues[turn_id] = queue
        await self._ensure_user_bus_subscriptions()
        return queue

    async def _ensure_user_bus_subscriptions(self) -> None:
        bus = self._get_bus()
        if bus is None or self._user_bus_subscribed:
            return
        for event_type in (
            TurnStarted, ThinkingUpdated, TokenEmitted, SkillLoaded,
            ToolStarted, ToolFinished, TurnCompleted, TurnFailed,
        ):
            await bus.subscribe(event_type, self._route_user_turn_event)
        self._user_bus_subscribed = True

    async def _route_user_turn_event(self, event) -> None:
        queue = self._user_turn_queues.get(getattr(event, "turn_id", ""))
        if queue is None:
            return
        if isinstance(event, TurnCompleted):
            await queue.put(_QUEUE_END)
            return
        if isinstance(event, TurnFailed):
            await queue.put(_QueuedTurnError(RuntimeError(event.error or "Turn failed")))
            await queue.put(_QUEUE_END)
            return
        await queue.put(event)

    async def _enqueue_user_turn(
        self,
        *,
        turn_id: str,
        user_message: str,
        session_id: str,
        queue: asyncio.Queue,
    ) -> None:
        req = _QueuedTurn(
            kind="user",
            source="agent",
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            event_queue=None if self._get_bus() is not None else queue,
        )
        self._ensure_worker()
        await self._queue.put(req)

    async def _consume_user_turn(self, turn_id: str, queue: asyncio.Queue) -> AsyncIterator:
        while True:
            item = await queue.get()
            if item is _QUEUE_END:
                break
            if isinstance(item, _QueuedTurnError):
                raise item.error
            yield item

    async def _process_turn(self, req: _QueuedTurn) -> None:
        sid = req.session_id or self._session_id
        turn_id = req.turn_id or uuid.uuid4().hex[:12]
        emit = self._push_notification
        direct_queue = req.event_queue
        turn_emit = self._compose_turn_emit(direct_queue, emit)
        started = TurnStarted(session_id=sid, turn_id=turn_id, source=req.source, kind=req.kind)
        await turn_emit(started)
        if req.on_started is not None:
            await req.on_started(sid, turn_id)

        try:
            result = await self._run_llm_turn(
                sid=sid,
                turn_id=turn_id,
                user_message=req.user_message,
                emit=turn_emit,
                stream_id=req.stream_id,
            )
            if req.kind == "user":
                self._last_result = result
            await turn_emit(TurnCompleted(
                session_id=sid,
                turn_id=turn_id,
                source=req.source,
                kind=req.kind,
                messages=result.messages,
                message_batch=result.message_batch,
                content=result.content,
                thinking=result.thinking,
            ))
            if req.on_completed is not None:
                await req.on_completed(sid, turn_id, result)
            if direct_queue is not None:
                await direct_queue.put(_QUEUE_END)
            if req.result_future is not None and not req.result_future.done():
                req.result_future.set_result(result.content)
        except Exception as e:
            logger.warning("%s turn failed", req.kind.capitalize(), exc_info=True)
            await emit(TurnFailed(
                session_id=sid, turn_id=turn_id, source=req.source, error=str(e),
            ))
            if req.on_failed is not None:
                await req.on_failed(sid, turn_id, e)
            if direct_queue is not None:
                await direct_queue.put(_QueuedTurnError(e))
                await direct_queue.put(_QUEUE_END)
            if req.result_future is not None and not req.result_future.done():
                req.result_future.set_exception(e)

    async def _run_llm_turn(
        self,
        *,
        sid: str,
        turn_id: str,
        user_message: str,
        emit,
        stream_id: str,
    ) -> TurnResult:
        prev_msgs = await self._memory.get_context(session_id=sid)
        _ensure_reasoning_roundtrip(prev_msgs)
        messages = [*prev_msgs, HumanMessage(content=user_message)]

        collected_content = ""
        collected_thinking = ""
        loaded_skill_ids: list[str] = []
        tool_names: list[str] = []
        intermediate_msgs: list[BaseMessage] = []
        final_ai_msg: AIMessage | None = None
        graph = self._graph_getter()

        async for event in graph.astream_events(
            {"messages": messages},
            config={"callbacks": self._callbacks, "recursion_limit": self._max_iterations * 6 + 10},
            version="v2",
        ):
            kind = event.get("event", "")
            if kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk:
                    reasoning = None
                    if hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content")
                    if reasoning:
                        collected_thinking += reasoning
                        await emit(ThinkingUpdated(
                            session_id=sid,
                            turn_id=turn_id,
                            delta=reasoning,
                            stream_id=stream_id or "",
                        ))
                    if chunk.content:
                        collected_content += chunk.content
                        await emit(TokenEmitted(
                            session_id=sid,
                            turn_id=turn_id,
                            delta=chunk.content,
                            stream_id=stream_id or "",
                        ))

            elif kind == "on_chat_model_end":
                msg = (event.get("data") or {}).get("output")
                if isinstance(msg, AIMessage):
                    if msg.tool_calls:
                        intermediate_msgs.append(msg)
                    else:
                        final_ai_msg = msg

            elif kind == "on_tool_start":
                name = event.get("name", "")
                input_data = event.get("data", {}).get("input")
                run_id = str(event.get("run_id") or "")
                tool_names.append(name)
                if not stream_id and name == "load_skill" and isinstance(input_data, dict):
                    skill_name = input_data.get("skill_name", "")
                    skill = self._skills.get_skill_by_name(skill_name)
                    if skill:
                        loaded_skill_ids.append(skill.id)
                        await emit(SkillLoaded(
                            session_id=sid,
                            turn_id=turn_id,
                            skill_name=skill.name,
                            skill_pattern=skill.pattern,
                        ))
                await emit(ToolStarted(
                    session_id=sid,
                    turn_id=turn_id,
                    tool_id=run_id,
                    tool_name=name,
                    tool_input=input_data,
                    is_cron=bool(stream_id),
                    stream_id=stream_id or "",
                ))

            elif kind == "on_tool_end":
                run_id = str(event.get("run_id") or "")
                output = event.get("data", {}).get("output")
                if isinstance(output, ToolMessage):
                    intermediate_msgs.append(output)
                await emit(ToolFinished(
                    session_id=sid,
                    turn_id=turn_id,
                    tool_id=run_id,
                    output=output.content if isinstance(output, ToolMessage) else output,
                    is_cron=bool(stream_id),
                    stream_id=stream_id or "",
                ))

        batch: list[BaseMessage] = [HumanMessage(content=user_message), *intermediate_msgs]
        if final_ai_msg is not None:
            batch.append(final_ai_msg)
        else:
            batch.append(AIMessage(
                content=collected_content,
                additional_kwargs={"reasoning_content": collected_thinking or ""},
            ))

        await self._memory.add_messages(batch, session_id=sid)
        full_history = await self._memory.get_context(session_id=sid)
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

    def _build_cron_started_hook(
        self, *, job_id: str, name: str, prompt: str, stream_id: str,
    ) -> Callable[[str, str], Awaitable[None]]:
        async def _hook(session_id: str, turn_id: str) -> None:
            await self._push_notification(ToolStarted(
                session_id=session_id,
                turn_id=turn_id,
                tool_id=stream_id,
                tool_name="cron",
                tool_input={"job_id": job_id, "name": name, "prompt": prompt},
                is_cron=True,
                stream_id=stream_id,
            ))
            await self._push_notification(ToolFinished(
                session_id=session_id,
                turn_id=turn_id,
                tool_id=stream_id,
                output=prompt,
                is_cron=True,
                stream_id=stream_id,
            ))
        return _hook

    def _build_cron_completed_hook(
        self, *, stream_id: str,
    ) -> Callable[[str, str, TurnResult], Awaitable[None]]:
        async def _hook(session_id: str, _: str, result: TurnResult) -> None:
            await self._push_notification(CronBatch(
                session_id=session_id, stream_id=stream_id, messages=result.message_batch,
            ))
            await self._push_notification(CronDone(
                session_id=session_id, stream_id=stream_id, content=result.content, thinking=result.thinking,
            ))
        return _hook

    def _build_cron_failed_hook(
        self, *, stream_id: str,
    ) -> Callable[[str, str, Exception], Awaitable[None]]:
        async def _hook(session_id: str, _: str, error: Exception) -> None:
            await self._push_notification(CronError(
                session_id=session_id, stream_id=stream_id, error=f"{type(error).__name__}: {error}",
            ))
        return _hook

    @staticmethod
    def _compose_turn_emit(
        queue: asyncio.Queue | None,
        publish_emit: Callable[[Any], Awaitable[None]],
    ) -> Callable[[Any], Awaitable[None]]:
        if queue is None:
            return publish_emit

        async def _emit(event) -> None:
            await publish_emit(event)
            await queue.put(event)

        return _emit


def _ensure_reasoning_roundtrip(messages: list) -> None:
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        ak = getattr(m, "additional_kwargs", None)
        if ak is None:
            m.additional_kwargs = {"reasoning_content": ""}
            continue
        if not isinstance(ak, dict):
            ak = dict(ak)
            m.additional_kwargs = ak
        if "reasoning_content" not in ak:
            ak["reasoning_content"] = ""
