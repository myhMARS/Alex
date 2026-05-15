"""Agent core orchestration — coordinates LLM, Memory, Tools, Skills, Streaming."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool as LCBaseTool

from alex.config import get_llm_config
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.prompts import get_system_prompt
from alex.skills.base import SkillManager
from alex.streaming.handler import StreamEvent

logger = logging.getLogger(__name__)


class ChatResponse(str):
    """Agent chat response — behaves like a string but carries thinking content.

    Usage:
        response = await agent.chat("hello")
        print(response)              # prints the response text (str behavior)
        print(response.thinking)     # access thinking/reasoning content
    """

    thinking: str

    def __new__(cls, content: str, thinking: str = "") -> ChatResponse:
        instance = super().__new__(cls, content)
        instance.thinking = thinking
        return instance

    def __repr__(self) -> str:
        if self.thinking:
            return f"ChatResponse({super().__repr__()}, thinking={self.thinking[:50]!r}...)"
        return super().__repr__()


DEFAULT_SYSTEM_PROMPT = get_system_prompt()

_REFLECT_INTERVAL = 5  # periodic reflect every N turns


class Agent:
    """Conversational agent with tool-use, memory, skills, and streaming."""

    def __init__(
        self,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_iterations: int = 5,
        tools: list[LCBaseTool] | None = None,
        callbacks: list[BaseCallbackHandler] | None = None,
        memory: MemoryBase | None = None,
        skill_manager: SkillManager | None = None,
        llm: BaseChatModel | None = None,
    ) -> None:
        self._llm = llm or LLMFactory.create(get_llm_config())
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._memory = memory or BufferMemory()
        self._skills = skill_manager or SkillManager()
        self._tools: dict[str, LCBaseTool] = {}
        self._turn_count = 0
        self._current_augmented_prompt = system_prompt
        self._last_used_skill_ids: list[str] = []
        self._last_query_matched: bool = True  # new-domain detection
        if tools:
            for t in tools:
                self._tools[t.name] = t
        self._graph = self._build_graph()

    # ── graph management ──────────────────────────────────────────────────

    def _build_graph(self):
        tools = list(self._tools.values()) if self._tools else None
        return create_agent(
            model=self._llm,
            tools=tools,
            system_prompt=self._current_augmented_prompt,
        )

    def _ensure_skills_prompt(self, query: str) -> None:
        """Rebuild graph if matched skills change the system prompt."""
        skills_text = self._skills.inject_skills_prompt(query)
        augmented = self._system_prompt + skills_text if skills_text else self._system_prompt
        if augmented != self._current_augmented_prompt:
            self._current_augmented_prompt = augmented
            self._graph = self._build_graph()

    # ── public API ───────────────────────────────────────────────────────

    @property
    def tools(self) -> list[LCBaseTool]:
        return list(self._tools.values())

    def register_tool(self, tool: LCBaseTool) -> None:
        self._tools[tool.name] = tool
        self._graph = self._build_graph()

    def unregister_tool(self, name: str) -> None:
        self._tools.pop(name, None)
        self._graph = self._build_graph()

    def get_tool(self, name: str) -> LCBaseTool | None:
        return self._tools.get(name)

    async def clear_history(self) -> None:
        await self._memory.clear()

    @property
    def history(self) -> list:
        return self._memory.get_context_sync()

    def provide_feedback(self, positive: bool) -> None:
        """User feedback — records skill usage and triggers reflection on negative."""
        for skill_id in self._last_used_skill_ids:
            self._skills.record_usage(skill_id, positive)
        if not positive:
            # negative feedback → schedule reflection
            # Works safely whether called from async context or not
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._do_reflect())
            except RuntimeError:
                # No running loop — run synchronously is not ideal,
                # but at least record_usage above already succeeded
                pass

    # ── chat ─────────────────────────────────────────────────────────────

    async def chat(self, user_message: str) -> ChatResponse:
        """Non-streaming chat.

        Returns:
            ChatResponse — behaves like str, with .thinking attribute for reasoning content.
        """
        self._ensure_skills_prompt(user_message)

        # track matched skill ids for feedback
        matched = self._skills.retrieve(user_message, top_k=3)
        self._last_used_skill_ids = [s.id for s in matched]
        self._last_query_matched = len(matched) > 0

        prev_msgs = await self._memory.get_context()
        messages = [*prev_msgs, HumanMessage(content=user_message)]

        recursion_limit = self._max_iterations * 6 + 10 if self._tools else 5
        result = await self._graph.ainvoke(
            {"messages": messages},
            config={
                "callbacks": self._callbacks,
                "recursion_limit": recursion_limit,
            },
        )

        # Append new messages (don't clear — preserve prior context)
        new_msgs = [m for m in result["messages"] if not isinstance(m, SystemMessage)]
        existing_count = self._memory.size
        if len(new_msgs) > existing_count:
            for m in new_msgs[existing_count:]:
                await self._memory.add_message(m)
        else:
            # graph returned truncated results — trust it as new state
            await self._memory.clear()
            await self._memory.add_messages(new_msgs)

        response, thinking = self._extract_response()
        if not response and self._tools:
            result = await self._graph.ainvoke(
                {"messages": result["messages"]},
                config={"callbacks": self._callbacks, "recursion_limit": 8},
            )
            retry_msgs = [m for m in result["messages"] if not isinstance(m, SystemMessage)]
            await self._memory.clear()
            await self._memory.add_messages(retry_msgs)
            response, thinking = self._extract_response()

        await self._maybe_reflect()
        text = response or "Sorry, I didn't understand. Please try again."
        return ChatResponse(text, thinking)

    # ── streaming chat ───────────────────────────────────────────────────

    async def chat_stream(self, user_message: str) -> AsyncIterator[StreamEvent]:
        """Streaming chat — yields StreamEvent for each token / tool call."""
        self._ensure_skills_prompt(user_message)

        matched = self._skills.retrieve(user_message, top_k=3)
        self._last_used_skill_ids = [s.id for s in matched]
        self._last_query_matched = len(matched) > 0

        prev_msgs = await self._memory.get_context()
        messages = [*prev_msgs, HumanMessage(content=user_message)]
        collected_content = ""
        collected_thinking = ""

        async for event in self._graph.astream_events(
            {"messages": messages},
            config={"callbacks": self._callbacks, "recursion_limit": self._max_iterations * 6 + 10},
            version="v2",
        ):
            kind = event.get("event", "")
            if kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk:
                    # Check for reasoning/thinking content (DeepSeek thinking mode)
                    reasoning = None
                    if hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content")
                    if reasoning:
                        collected_thinking += reasoning
                        yield StreamEvent(type="thinking", data=reasoning)
                    if chunk.content:
                        collected_content += chunk.content
                        yield StreamEvent(type="token", data=chunk.content)

            elif kind == "on_tool_start":
                yield StreamEvent(
                    type="tool_start",
                    data={
                        "name": event.get("name", ""),
                        "input": event.get("data", {}).get("input"),
                    },
                )

            elif kind == "on_tool_end":
                yield StreamEvent(
                    type="tool_end",
                    data={"output": event.get("data", {}).get("output")},
                )

        await self._memory.add_message(HumanMessage(content=user_message))
        # Store reasoning_content in additional_kwargs for round-trip
        ai_kwargs = {}
        if collected_thinking:
            ai_kwargs["reasoning_content"] = collected_thinking
        await self._memory.add_message(
            AIMessage(content=collected_content, additional_kwargs=ai_kwargs)
        )

        # Fire-and-forget reflection — don't block the response
        asyncio.ensure_future(self._maybe_reflect())
        yield StreamEvent(type="done", data=collected_content)

    # ── reflection ───────────────────────────────────────────────────────

    async def _maybe_reflect(self) -> None:
        self._turn_count += 1

        # trigger 1: periodic
        should_reflect = self._turn_count % _REFLECT_INTERVAL == 0

        # trigger 2: new domain (no skills matched)
        if not self._last_query_matched:
            should_reflect = True

        if should_reflect:
            await self._do_reflect()

    async def _do_reflect(self) -> None:
        try:
            recent = await self._memory.get_context()
            recent = recent[-20:]
            await self._skills.reflect(recent, self._llm)
        except Exception:
            logger.warning("Skill reflection failed", exc_info=True)

    # ── internal ─────────────────────────────────────────────────────────

    def _extract_response(self) -> tuple[str, str]:
        """Extract the final text response and thinking from memory.

        Returns:
            (response_text, thinking_text)
        """
        for m in reversed(self._memory.get_context_sync()):
            if isinstance(m, AIMessage) and not m.tool_calls and m.content:
                thinking = m.additional_kwargs.get("reasoning_content", "")
                return m.content, thinking
        return "", ""
