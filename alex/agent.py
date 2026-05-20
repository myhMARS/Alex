"""Agent core orchestration — coordinates LLM, Memory, Tools, Skills, Streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool as LCBaseTool, StructuredTool
from pydantic import BaseModel, Field

from alex.config import get_llm_config
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.prompts import get_system_prompt
from alex.cron import CronManager
from alex.events import CronDebugEvent, CronJobEvent, SkillReflectErrorEvent, SkillReflectEvent
from alex.skills.base import SkillManager
from alex.streaming.handler import StreamEvent

logger = logging.getLogger(__name__)


_REFLECT_INTERVAL = 5  # periodic reflect every N turns


class LoadSkillInput(BaseModel):
    skill_name: str = Field(description="Name of the skill to load from the directory")


class CronHistoryInput(BaseModel):
    query: str = Field(default="", description="Optional job id, execution id, status, or partial task name")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of history entries to return")


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
        self._session_id: str = ""
        self._cron_history: list[dict] = []
        self._last_used_skill_ids: list[str] = []
        self._last_query_matched: bool = True  # new-domain detection
        self._skill_episodes: list[dict] = []  # accumulated multi-turn experience
        self._pending_notifications: list = []  # typed events from background tasks
        self._cron = CronManager(self.push_notification)
        self._turn_lock = asyncio.Lock()  # serialise chat_stream / cron reply turns
        self._active_cron_streams: set[str] = set()
        self._reflecting = False
        self._tools["load_skill"] = self._create_load_skill_tool()
        self._tools["cron_history"] = self._create_cron_history_tool()
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
        self._cron.bind_event_loop(loop)

    def set_session_context(self, session_id: str, cron_history: list[dict] | None = None) -> None:
        """Set the current TUI session context for cron history queries."""
        self._session_id = session_id
        self._cron_history = list(cron_history or [])

    @property
    def session_id(self) -> str:
        return self._session_id

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

    def pop_notifications(self) -> list:
        """Drain and return pending system events (typed dataclass instances)."""
        notifications = self._pending_notifications[:]
        self._pending_notifications.clear()
        return notifications

    def push_notification(self, event) -> None:
        """Enqueue a typed event for the frontend to consume.

        CronJobEvents with subscribe=True automatically trigger an LLM
        streaming reply via _stream_cron_reply().
        """
        self._pending_notifications.append(event)
        if isinstance(event, CronJobEvent) and event.subscribe:
            try:
                asyncio.create_task(self._stream_cron_reply(event))
            except RuntimeError:
                pass

    async def _run_cron_action(self, action: str, params: dict) -> str:
        """Execute a persisted cron action after schedule or after restart."""
        action = (action or "").strip()
        params = params or {}

        if action == "notify":
            return str(params.get("message", ""))

        tool = self._tools.get(action)
        if tool is not None:
            return str(await tool.ainvoke(params))

        raise ValueError(f"Unknown action: {action}")

    async def _stream_cron_reply(self, cron_evt: CronJobEvent) -> None:
        tool_call_id = cron_evt.tool_call_id
        if tool_call_id in self._active_cron_streams:
            return
        self._active_cron_streams.add(tool_call_id)

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

            # Notify TUI that a cron stream is starting — inside the turn lock
            # so the bubble only appears after any in-progress user chat ends.
            async with self._turn_lock:
                self._pending_notifications.append(StreamEvent(
                    type="tool_start",
                    data={"id": tool_call_id, "name": "cron", "input": tool_args},
                    metadata={"stream_id": tool_call_id, "is_cron": True},
                ))
                self._pending_notifications.append(StreamEvent(
                    type="tool_end",
                    data={"id": tool_call_id, "output": tool_text},
                    metadata={"stream_id": tool_call_id, "is_cron": True},
                ))

                prev_msgs = await self._memory.get_context()
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
                        tool_calls=[{"name": "cron", "args": tool_args, "id": tool_call_id}],
                    ),
                    ToolMessage(content=tool_text, tool_call_id=tool_call_id),
                ]

                collected_content = ""
                collected_thinking = ""
                last_flush = time.monotonic()
                token_buf = ""
                thinking_buf = ""
                cron_intermediate_msgs: list = []
                _cron_final_ai_msg: AIMessage | None = None

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
                                    self._pending_notifications.append(StreamEvent(
                                        type="thinking", data=thinking_buf,
                                        metadata={"stream_id": tool_call_id},
                                    ))
                                    thinking_buf = ""
                                if token_buf:
                                    self._pending_notifications.append(StreamEvent(
                                        type="token", data=token_buf,
                                        metadata={"stream_id": tool_call_id},
                                    ))
                                    token_buf = ""
                                last_flush = now

                        elif kind == "on_chat_model_end":
                            msg = (event.get("data") or {}).get("output")
                            if isinstance(msg, AIMessage):
                                if msg.tool_calls:
                                    cron_intermediate_msgs.append(msg)
                                else:
                                    _cron_final_ai_msg = msg

                        elif kind == "on_tool_start":
                            rid = str(event.get("run_id") or "")
                            tname = event.get("name", "")
                            input_data = event.get("data", {}).get("input")
                            self._pending_notifications.append(StreamEvent(
                                type="tool_start",
                                data={"id": rid, "name": tname, "input": input_data},
                                metadata={"stream_id": tool_call_id},
                            ))

                        elif kind == "on_tool_end":
                            rid = str(event.get("run_id") or "")
                            out = event.get("data", {}).get("output")
                            if isinstance(out, ToolMessage):
                                cron_intermediate_msgs.append(out)
                            self._pending_notifications.append(StreamEvent(
                                type="tool_end",
                                data={"id": rid, "output": out},
                                metadata={"stream_id": tool_call_id},
                            ))

                    if thinking_buf:
                        self._pending_notifications.append(StreamEvent(
                            type="thinking", data=thinking_buf,
                            metadata={"stream_id": tool_call_id},
                        ))
                    if token_buf:
                        self._pending_notifications.append(StreamEvent(
                            type="token", data=token_buf,
                            metadata={"stream_id": tool_call_id},
                        ))

                    # Build exact message batch and write atomically.
                    cron_batch: list[BaseMessage] = []
                    tc_msg = AIMessage(
                        content="",
                        additional_kwargs={
                            "reasoning_content": "",
                            "alex_turn_start": True,
                            "alex_turn_kind": "cron",
                        },
                        tool_calls=[{"name": "cron", "args": tool_args, "id": tool_call_id}],
                    )
                    cron_batch.append(tc_msg)

                    tool_msg = ToolMessage(content=tool_text, tool_call_id=tool_call_id)
                    cron_batch.append(tool_msg)

                    for m in cron_intermediate_msgs:
                        cron_batch.append(m)

                    if _cron_final_ai_msg is not None:
                        cron_batch.append(_cron_final_ai_msg)
                    else:
                        ai_kwargs = {"reasoning_content": collected_thinking or ""}
                        fallback = AIMessage(content=collected_content, additional_kwargs=ai_kwargs)
                        cron_batch.append(fallback)

                    await self._memory.add_messages(cron_batch)

                    self._pending_notifications.append(StreamEvent(
                        type="message_batch", data=cron_batch,
                        metadata={"stream_id": tool_call_id},
                    ))
                    self._pending_notifications.append(StreamEvent(
                        type="done",
                        data=collected_content,
                        metadata={
                            "stream_id": tool_call_id,
                            "is_cron_done": True,
                            "thinking": collected_thinking,
                        },
                    ))
                except Exception as e:
                    logger.warning("Cron subscribed reply failed", exc_info=True)
                    self._pending_notifications.append(StreamEvent(
                        type="error",
                        data=f"{type(e).__name__}: {e}",
                        metadata={"stream_id": tool_call_id, "is_cron_error": True},
                    ))
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

    @property
    def is_reflecting(self) -> bool:
        return self._reflecting

    def provide_feedback(self, positive: bool) -> None:
        """User feedback — records skill usage and triggers reflection on negative."""
        for skill_id in self._last_used_skill_ids:
            self._skills.record_usage(skill_id, positive)
        if not positive:
            try:
                asyncio.get_running_loop().create_task(self._do_reflect())
            except RuntimeError:
                pass

    # ── reflection / skills (public) ──────────────────────────────────────

    async def reflect(self) -> dict:
        """Force skill reflection. Returns {new, updated, deprecated, names}."""
        await self._do_reflect()

    def list_skills(self) -> list[dict]:
        """List all skills with metadata for display."""
        all_skills = self._skills.store.list_all()
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
        for s in self._skills.store.list_all():
            if s.id.startswith(target) or s.name.lower() == target.lower():
                found = s
                break
        if found:
            self._skills.store.remove(found.id)
            return found.name
        return None

    def deprecate_skill(self, target: str) -> str | None:
        """Deprecate a skill by name or id prefix. Returns skill name or None."""
        found = None
        for s in self._skills.store.list_all():
            if s.id.startswith(target) or s.name.lower() == target.lower():
                found = s
                break
        if found:
            self._skills.store.deprecate(found.id)
            return found.name
        return None

    async def merge_skills(self) -> dict:
        """LLM-based skill deduplication. Returns {merged, deprecated, remaining}."""
        return await self._skills.merge_skills(self._llm)

    # ── history restore (public) ──────────────────────────────────────────

    async def restore_history(self, messages: list) -> None:
        """Clear memory and replay a standard message sequence.

        Accepts either LangChain BaseMessage objects or dicts with keys
        matching the core session serialization format (type, content,
        tool_calls, tool_call_id, additional_kwargs).  This is the exact
        inverse of the session persistence layer — no UI view-model involved.
        """
        from alex.session import deserialize_message

        await self._memory.clear()
        for item in messages:
            if isinstance(item, BaseMessage):
                msg = item
            elif isinstance(item, dict):
                msg = deserialize_message(item)
            else:
                continue
            await self._memory.add_message(msg)

    # ── streaming chat ───────────────────────────────────────────────────

    async def chat_stream(self, user_message: str) -> AsyncIterator[StreamEvent]:
        """Streaming chat — yields StreamEvent for each token / tool call."""
        self._ensure_skills_prompt(user_message)

        async with self._turn_lock:
            prev_msgs = await self._memory.get_context()
            self._ensure_reasoning_roundtrip(prev_msgs)
            messages = [*prev_msgs, HumanMessage(content=user_message)]
            collected_content = ""
            collected_thinking = ""
            loaded_skill_ids: list[str] = []
            tool_names: list[str] = []
            tool_run_ids: list[str] = []
            intermediate_msgs: list = []
            _final_ai_msg: AIMessage | None = None

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
                            yield StreamEvent(type="thinking", data=reasoning)
                        if chunk.content:
                            collected_content += chunk.content
                            yield StreamEvent(type="token", data=chunk.content)

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
                    output = event.get("data", {}).get("output")
                    if isinstance(output, ToolMessage):
                        intermediate_msgs.append(output)
                    yield StreamEvent(
                        type="tool_end",
                        data={"id": run_id, "output": output},
                    )

            self._last_used_skill_ids = loaded_skill_ids
            self._last_query_matched = len(loaded_skill_ids) > 0

            # Build the exact message batch and write atomically within the lock.
            user_msg = HumanMessage(content=user_message)
            batch: list[BaseMessage] = [user_msg]
            for m in intermediate_msgs:
                batch.append(m)
            if _final_ai_msg is not None:
                batch.append(_final_ai_msg)
            else:
                ai_kwargs = {"reasoning_content": collected_thinking or ""}
                fallback = AIMessage(content=collected_content, additional_kwargs=ai_kwargs)
                batch.append(fallback)
            await self._memory.add_messages(batch)

            yield StreamEvent(type="message_batch", data=batch)

            # Record episode for multi-turn skill extraction
            loaded_names = [
                s.name for sid in loaded_skill_ids
                if (s := self._skills.store.get(sid))
            ]
            self._record_episode(user_message, loaded_names, tool_names, collected_content)

        await self._maybe_reflect()
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
            self._pending_notifications.append(SkillReflectEvent(
                new=summary.get("new", 0),
                updated=summary.get("updated", 0),
                deprecated=summary.get("deprecated", 0),
                names=summary.get("new_skill_names", []),
                updated_names=summary.get("updated_skill_names", []),
            ))
        except Exception as e:
            logger.warning("Skill reflection failed", exc_info=True)
            self._pending_notifications.append(SkillReflectErrorEvent(
                error=str(e),
            ))
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
