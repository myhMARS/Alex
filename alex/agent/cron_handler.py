"""Cron turn handler — streams LLM replies for subscribed cron job results."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from alex.bus.events import (
    CronJobEvent,
    ThinkingUpdated,
    TokenEmitted,
    ToolStarted,
    ToolFinished,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    CronBatch,
    CronDone,
    CronError,
)
from alex.memory.base import MemoryBase
from alex.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


class CronTurnHandler:
    """Handles cron job reply streaming — reads cron result, streams LLM
    response, and publishes typed events to the bus."""

    def __init__(
        self,
        llm: BaseChatModel,
        memory: MemoryBase,
        tool_executor: ToolExecutor,
        push_notification,
        turn_lock: asyncio.Lock,
        max_iterations: int = 5,
        callbacks: list[BaseCallbackHandler] | None = None,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._tool_executor = tool_executor
        self._push_notification = push_notification
        self._turn_lock = turn_lock
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._session_id = session_id
        self._active_streams: set[str] = set()

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    @property
    def is_active(self) -> bool:
        return len(self._active_streams) > 0

    async def handle(self, cron_evt: CronJobEvent, graph) -> None:
        stream_id = cron_evt.tool_call_id
        if stream_id in self._active_streams:
            return
        self._active_streams.add(stream_id)

        tool_text = (cron_evt.result if cron_evt.status != "FAILED" else cron_evt.error).strip()
        if len(tool_text) > 1600:
            tool_text = tool_text[:1600] + "..."

        try:
            tool_args = {
                "job_id": cron_evt.job_id,
                "name": cron_evt.name,
                "action": "",
                "run_status": cron_evt.status,
                "runs_done": 0,
            }

            async with self._turn_lock:
                cron_turn_id = uuid.uuid4().hex[:12]
                sid = cron_evt.session_id or self._session_id
                self._push_notification(TurnStarted(
                    session_id=sid, turn_id=cron_turn_id, source="cron", kind="cron",
                ))
                self._push_notification(ToolStarted(
                    session_id=sid, turn_id=cron_turn_id,
                    tool_id=stream_id, tool_name="cron", tool_input=tool_args,
                    is_cron=True, stream_id=stream_id,
                ))
                self._push_notification(ToolFinished(
                    session_id=sid, turn_id=cron_turn_id,
                    tool_id=stream_id, output=tool_text,
                    is_cron=True, stream_id=stream_id,
                ))

                prev_msgs = await self._memory.get_context(session_id=sid)
                self._ensure_reasoning_roundtrip(prev_msgs)
                messages = [
                    *prev_msgs,
                    AIMessage(
                        content="",
                        additional_kwargs={
                            "reasoning_content": "",
                            "alex_turn_start": True,
                            "alex_turn_kind": "cron",
                        },
                        tool_calls=[{"name": "cron", "args": tool_args, "id": stream_id}],
                    ),
                    ToolMessage(content=tool_text, tool_call_id=stream_id),
                ]

                collected_content = ""
                collected_thinking = ""
                last_flush = time.monotonic()
                token_buf = ""
                thinking_buf = ""
                intermediate_msgs: list = []
                _final_ai_msg: AIMessage | None = None

                try:
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
                                    thinking_buf += reasoning
                                if chunk.content:
                                    collected_content += chunk.content
                                    token_buf += chunk.content

                            now = time.monotonic()
                            if now - last_flush > 0.05:
                                if thinking_buf:
                                    self._push_notification(ThinkingUpdated(
                                        session_id=sid, delta=thinking_buf,
                                        stream_id=stream_id,
                                    ))
                                    thinking_buf = ""
                                if token_buf:
                                    self._push_notification(TokenEmitted(
                                        session_id=sid, delta=token_buf,
                                        stream_id=stream_id,
                                    ))
                                    token_buf = ""
                                last_flush = now

                        elif kind == "on_chat_model_end":
                            msg = (event.get("data") or {}).get("output")
                            if isinstance(msg, AIMessage):
                                if msg.tool_calls:
                                    intermediate_msgs.append(msg)
                                else:
                                    _final_ai_msg = msg

                        elif kind == "on_tool_start":
                            rid = str(event.get("run_id") or "")
                            tname = event.get("name", "")
                            input_data = event.get("data", {}).get("input")
                            self._push_notification(ToolStarted(
                                session_id=sid, turn_id=cron_turn_id,
                                tool_id=rid, tool_name=tname, tool_input=input_data,
                                is_cron=True, stream_id=stream_id,
                            ))

                        elif kind == "on_tool_end":
                            rid = str(event.get("run_id") or "")
                            out = event.get("data", {}).get("output")
                            if isinstance(out, ToolMessage):
                                intermediate_msgs.append(out)
                            self._push_notification(ToolFinished(
                                session_id=sid, turn_id=cron_turn_id,
                                tool_id=rid,
                                output=out.content if isinstance(out, ToolMessage) else out,
                                is_cron=True, stream_id=stream_id,
                            ))

                    if thinking_buf:
                        self._push_notification(ThinkingUpdated(
                            session_id=sid, delta=thinking_buf, stream_id=stream_id,
                        ))
                    if token_buf:
                        self._push_notification(TokenEmitted(
                            session_id=sid, delta=token_buf, stream_id=stream_id,
                        ))

                    cron_batch: list[BaseMessage] = []
                    tc_msg = AIMessage(
                        content="",
                        additional_kwargs={
                            "reasoning_content": "",
                            "alex_turn_start": True,
                            "alex_turn_kind": "cron",
                        },
                        tool_calls=[{"name": "cron", "args": tool_args, "id": stream_id}],
                    )
                    cron_batch.append(tc_msg)
                    tool_msg = ToolMessage(content=tool_text, tool_call_id=stream_id)
                    cron_batch.append(tool_msg)
                    for m in intermediate_msgs:
                        cron_batch.append(m)
                    if _final_ai_msg is not None:
                        cron_batch.append(_final_ai_msg)
                    else:
                        ai_kwargs = {"reasoning_content": collected_thinking or ""}
                        fallback = AIMessage(content=collected_content, additional_kwargs=ai_kwargs)
                        cron_batch.append(fallback)

                    await self._memory.add_messages(cron_batch, session_id=sid)
                    full_history = await self._memory.get_context(session_id=sid)

                    self._push_notification(CronBatch(
                        session_id=sid, stream_id=stream_id, messages=cron_batch,
                    ))
                    self._push_notification(CronDone(
                        session_id=sid, stream_id=stream_id,
                        content=collected_content, thinking=collected_thinking,
                    ))
                    self._push_notification(TurnCompleted(
                        session_id=sid, turn_id=cron_turn_id,
                        source="cron", kind="cron", messages=full_history,
                        content=collected_content, thinking=collected_thinking,
                    ))
                except Exception as e:
                    logger.warning("Cron subscribed reply failed", exc_info=True)
                    self._push_notification(CronError(
                        session_id=sid, stream_id=stream_id,
                        error=f"{type(e).__name__}: {e}",
                    ))
                    self._push_notification(TurnFailed(
                        session_id=sid, turn_id=cron_turn_id,
                        source="cron", error=str(e),
                    ))
        finally:
            self._active_streams.discard(stream_id)

    @staticmethod
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
