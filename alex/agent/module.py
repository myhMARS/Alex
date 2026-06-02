"""AgentModule — agent 模块的 bus 入口，实现 TurnServices 提供 bus 交互。

所有与 bus 的交互集中在此模块中，TurnProcessor 通过 TurnServices
协议接收注入的回调，不直接依赖 bus。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from alex.kernel.contracts.chat import UserTurnRequested
from alex.kernel.contracts.cron import CronTurnRequested
from alex.kernel.contracts.memory import AppendMessages, GetContext
from alex.kernel.contracts.skills import LoadSkill, RetrieveSkills
from alex.kernel.contracts.tools import ExecuteTool, GetToolCatalog
from alex.kernel.dto.skill import SkillCard
from alex.kernel.dto.tool import ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class AgentModule:
    """Agent 模块 — 订阅 UserTurnRequested，通过 bus 与其他模块交互。

    实现 TurnServices 协议，将 bus request 调用注入 TurnProcessor。
    """

    name = "agent"
    dependencies: list[str] = ["memory", "tools", "skill"]

    def __init__(self, agent: Any = None) -> None:
        self._agent: Any = agent
        self._bus: Any = None

    async def start(self, bus: Any) -> None:
        self._bus = bus
        if self._agent is None:
            from alex.agent.service import Agent
            from alex.prompts import get_system_prompt
            self._agent = Agent(bus, system_prompt=get_system_prompt())
        else:
            self._agent.bind_event_bus(bus)
        await self._agent.start_services()
        await bus.subscribe(UserTurnRequested, self._on_user_turn)
        await bus.subscribe(CronTurnRequested, self._on_cron_turn)
        logger.info("AgentModule started (subscribes UserTurnRequested/CronTurnRequested)")

    async def stop(self) -> None:
        await self._agent.shutdown()
        self._bus = None

    # ── TurnServices 实现（bus request 调用）──────────────────────────

    async def get_memory_context(self, session_id: str) -> list[dict[str, Any]]:
        return await self._bus.request(GetContext(session_id=session_id))

    async def append_memory(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        await self._bus.request(AppendMessages(session_id=session_id, messages=messages))

    async def get_skill_by_name(self, skill_name: str) -> SkillCard | None:
        try:
            return await self._bus.request(LoadSkill(skill_name=skill_name))
        except Exception:
            logger.debug("LoadSkill failed for '%s'", skill_name, exc_info=True)
            return None

    async def retrieve_skills(self, query: str, top_k: int = 3) -> list[SkillCard]:
        return await self._bus.request(RetrieveSkills(query=query, top_k=top_k))

    async def get_tool_catalog(self) -> list[ToolSpec]:
        return await self._bus.request(GetToolCatalog())

    async def execute_tool(self, ctx: Any, tool_name: str, tool_args: dict[str, Any]) -> ToolResult:
        return await self._bus.request(ExecuteTool(
            session_id=getattr(ctx, "session_id", ""),
            turn_id=getattr(ctx, "turn_id", "") or "",
            name=tool_name,
            args=tool_args,
            ctx=ctx,
        ))

    # ── 事件处理 ─────────────────────────────────────────────────────

    async def _on_user_turn(self, cmd: UserTurnRequested) -> None:
        """收到用户消息 → 在独立 task 中处理（避免阻塞 bus dispatch loop）。"""
        logger.info("received UserTurnRequested sid=%s text=%s", cmd.session_id, cmd.user_text[:50])
        asyncio.create_task(self._process_user_turn(cmd))

    async def _process_user_turn(self, cmd: UserTurnRequested) -> None:
        """实际处理用户 turn。"""
        self._agent.set_session_context(cmd.session_id)
        try:
            await self._agent.chat_stream(cmd.user_text)
        except Exception as e:
            logger.warning("User turn failed: %s: %s", type(e).__name__, e, exc_info=True)
            return

        # turn 完成后，如果没匹配到 skill 则自动触发反思
        # 反思引擎可能基于本轮对话提取新 skill
        result = self._agent.last_turn_result
        if result and not result.last_query_matched:
            from alex.kernel.contracts.skills import ReflectSkills
            logger.info("auto reflect: no skill matched in turn sid=%s", cmd.session_id)
            self._bus.publish(ReflectSkills(session_id=cmd.session_id))

    async def _on_cron_turn(self, cmd: CronTurnRequested) -> None:
        """收到 cron 触发 → 在独立 task 中执行 cron turn（通过 TurnProcessor FIFO 串行化）。"""
        trigger = cmd.trigger or {}
        logger.info("received CronTurnRequested sid=%s job=%s", cmd.session_id, trigger.get("job_id", ""))
        asyncio.create_task(self._process_cron_turn(cmd))

    async def _process_cron_turn(self, cmd: CronTurnRequested) -> None:
        """实际处理 cron turn — 委托给 ChatAppService 的 cron 执行路径。"""
        trigger = cmd.trigger or {}
        session_id = cmd.session_id or trigger.get("session_id", "")
        job_id = trigger.get("job_id", "")
        name = trigger.get("name", "")
        prompt = trigger.get("prompt", "")
        stream_id = trigger.get("stream_id", "")

        self._agent.set_session_context(session_id)
        try:
            await self._agent.execute_cron_prompt(
                session_id=session_id,
                job_id=job_id,
                name=name,
                prompt=prompt,
                stream_id=stream_id,
                wait_until_done=True,
            )
        except Exception:
            logger.warning("Cron turn failed (job=%s)", job_id, exc_info=True)

    @property
    def agent(self) -> Any:
        return self._agent
