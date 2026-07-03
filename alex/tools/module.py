"""ToolsModule — 工具网关，通过 bus 暴露工具目录、执行和权限确认。

简化设计：
1. 启动时将所有工具信息广播到 bus（ToolsProvided 事件）
2. 其他模块通过 bus request 提交执行请求（ExecuteTool）
3. 需要权限确认时：发布 ToolApprovalRequested → TUI 展示弹窗 →
   TUI 发布 ToolApprovalResolved → 工具模块收到后继续/中止执行 → 结果通过 request reply 返回
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any

from alex.kernel.contracts.tools import (
    ExecuteTool,
    GetToolCatalog,
    InvokeProviderTool,
    RegisterTool,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolFinished,
    ToolsProvided,
    ToolStarted,
    UnregisterTool,
)
from alex.kernel.dto.tool import ToolResult, ToolSpec
from alex.tools.models import AlexTool
from alex.tools.permissions import (
    PermissionPolicy,
    build_approval_request,
    required_permission,
)
from alex.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _accepts_param(coroutine: Callable[..., Any], param_name: str) -> bool:
    """Return True if *coroutine* accepts *param_name* as a keyword argument."""
    try:
        sig = inspect.signature(coroutine)
        param = sig.parameters.get(param_name)
        return param is not None and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.VAR_KEYWORD,
        )
    except (TypeError, ValueError):
        return False


class ToolsModule:
    """工具网关 — 目录管理、执行路由、权限确认，全部通过 bus 通信。

    核心流程：
    - start() 时注册 request handler 并广播内建工具目录
    - GetToolCatalog → 返回合并后的工具列表（builtin + mcp + plugin）
    - ExecuteTool → 检查权限 → 需要确认则走 bus 事件回执 → 执行 → 返回结果
    - ToolsProvided 事件 → 收编外部 provider 的工具到目录
    """

    name = "tools"
    dependencies: list[str] = []

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        permissions: PermissionPolicy | None = None,
    ) -> None:
        self._registry: ToolRegistry = registry or ToolRegistry()
        self._permissions: PermissionPolicy = permissions or PermissionPolicy.from_env()
        self._bus: Any = None
        # 外部 provider 工具（mcp, plugin）
        self._provider_specs: dict[str, list[ToolSpec]] = {}
        # 权限确认等待队列：req_id → Future[(granted, remember)]
        self._pending_approvals: dict[str, asyncio.Future[tuple[bool, bool]]] = {}

    async def start(self, bus: Any) -> None:
        """连接到 bus，注册 handler，注册内建工具，广播工具目录。"""
        self._bus = bus

        # 注册 request handler
        bus.provide(GetToolCatalog, self._handle_catalog)
        bus.provide(ExecuteTool, self._handle_execute)
        bus.provide(InvokeProviderTool, self._handle_provider_execute)
        bus.provide(RegisterTool, self._handle_register_tool)
        bus.provide(UnregisterTool, self._handle_unregister_tool)

        # 订阅事件
        await bus.subscribe(ToolsProvided, self._on_tools_provided)
        await bus.subscribe(ToolApprovalResolved, self._on_approval_resolved)

        # 注册所有内建工具（如果 registry 为空则自动注册）
        if not self._registry.list():
            self._register_builtin_tools()

        # 启动时广播所有内建工具信息
        self._broadcast_builtin_tools()

        logger.info("ToolsModule started — catalog broadcast, handlers registered")

    async def stop(self) -> None:
        """清理待处理的权限请求。"""
        # 取消所有未完成的权限确认
        for future in self._pending_approvals.values():
            if not future.done():
                future.set_result((False, False))
        self._pending_approvals.clear()
        self._bus = None

    # ── 工具注册（直接 API，用于启动时批量注册内建工具）────────────────

    def register_tool(self, tool: AlexTool) -> None:
        """注册单个内建工具。"""
        self._registry.register(tool)

    def register_tools_batch(self, tools: list[AlexTool]) -> None:
        """批量注册内建工具。"""
        for t in tools:
            self._registry.register(t)

    # ── 内建工具注册 ─────────────────────────────────────────────────

    def _register_builtin_tools(self) -> None:
        """注册所有内建工具（time, web, fs, shell, cron 等）。"""
        import json
        from alex.tools import (
            FileReadTracker,
            create_available_shell_tools,
            create_cron_cancel_tool,
            create_edit_tool,
            create_glob_tool,
            create_grep_tool,
            create_read_tool,
            create_time_tool,
            create_web_fetch_tool,
            create_web_search_tool,
            create_write_tool,
        )
        from alex.tools.cron import create_cron_tool
        from alex.kernel.contracts.cron import ListCronJobs

        tracker = FileReadTracker()

        base_tools = [
            create_time_tool(),
            create_web_search_tool(),
            create_web_fetch_tool(),
            create_read_tool(tracker=tracker),
            create_write_tool(tracker=tracker),
            create_edit_tool(tracker=tracker),
            create_glob_tool(),
            create_grep_tool(),
            *create_available_shell_tools(),
        ]

        for tool in base_tools:
            self._registry.register(tool)

        # cron 和 cron_cancel 需要 bus 引用
        self._registry.register(create_cron_tool(self._bus))
        self._registry.register(create_cron_cancel_tool(self._bus))

        # cron_jobs — 通过 bus request 获取
        bus = self._bus

        async def _list_cron_jobs_tool(**_kwargs) -> str:
            try:
                jobs = await bus.request(ListCronJobs())
                return json.dumps(jobs, ensure_ascii=False, default=str)
            except Exception as e:
                return f"Error: {e}"

        from pydantic import BaseModel, Field

        class CronJobsInput(BaseModel):
            query: str = Field(default="", description="Optional filter")
            limit: str = Field(default="", description="Max results")

        self._registry.register(AlexTool.from_function(
            name="cron_jobs",
            description=(
                "List current cron jobs, including durable jobs restored from disk. "
                "Returns job id, schedule, status, prompt, and next run time."
            ),
            coroutine=_list_cron_jobs_tool,
            args_schema=CronJobsInput,
        ))

        logger.info("Registered %d builtin tools", len(self._registry.list()))

    # ── 广播工具目录 ──────────────────────────────────────────────────

    def _broadcast_builtin_tools(self) -> None:
        """将所有内建工具信息广播到 bus，订阅者可直接获取。"""
        if not self._bus:
            return
        tools = self._registry.list()
        specs = [
            {
                "name": t.name,
                "description": t.description,
                "json_schema": t.parameters,
                "provider": "builtin",
                "metadata": t.metadata or {},
            }
            for t in tools
        ]
        if specs:
            self._bus.publish(ToolsProvided(provider="builtin", specs=specs))
            logger.info("Broadcast %d builtin tools to bus", len(specs))

    # ── Request Handlers ─────────────────────────────────────────────

    async def _handle_catalog(self, _req: GetToolCatalog) -> list[ToolSpec]:
        """返回合并后的工具目录（builtin + mcp + plugin）。"""
        catalog: list[ToolSpec] = []

        # 内建工具
        for tool in self._registry.list():
            catalog.append(ToolSpec(
                name=tool.name,
                description=tool.description,
                json_schema=tool.parameters,
                provider="builtin",
                metadata=tool.metadata or {},
            ))

        # 外部 provider 工具
        for provider_specs in self._provider_specs.values():
            catalog.extend(provider_specs)

        return catalog

    async def _handle_execute(self, req: ExecuteTool) -> ToolResult:
        """执行工具 — 核心流程：路由 → 权限检查 → 执行 → 返回结果。

        权限确认完全通过 bus 事件实现：
        1. 发布 ToolApprovalRequested 到 bus
        2. 等待 ToolApprovalResolved 事件（TUI 确认后发布）
        3. 根据结果继续执行或拒绝
        """
        logger.info("execute tool=%s sid=%s", req.name, req.session_id)
        run_id = uuid.uuid4().hex[:12]

        # 检查是否为外部 provider 工具 → 转发
        for provider, specs in self._provider_specs.items():
            for spec in specs:
                if spec.name == req.name:
                    return await self._forward_to_provider(req, provider, run_id)

        # 内建工具执行
        tool = self._registry.get(req.name)
        if tool is None:
            return ToolResult(name=req.name, error=f"tool '{req.name}' not found", run_id=run_id)

        # 权限检查（通过 bus 事件回执）
        granted, reason = await self._check_permission_via_bus(tool, req.args)
        if not granted:
            logger.info("tool blocked=%s reason=%s", req.name, reason)
            return ToolResult(name=req.name, error=f"tool '{req.name}' blocked: {reason}", run_id=run_id)

        # 发布工具开始执行事件 → TUI 从 bus 订阅渲染
        if self._bus:
            self._bus.publish(ToolStarted(
                session_id=req.session_id,
                turn_id=req.turn_id,
                tool_id=run_id,
                tool_name=req.name,
                tool_input=req.args,
            ))

        # 执行工具 — 注入 session 上下文，让工具能将定时任务关联到正确的 session
        args = dict(req.args)
        if req.session_id and _accepts_param(tool.coroutine, "_session_id"):
            args["_session_id"] = req.session_id

        try:
            result_str = await tool.invoke(args)
            result = ToolResult(name=req.name, output=str(result_str), run_id=run_id)
        except Exception as e:
            result = ToolResult(name=req.name, error=f"{type(e).__name__}: {e}", run_id=run_id)

        # 发布工具执行完成事件 → TUI 从 bus 订阅渲染
        if self._bus:
            output = result.output if result.ok else result.error
            self._bus.publish(ToolFinished(
                session_id=req.session_id,
                turn_id=req.turn_id,
                tool_id=run_id,
                output=output,
            ))

        return result

    async def _handle_provider_execute(self, req: InvokeProviderTool) -> ToolResult:
        """执行外部 provider 工具（mcp/plugin 持有实际连接）。"""
        run_id = uuid.uuid4().hex[:12]

        # 尝试在 registry 中查找（mcp 模块可能已注册了可执行的工具）
        tool = self._registry.get(req.name)
        if tool is None:
            return ToolResult(
                name=req.name,
                error=f"Tool '{req.name}' not found for provider '{req.provider}'",
                run_id=run_id,
            )

        try:
            result_str = await tool.invoke(req.args)
            return ToolResult(name=req.name, output=str(result_str), run_id=run_id)
        except Exception as e:
            return ToolResult(name=req.name, error=f"{type(e).__name__}: {e}", run_id=run_id)

    async def _handle_register_tool(self, req: RegisterTool) -> None:
        """通过 bus 注册工具，使用传入的 json_schema。"""
        import inspect

        coroutine = req.callable_ref
        # 包装同步函数为协程
        if not inspect.iscoroutinefunction(coroutine):
            original = coroutine

            async def _wrapper(**kw: Any) -> str:
                return str(original(**kw))

            coroutine = _wrapper

        tool = AlexTool(
            name=req.name,
            description=req.description,
            parameters=req.json_schema or {"type": "object", "properties": {}, "required": []},
            coroutine=coroutine,
            metadata=dict(req.metadata or {}),
        )
        self._registry.register(tool)
        # 广播新注册的工具
        if self._bus:
            self._bus.publish(ToolsProvided(provider="builtin", specs=[{
                "name": tool.name,
                "description": tool.description,
                "json_schema": tool.parameters,
                "provider": "builtin",
                "metadata": tool.metadata or {},
            }]))

    async def _handle_unregister_tool(self, req: UnregisterTool) -> None:
        """通过 bus 移除工具。"""
        self._registry.unregister(req.name)

    # ── 权限检查（完全通过 bus 事件）─────────────────────────────────

    async def _check_permission_via_bus(
        self, tool: AlexTool, args: dict[str, Any],
    ) -> tuple[bool, str]:
        """检查工具权限，需要确认时通过 bus 发布请求并等待回执。

        流程：
        1. 检查是否需要权限
        2. 检查是否已在 allowed 集合中（自动放行）
        3. 需要确认 → 发布 ToolApprovalRequested → 等待 ToolApprovalResolved
        """
        perm = required_permission(tool)
        if not perm:
            return True, ""

        # 已在 denied 集合
        if perm in self._permissions.denied:
            return False, f"permission '{perm}' explicitly denied"

        # 已在 allowed 集合（自动放行）
        if perm in self._permissions.allowed:
            return True, ""

        # 需要用户确认 → 通过 bus 发布权限请求
        approval_request = await build_approval_request(tool, perm, args)
        req_id = uuid.uuid4().hex[:12]

        # 创建 Future 等待 TUI 回复
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, bool]] = loop.create_future()
        self._pending_approvals[req_id] = future

        # 发布权限请求到 bus → TUI 订阅此事件展示确认弹窗
        logger.info("permission approval requested tool=%s perm=%s", tool.name, perm)
        self._bus.publish(ToolApprovalRequested(
            req_id=req_id,
            tool_name=tool.name,
            preview=approval_request.summary or "",
            permission=perm,
        ))

        # 等待 TUI 通过 bus 发布 ToolApprovalResolved
        try:
            granted, remember = await asyncio.wait_for(future, timeout=120.0)
        except asyncio.TimeoutError:
            _ = self._pending_approvals.pop(req_id, None)
            return False, f"permission '{perm}' approval timed out"
        finally:
            _ = self._pending_approvals.pop(req_id, None)

        if granted and remember:
            self._permissions.allowed.add(perm)

        if not granted:
            return False, f"user denied permission '{perm}'"

        return True, ""

    # ── 事件处理 ─────────────────────────────────────────────────────

    async def _on_tools_provided(self, event: ToolsProvided) -> None:
        """收编外部 provider 宣布的工具到目录。"""
        # 忽略自己广播的 builtin 事件（避免重复）
        if event.provider == "builtin":
            return
        specs = [
            ToolSpec(
                name=s["name"],
                description=s.get("description", ""),
                json_schema=s.get("json_schema", {}),
                provider=event.provider,
                metadata=s.get("metadata", {}),
            )
            for s in event.specs
        ]
        self._provider_specs[event.provider] = specs
        logger.info("Received %d tools from provider '%s'", len(specs), event.provider)

    async def _on_approval_resolved(self, event: ToolApprovalResolved) -> None:
        """TUI 确认/拒绝权限后，通过 bus 发布此事件，工具模块在此接收。"""
        logger.info("approval resolved req_id=%s granted=%s", event.req_id, event.granted)
        future = self._pending_approvals.get(event.req_id)
        if future is not None and not future.done():
            future.set_result((event.granted, event.remember))

    # ── 辅助方法 ─────────────────────────────────────────────────────

    async def _forward_to_provider(
        self, req: ExecuteTool, provider: str, run_id: str,
    ) -> ToolResult:
        """将执行请求转发给外部 provider，发布 ToolStarted/ToolFinished 事件。"""
        logger.info("forwarding tool=%s to provider=%s", req.name, provider)
        # 发布开始事件
        if self._bus:
            self._bus.publish(ToolStarted(
                session_id=req.session_id,
                turn_id=req.turn_id,
                tool_id=run_id,
                tool_name=req.name,
                tool_input=req.args,
            ))

        try:
            result = await self._bus.request(InvokeProviderTool(
                session_id=req.session_id,
                turn_id=req.turn_id,
                provider=provider,
                name=req.name,
                args=req.args,
                ctx=req.ctx,
            ), timeout=req.timeout)
        except Exception as e:
            result = ToolResult(name=req.name, error=str(e), run_id=run_id)

        # 发布完成事件
        if self._bus:
            output = result.output if result.ok else result.error
            self._bus.publish(ToolFinished(
                session_id=req.session_id,
                turn_id=req.turn_id,
                tool_id=run_id,
                output=output,
            ))

        return result

    # ── 属性访问 ─────────────────────────────────────────────────────

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def permissions(self) -> PermissionPolicy:
        return self._permissions
