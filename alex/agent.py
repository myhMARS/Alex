"""Agent core orchestration — coordinates LLM, Memory, Tools, Skills, Streaming."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool as LCBaseTool, StructuredTool
from pydantic import BaseModel, Field

from alex.config import get_llm_config
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.prompts import get_system_prompt
from alex.cron import CronManager
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


_REFLECT_INTERVAL = 5  # periodic reflect every N turns


class LoadSkillInput(BaseModel):
    skill_name: str = Field(description="Name of the skill to load from the directory")


class Agent:
    """Conversational agent with tool-use, memory, skills, and streaming."""

    def __init__(
        self,
        system_prompt: str | None = None,
        max_iterations: int = 5,
        tools: list[LCBaseTool] | None = None,
        callbacks: list[BaseCallbackHandler] | None = None,
        memory: MemoryBase | None = None,
        skill_manager: SkillManager | None = None,
        llm: BaseChatModel | None = None,
    ) -> None:
        self._llm = llm or LLMFactory.create(get_llm_config())
        self._system_prompt = system_prompt or get_system_prompt()
        self._max_iterations = max_iterations
        self._callbacks = callbacks or []
        self._memory = memory or BufferMemory()
        self._skills = skill_manager or SkillManager()
        self._tools: dict[str, LCBaseTool] = {}
        self._turn_count = 0
        self._current_augmented_prompt = self._system_prompt
        self._last_used_skill_ids: list[str] = []
        self._last_query_matched: bool = True  # new-domain detection
        self._skill_episodes: list[dict] = []  # accumulated multi-turn experience
        self._pending_notifications: list[dict] = []  # system notifications from background tasks
        self._cron = CronManager(self.push_notification)
        self._bg_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_cron_streams: set[str] = set()
        self._reflecting = False
        self._tools["load_skill"] = self._create_load_skill_tool()
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

    def _create_load_skill_tool(self) -> StructuredTool:
        async def _load_skill(skill_name: str) -> str:
            skill = self._skills.get_skill_by_name(skill_name)
            if skill:
                return f"[Skill: {skill.name}]\n\nWhen to apply: {skill.pattern}\n\nExecution methodology:\n{skill.instruction}"
            names = [s.name for s in self._skills.store.list_all() if s.status != "DEPRECATED"]
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

    def bind_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._cron.bind_event_loop(loop)

    async def start_services(self) -> None:
        await self._cron._ensure_scheduler()

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

    def pop_notifications(self) -> list[dict]:
        """Drain and return pending system notifications."""
        notifications = self._pending_notifications[:]
        self._pending_notifications.clear()
        return notifications

    def push_notification(self, note: dict) -> None:
        self._pending_notifications.append(note)
        if note.get("type") == "cron_job_done":
            job = (note.get("job") or {}) if isinstance(note.get("job"), dict) else {}
            if job.get("subscribe"):
                loop = self._loop
                if loop is not None and loop.is_running():
                    try:
                        loop.call_soon_threadsafe(lambda: asyncio.create_task(self._stream_cron_reply(note)))
                        return
                    except Exception:
                        pass
                try:
                    asyncio.get_running_loop().create_task(self._stream_cron_reply(note))
                except RuntimeError:
                    pass

    async def _stream_cron_reply(self, note: dict) -> None:
        job = (note.get("job") or {}) if isinstance(note.get("job"), dict) else {}
        job_id = str(job.get("id", ""))
        name = str(job.get("name", "job"))
        action = str(job.get("action", ""))
        run_status = str(note.get("run_status", job.get("status", "")))
        tool_text = str(note.get("result") or "") if run_status != "FAILED" else str(note.get("error") or "")
        tool_text = tool_text.strip()
        if len(tool_text) > 1600:
            tool_text = tool_text[:1600] + "..."

        tool_call_id = str(note.get("tool_call_id") or f"cron:{job_id}:{int(job.get('runs_done', 0) or 0)}")
        if tool_call_id in self._active_cron_streams:
            return
        self._active_cron_streams.add(tool_call_id)

        try:
            async with self._bg_lock:
                tool_args = {
                    "job_id": job_id,
                    "name": name,
                    "action": action,
                    "run_status": run_status,
                    "runs_done": int(job.get("runs_done", 0) or 0),
                }

            self._pending_notifications.append({
                "type": "cron_stream_start",
                "stream_id": tool_call_id,
                "job": job,
                "tool_call": {"name": "cron", "args": tool_args},
            })
            self._pending_notifications.append({
                "type": "cron_stream_tool_start",
                "stream_id": tool_call_id,
                "data": {"id": tool_call_id, "name": "cron", "input": tool_args},
            })
            self._pending_notifications.append({
                "type": "cron_stream_tool_end",
                "stream_id": tool_call_id,
                "data": {"id": tool_call_id, "output": tool_text},
            })

            prev_msgs = await self._memory.get_context()
            self._ensure_reasoning_roundtrip(prev_msgs)
            messages = [
                *prev_msgs,
                AIMessage(
                    content="",
                    additional_kwargs={"reasoning_content": ""},
                    tool_calls=[{"name": "cron", "args": tool_args, "id": tool_call_id}],
                ),
                ToolMessage(content=tool_text, tool_call_id=tool_call_id),
            ]

            collected_content = ""
            collected_thinking = ""
            last_flush = time.monotonic()
            token_buf = ""
            thinking_buf = ""

            try:
                async for event in self._graph.astream_events(
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
                                self._pending_notifications.append({
                                    "type": "cron_stream_thinking",
                                    "stream_id": tool_call_id,
                                    "data": thinking_buf,
                                })
                                thinking_buf = ""
                            if token_buf:
                                self._pending_notifications.append({
                                    "type": "cron_stream_token",
                                    "stream_id": tool_call_id,
                                    "data": token_buf,
                                })
                                token_buf = ""
                            last_flush = now

                    elif kind == "on_tool_start":
                        rid = str(event.get("run_id") or "")
                        tname = event.get("name", "")
                        input_data = event.get("data", {}).get("input")
                        self._pending_notifications.append({
                            "type": "cron_stream_tool_start",
                            "stream_id": tool_call_id,
                            "data": {"id": rid, "name": tname, "input": input_data},
                        })

                    elif kind == "on_tool_end":
                        rid = str(event.get("run_id") or "")
                        out = event.get("data", {}).get("output")
                        self._pending_notifications.append({
                            "type": "cron_stream_tool_end",
                            "stream_id": tool_call_id,
                            "data": {"id": rid, "output": out},
                        })

                if thinking_buf:
                    self._pending_notifications.append({
                        "type": "cron_stream_thinking",
                        "stream_id": tool_call_id,
                        "data": thinking_buf,
                    })
                if token_buf:
                    self._pending_notifications.append({
                        "type": "cron_stream_token",
                        "stream_id": tool_call_id,
                        "data": token_buf,
                    })

                await self._memory.add_message(AIMessage(content="", tool_calls=[{"name": "cron", "args": tool_args, "id": tool_call_id}]))
                await self._memory.add_message(ToolMessage(content=tool_text, tool_call_id=tool_call_id))
                ai_kwargs = {"reasoning_content": collected_thinking or ""}
                await self._memory.add_message(AIMessage(content=collected_content, additional_kwargs=ai_kwargs))

                self._pending_notifications.append({
                    "type": "cron_stream_done",
                    "stream_id": tool_call_id,
                    "job": job,
                    "response": collected_content,
                    "thinking": collected_thinking,
                })
            except Exception as e:
                logger.warning("Cron subscribed reply failed", exc_info=True)
                self._pending_notifications.append({
                    "type": "cron_stream_error",
                    "stream_id": tool_call_id,
                    "job": job,
                    "error": f"{type(e).__name__}: {e}",
                })
        finally:
            self._active_cron_streams.discard(tool_call_id)

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

    def list_cron_jobs(self) -> list[dict]:
        return self._cron.list_jobs()

    async def cancel_cron_job(self, job_id: str) -> bool:
        return await self._cron.cancel(job_id)

    async def shutdown(self) -> None:
        await self._cron.shutdown()
        self._loop = None

    @property
    def is_reflecting(self) -> bool:
        return self._reflecting

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

        prev_msgs = await self._memory.get_context()
        self._ensure_reasoning_roundtrip(prev_msgs)
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
        self._ensure_reasoning_roundtrip(new_msgs)
        existing_count = self._memory.size
        if len(new_msgs) > existing_count:
            for m in new_msgs[existing_count:]:
                await self._memory.add_message(m)
        else:
            # graph returned truncated results — trust it as new state
            await self._memory.clear()
            await self._memory.add_messages(new_msgs)

        # Track loaded skills from tool calls
        loaded_skill_ids: list[str] = []
        for m in new_msgs:
            if hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    if tc.get("name") == "load_skill":
                        skill_name = tc.get("args", {}).get("skill_name", "")
                        skill = self._skills.get_skill_by_name(skill_name)
                        if skill:
                            loaded_skill_ids.append(skill.id)
        self._last_used_skill_ids = loaded_skill_ids
        self._last_query_matched = len(loaded_skill_ids) > 0

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

        # Record episode for multi-turn skill extraction
        tool_names = list({
            tc.get("name", "")
            for m in new_msgs if hasattr(m, "tool_calls")
            for tc in (m.tool_calls or [])
        })
        loaded_names = [
            s.name for sid in loaded_skill_ids
            if (s := self._skills.store.get(sid))
        ]
        self._record_episode(user_message, loaded_names, tool_names, response)

        await self._maybe_reflect()
        text = response or "Sorry, I didn't understand. Please try again."
        return ChatResponse(text, thinking)

    # ── streaming chat ───────────────────────────────────────────────────

    async def chat_stream(self, user_message: str) -> AsyncIterator[StreamEvent]:
        """Streaming chat — yields StreamEvent for each token / tool call."""
        self._ensure_skills_prompt(user_message)

        prev_msgs = await self._memory.get_context()
        self._ensure_reasoning_roundtrip(prev_msgs)
        messages = [*prev_msgs, HumanMessage(content=user_message)]
        collected_content = ""
        collected_thinking = ""
        loaded_skill_ids: list[str] = []
        tool_names: list[str] = []
        tool_run_ids: list[str] = []

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
                name = event.get("name", "")
                input_data = event.get("data", {}).get("input")
                run_id = str(event.get("run_id") or "")
                tool_names.append(name)
                if run_id:
                    tool_run_ids.append(run_id)
                if name == "load_skill" and isinstance(input_data, dict):
                    skill_name = input_data.get("skill_name", "")
                    skill = self._skills.get_skill_by_name(skill_name)
                    if skill:
                        loaded_skill_ids.append(skill.id)
                        yield StreamEvent(
                            type="skill_load",
                            data={"name": skill.name, "pattern": skill.pattern},
                        )
                yield StreamEvent(
                    type="tool_start",
                    data={"id": run_id, "name": name, "input": input_data},
                )

            elif kind == "on_tool_end":
                run_id = str(event.get("run_id") or "")
                yield StreamEvent(
                    type="tool_end",
                    data={"id": run_id, "output": event.get("data", {}).get("output")},
                )

        self._last_used_skill_ids = loaded_skill_ids
        self._last_query_matched = len(loaded_skill_ids) > 0

        await self._memory.add_message(HumanMessage(content=user_message))
        # Store reasoning_content in additional_kwargs for round-trip
        ai_kwargs = {"reasoning_content": collected_thinking or ""}
        await self._memory.add_message(
            AIMessage(content=collected_content, additional_kwargs=ai_kwargs)
        )

        # Record episode for multi-turn skill extraction
        loaded_names = [
            s.name for sid in loaded_skill_ids
            if (s := self._skills.store.get(sid))
        ]
        self._record_episode(user_message, loaded_names, tool_names, collected_content)

        # Reflection is now triggered by TUI after receiving done event
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
            self._reflecting = True
            await self._do_reflect()
            self._reflecting = False
    async def _do_reflect(self) -> None:
        self._reflecting = True
        try:
            recent = await self._memory.get_context()
            recent = recent[-20:]
            summary = await self._skills.reflect(recent, self._llm, episodes=self._skill_episodes)
            self._skill_episodes.clear()
            self._pending_notifications.append({
                "type": "skill_reflect",
                "new": summary.get("new", 0),
                "updated": summary.get("updated", 0),
                "deprecated": summary.get("deprecated", 0),
                "names": summary.get("new_skill_names", []),
            })
        except Exception as e:
            logger.warning("Skill reflection failed", exc_info=True)
            self._pending_notifications.append({
                "type": "skill_reflect_error",
                "error": str(e),
            })
        finally:
            self._reflecting = False

    def _record_episode(self, user_message: str, loaded_skills: list[str], tool_names: list[str], response: str) -> None:
        """Record a problem-solving episode for multi-turn skill extraction."""
        self._skill_episodes.append({
            "query": user_message[:200],
            "skills_loaded": loaded_skills,
            "tools_used": tool_names,
            "outcome": response[:300],
        })

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
