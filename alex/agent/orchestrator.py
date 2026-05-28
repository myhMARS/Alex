"""Turn orchestrator — runs a single user conversation turn with streaming."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from alex.bus.events import (
    SkillLoaded,
    ThinkingUpdated,
    TokenEmitted,
    ToolStarted,
    ToolFinished,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from alex.memory.base import MemoryBase
from alex.skill import SkillService

logger = logging.getLogger(__name__)


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


class TurnOrchestrator:
    """Runs a single user turn — gets context, streams typed UI events,
    persists the resulting message batch to memory, and publishes lifecycle
    events to the bus."""

    def __init__(
        self,
        llm: BaseChatModel,
        memory: MemoryBase,
        skill_manager: SkillService,
        push_notification,
        turn_lock: asyncio.Lock,
        max_iterations: int = 5,
        callbacks: list[BaseCallbackHandler] | None = None,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self._memory = memory
        self._skills = skill_manager
        self._push_notification = push_notification
        self._turn_lock = turn_lock
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._session_id = session_id

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    async def run(self, user_message: str, graph) -> AsyncIterator:
        """Stream a user turn — yields typed events for each token / tool call.

        Publishes TurnStarted on entry and TurnCompleted on completion.
        The caller reads last_result after the generator exhausts for the
        turn outcome.
        """
        turn_id = uuid.uuid4().hex[:12]
        sid = self._session_id
        self._push_notification(TurnStarted(
            session_id=sid, turn_id=turn_id, source="agent", kind="user",
        ))

        try:
            async with self._turn_lock:
                prev_msgs = await self._memory.get_context(session_id=sid)
                _ensure_reasoning_roundtrip(prev_msgs)
                messages = [*prev_msgs, HumanMessage(content=user_message)]

                collected_content = ""
                collected_thinking = ""
                loaded_skill_ids: list[str] = []
                tool_names: list[str] = []
                intermediate_msgs: list[BaseMessage] = []
                _final_ai_msg: AIMessage | None = None

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
                                yield ThinkingUpdated(session_id=sid, turn_id=turn_id, delta=reasoning)
                            if chunk.content:
                                collected_content += chunk.content
                                yield TokenEmitted(session_id=sid, turn_id=turn_id, delta=chunk.content)

                    elif kind == "on_chat_model_end":
                        msg = (event.get("data") or {}).get("output")
                        if isinstance(msg, AIMessage):
                            if msg.tool_calls:
                                intermediate_msgs.append(msg)
                            else:
                                _final_ai_msg = msg

                    elif kind == "on_tool_start":
                        name = event.get("name", "")
                        input_data = event.get("data", {}).get("input")
                        run_id = str(event.get("run_id") or "")
                        tool_names.append(name)
                        if name == "load_skill" and isinstance(input_data, dict):
                            skill_name = input_data.get("skill_name", "")
                            skill = self._skills.get_skill_by_name(skill_name)
                            if skill:
                                loaded_skill_ids.append(skill.id)
                                yield SkillLoaded(
                                    session_id=sid, turn_id=turn_id,
                                    skill_name=skill.name, skill_pattern=skill.pattern,
                                )
                        yield ToolStarted(
                            session_id=sid, turn_id=turn_id,
                            tool_id=run_id, tool_name=name, tool_input=input_data,
                        )

                    elif kind == "on_tool_end":
                        run_id = str(event.get("run_id") or "")
                        output = event.get("data", {}).get("output")
                        if isinstance(output, ToolMessage):
                            intermediate_msgs.append(output)
                        yield ToolFinished(
                            session_id=sid, turn_id=turn_id,
                            tool_id=run_id,
                            output=output.content if isinstance(output, ToolMessage) else output,
                        )

                # Build exact message batch and write atomically.
                user_msg = HumanMessage(content=user_message)
                batch: list[BaseMessage] = [user_msg]
                for m in intermediate_msgs:
                    batch.append(m)
                if _final_ai_msg is not None:
                    batch.append(_final_ai_msg)
                else:
                    ai_kwargs = {"reasoning_content": collected_thinking or ""}
                    batch.append(AIMessage(content=collected_content, additional_kwargs=ai_kwargs))

                await self._memory.add_messages(batch, session_id=sid)
                full_history = await self._memory.get_context(session_id=sid)

            self._last_result = TurnResult(
                turn_id=turn_id,
                messages=full_history,
                message_batch=batch,
                content=collected_content,
                thinking=collected_thinking,
                loaded_skill_ids=loaded_skill_ids,
                tool_names=tool_names,
                last_query_matched=len(loaded_skill_ids) > 0,
            )

            self._push_notification(TurnCompleted(
                session_id=sid, turn_id=turn_id, source="agent",
                kind="user", messages=full_history, content=collected_content,
                thinking=collected_thinking,
            ))
        except Exception:
            logger.warning("User turn failed", exc_info=True)
            self._push_notification(TurnFailed(
                session_id=sid, turn_id=turn_id, source="agent", kind="user",
            ))
            raise

    @property
    def last_result(self) -> TurnResult | None:
        return getattr(self, "_last_result", None)


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
