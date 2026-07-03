"""SkillModule — exposes skill operations via the message bus.

Phase 2: thin wrapper around existing SkillService.
Phase 4 will convert agent to use bus requests instead of direct calls.
"""

from __future__ import annotations

import logging
from typing import Any

from alex.skill.factory import create_default_skill_service
from alex.kernel.contracts.chat import FeedbackSubmitted
from alex.kernel.contracts.skills import (
    DeleteSkill,
    DeprecateSkill,
    GetSkillName,
    ListSkills,
    LoadSkill,
    MergeSkills,
    RecordSkillUsage,
    ReflectSkills,
    RetrieveSkills,
    SkillsReflected,
)
from alex.kernel.dto.skill import SkillCard

logger = logging.getLogger(__name__)


class SkillModule:
    """Pluggable skill module — provides retrieval, loading, and reflection."""

    name = "skill"
    dependencies: list[str] = ["tools"]

    def __init__(self, skill_service: Any = None) -> None:
        self._service = skill_service or create_default_skill_service()
        self._bus: Any = None

    async def start(self, bus: Any) -> None:
        self._bus = bus
        bus.provide(RetrieveSkills, self._handle_retrieve)
        bus.provide(LoadSkill, self._handle_load)
        bus.provide(ListSkills, self._handle_list)
        bus.provide(GetSkillName, self._handle_get_name)
        bus.provide(DeleteSkill, self._handle_delete)
        bus.provide(DeprecateSkill, self._handle_deprecate)
        bus.provide(RecordSkillUsage, self._handle_record_usage)
        bus.provide(MergeSkills, self._handle_merge)
        await bus.subscribe(ReflectSkills, self._handle_reflect)
        await bus.subscribe(FeedbackSubmitted, self._on_feedback)

        # 将 load_skill 和 list_skills 注册为工具 → ToolsModule 收编到工具目录
        try:
            from alex.kernel.contracts.tools import RegisterTool
            await bus.request(RegisterTool(
                name="load_skill",
                description=(
                    "Load the full execution methodology for a skill from the skill directory. "
                    "Use this when a skill's pattern matches the user's request and you need "
                    "the step-by-step execution guide to properly handle this type of task."
                ),
                json_schema={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "Name of the skill to load"},
                    },
                    "required": ["skill_name"],
                },
                callable_ref=self._load_skill_tool,
            ))
            await bus.request(RegisterTool(
                name="list_skills",
                description=(
                    "List all available skills with their name, pattern, and status. "
                    "Use this when the user asks about available skills or how many skills exist."
                ),
                json_schema={
                    "type": "object",
                    "properties": {},
                },
                callable_ref=self._list_skills_tool,
            ))
        except Exception as e:
            # ToolsModule 未启动时静默跳过（测试环境）
            logger.debug("Skill tools registration skipped: %s", e)

        logger.info("SkillModule started (provides RetrieveSkills/LoadSkill, registers load_skill tool)")

    async def stop(self) -> None:
        self._bus = None

    # ── load_skill / list_skills 工具实现 ─────────────────────────────

    async def _load_skill_tool(self, skill_name: str = "") -> str:
        """作为工具被 LLM 调用 — 返回 skill 的完整执行方法论。"""
        skill = self._service.get_skill_by_name(skill_name)
        if skill is None:
            return f"Skill '{skill_name}' not found."
        return (
            f"[Skill: {skill.name}]\n\n"
            f"When to apply: {skill.pattern}\n\n"
            f"Execution methodology:\n{skill.instruction}"
        )

    async def _list_skills_tool(self) -> str:
        """作为工具被 LLM 调用 — 返回所有 skill 的列表。"""
        skills = self._service.list_all()
        active = [s for s in skills if s.status != "DEPRECATED"]
        if not active:
            return "No skills available."
        lines = [f"Total: {len(active)} skills\n"]
        for s in active:
            lines.append(f"- {s.name} [{s.status}] pattern: {s.pattern}")
        return "\n".join(lines)

    # ── request handlers ─────────────────────────────────────────────────

    async def _handle_retrieve(self, req: RetrieveSkills) -> list[SkillCard]:
        skills = self._service.retrieve(req.query, top_k=req.top_k)
        return [
            SkillCard(
                id=s.id,
                name=s.name,
                pattern=s.pattern,
                summary=getattr(s, "summary", ""),
                instruction=s.instruction or "",
                tags=list(s.tags or []),
                version=getattr(s, "version", 1),
                status=getattr(s, "status", "ACTIVE"),
            )
            for s in skills
        ]

    async def _handle_load(self, req: LoadSkill) -> SkillCard:
        skill = self._service.get_skill_by_name(req.skill_name)
        if skill is None:
            raise ValueError(f"Skill not found: {req.skill_name}")

        return SkillCard(
            id=skill.id,
            name=skill.name,
            pattern=skill.pattern,
            summary=getattr(skill, "summary", ""),
            instruction=skill.instruction or "",
            tags=list(skill.tags or []),
            version=getattr(skill, "version", 1),
            status=getattr(skill, "status", "ACTIVE"),
        )

    async def _handle_list(self, req: ListSkills) -> list[dict]:
        skills = self._service.list_all()
        if not req.include_deprecated:
            skills = [s for s in skills if s.status != "DEPRECATED"]
        return [{"id": s.id, "name": s.name, "status": s.status,
                 "use_count": s.use_count, "success_count": s.success_count,
                 "failure_count": s.failure_count, "pattern": s.pattern,
                 "instruction": s.instruction, "tags": s.tags} for s in skills]

    async def _handle_get_name(self, req: GetSkillName) -> str:
        skill = self._service.get_skill(req.skill_id)
        return skill.name if skill else req.skill_id

    async def _handle_delete(self, req: DeleteSkill) -> str | None:
        for s in self._service.list_all():
            if s.id.startswith(req.target) or s.name.lower() == req.target.lower():
                self._service.remove_skill(s.id)
                return s.name
        return None

    async def _handle_deprecate(self, req: DeprecateSkill) -> str | None:
        for s in self._service.list_all():
            if s.id.startswith(req.target) or s.name.lower() == req.target.lower():
                self._service.deprecate_skill(s.id)
                return s.name
        return None

    async def _handle_record_usage(self, req: RecordSkillUsage) -> None:
        self._service.record_usage(req.skill_id, req.positive)

    async def _handle_merge(self, _req: MergeSkills) -> dict:
        return await self._service.merge_skills(config=None)

    # ── command handlers ─────────────────────────────────────────────────

    async def _handle_reflect(self, cmd: ReflectSkills) -> None:
        logger.info("reflect started sid=%s", cmd.session_id)
        # 从 memory 获取最近对话历史用于反思
        recent_messages: list = []
        if self._bus and cmd.session_id:
            try:
                from alex.kernel.contracts.memory import GetContext
                recent_messages = await self._bus.request(GetContext(session_id=cmd.session_id))
            except Exception:
                logger.debug("Failed to get context for reflect", exc_info=True)
        try:
            result = await self._service.reflect(recent_messages=recent_messages, episodes=[])
        except Exception:
            logger.warning("reflect failed", exc_info=True)
            return
        logger.info("reflect done new=%s updated=%s deprecated=%s", result.get("new", 0), result.get("updated", 0), result.get("deprecated", 0))
        if self._bus:
            self._bus.publish(SkillsReflected(
                session_id=cmd.session_id,
                new=result.get("new", 0),
                updated=result.get("updated", 0),
                deprecated=result.get("deprecated", 0),
                names=result.get("new_skill_names", []),
                updated_names=result.get("updated_skill_names", []),
            ))

    async def _on_feedback(self, cmd: FeedbackSubmitted) -> None:
        """收到用户反馈 → 记录 skill usage，负面反馈触发反思。"""
        # 记录 usage（如果有关联的 skill）
        # 负面反馈触发反思
        if not cmd.positive and cmd.session_id:
            # 触发反思
            recent_messages: list = []
            try:
                from alex.kernel.contracts.memory import GetContext
                recent_messages = await self._bus.request(GetContext(session_id=cmd.session_id))
            except Exception:
                pass
            result = await self._service.reflect(recent_messages=recent_messages, episodes=[])
            if self._bus:
                self._bus.publish(SkillsReflected(
                    session_id=cmd.session_id,
                    new=result.get("new", 0),
                    updated=result.get("updated", 0),
                    deprecated=result.get("deprecated", 0),
                    names=result.get("new_skill_names", []),
                    updated_names=result.get("updated_skill_names", []),
                ))

    @property
    def service(self) -> Any:
        return self._service
