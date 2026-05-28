"""Tests for the permission policy + executor integration."""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from alex.tools.permissions import (
    DEFAULT_ALLOWED,
    PERMISSION_NETWORK,
    PERMISSION_READ,
    PERMISSION_SHELL,
    PERMISSION_WRITE,
    PermissionPolicy,
    gate_tool_with_policy,
    gate_tools_with_policy,
    required_permission,
)
from alex.tools.ports import ToolExecutionContext
from alex.tools.registry import ToolRegistry
from alex.tools.executor import ToolExecutor


class _NoopInput(BaseModel):
    text: str = Field(default="ok")


async def _noop(text: str = "ok") -> str:
    return text


def _make_tool(name: str, permission: str | None) -> StructuredTool:
    metadata = {"required_permission": permission} if permission else None
    return StructuredTool.from_function(
        coroutine=_noop, name=name, description=name, args_schema=_NoopInput,
        metadata=metadata,
    )


class TestPermissionPolicy:
    @pytest.mark.asyncio
    async def test_unrestricted_tool_passes(self):
        policy = PermissionPolicy()
        granted, _ = await policy.check("anything", None)
        assert granted

    @pytest.mark.asyncio
    async def test_default_allows_read_and_network(self):
        policy = PermissionPolicy()
        assert (await policy.check("t", PERMISSION_READ))[0]
        assert (await policy.check("t", PERMISSION_NETWORK))[0]
        assert not (await policy.check("t", PERMISSION_WRITE))[0]
        assert not (await policy.check("t", PERMISSION_SHELL))[0]

    @pytest.mark.asyncio
    async def test_explicit_deny_overrides_allow(self):
        policy = PermissionPolicy(
            allowed={PERMISSION_READ, PERMISSION_WRITE},
            denied={PERMISSION_WRITE},
        )
        assert (await policy.check("t", PERMISSION_READ))[0]
        granted, reason = await policy.check("t", PERMISSION_WRITE)
        assert not granted
        assert "denied" in reason

    @pytest.mark.asyncio
    async def test_confirm_hook_grants_and_caches(self):
        calls: list[tuple[str, str]] = []

        async def _hook(req) -> bool:
            calls.append((req.tool_name, req.permission))
            return True

        policy = PermissionPolicy(confirm_hook=_hook)
        # First call must consult the hook.
        granted, _ = await policy.check("t", PERMISSION_WRITE)
        assert granted is True
        assert calls == [("t", PERMISSION_WRITE)]
        # Second call should use the cached grant.
        granted, _ = await policy.check("t", PERMISSION_WRITE)
        assert granted is True
        assert calls == [("t", PERMISSION_WRITE)]

    @pytest.mark.asyncio
    async def test_confirm_hook_can_refuse(self):
        async def _hook(_req) -> bool:
            return False

        policy = PermissionPolicy(confirm_hook=_hook)
        granted, reason = await policy.check("t", PERMISSION_WRITE)
        assert not granted
        assert "user denied" in reason

    @pytest.mark.asyncio
    async def test_confirm_hook_allow_once_does_not_cache(self):
        calls: list[tuple[str, str]] = []

        async def _hook(req):
            calls.append((req.tool_name, req.permission))
            return (True, False)  # allow once, do not remember

        policy = PermissionPolicy(confirm_hook=_hook)
        assert (await policy.check("t", PERMISSION_WRITE))[0]
        assert (await policy.check("t", PERMISSION_WRITE))[0]
        # The hook is consulted both times because remember=False.
        assert len(calls) == 2
        assert PERMISSION_WRITE not in policy.allowed

    @pytest.mark.asyncio
    async def test_confirm_hook_allow_always_caches(self):
        calls: list[tuple[str, str]] = []

        async def _hook(req):
            calls.append((req.tool_name, req.permission))
            return (True, True)  # allow always

        policy = PermissionPolicy(confirm_hook=_hook)
        assert (await policy.check("t", PERMISSION_WRITE))[0]
        assert (await policy.check("t", PERMISSION_WRITE))[0]
        # Cached after the first grant.
        assert len(calls) == 1
        assert PERMISSION_WRITE in policy.allowed

    def test_from_env_overrides(self, monkeypatch):
        monkeypatch.setenv("ALEX_TOOL_PERMISSIONS", "read,write")
        monkeypatch.setenv("ALEX_TOOL_DENY", "write")
        policy = PermissionPolicy.from_env()
        assert PERMISSION_READ in policy.allowed
        assert PERMISSION_WRITE in policy.allowed
        assert PERMISSION_WRITE in policy.denied

    def test_from_env_defaults_when_unset(self, monkeypatch):
        monkeypatch.delenv("ALEX_TOOL_PERMISSIONS", raising=False)
        monkeypatch.delenv("ALEX_TOOL_DENY", raising=False)
        policy = PermissionPolicy.from_env()
        assert policy.allowed == set(DEFAULT_ALLOWED)


class TestRequiredPermissionHelper:
    def test_reads_from_metadata(self):
        tool = _make_tool("t", PERMISSION_WRITE)
        assert required_permission(tool) == "write"

    def test_returns_none_when_missing(self):
        tool = _make_tool("t", None)
        assert required_permission(tool) is None


class TestExecutorEnforcesPolicy:
    @pytest.mark.asyncio
    async def test_blocks_unallowed_permission(self):
        registry = ToolRegistry()
        registry.register(_make_tool("write_thing", PERMISSION_WRITE))
        executor = ToolExecutor(registry, permissions=PermissionPolicy())
        result = await executor.execute(
            ToolExecutionContext(session_id="s1"), "write_thing", {"text": "ok"},
        )
        assert result.startswith("Error:")
        assert "blocked" in result

    @pytest.mark.asyncio
    async def test_runs_when_permission_granted(self):
        registry = ToolRegistry()
        registry.register(_make_tool("write_thing", PERMISSION_WRITE))
        policy = PermissionPolicy(allowed={PERMISSION_WRITE})
        executor = ToolExecutor(registry, permissions=policy)
        result = await executor.execute(
            ToolExecutionContext(session_id="s1"), "write_thing", {"text": "hi"},
        )
        assert result == "hi"

    @pytest.mark.asyncio
    async def test_set_permissions_propagates_to_executor(self):
        registry = ToolRegistry()
        registry.register(_make_tool("write_thing", PERMISSION_WRITE))
        executor = ToolExecutor(registry, permissions=PermissionPolicy())
        executor.set_permissions(PermissionPolicy(allowed={PERMISSION_WRITE}))
        result = await executor.execute(
            ToolExecutionContext(session_id="s1"), "write_thing", {"text": "hi"},
        )
        assert result == "hi"


class TestToolGating:
    """Tools invoked directly via ainvoke (LangGraph path) must also be gated."""

    @pytest.mark.asyncio
    async def test_unwrapped_no_permission_passes_through(self):
        tool = _make_tool("safe", None)
        gated = gate_tool_with_policy(tool, PermissionPolicy())
        assert gated is tool
        # No metadata wrapper applied.
        assert getattr(tool, "_alex_permission_gated", None) is None
        result = await tool.ainvoke({"text": "ok"})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_blocks_when_permission_missing(self):
        tool = _make_tool("write_thing", PERMISSION_WRITE)
        gate_tool_with_policy(tool, PermissionPolicy())
        result = await tool.ainvoke({"text": "no"})
        assert "blocked" in result

    @pytest.mark.asyncio
    async def test_allows_when_permission_granted(self):
        tool = _make_tool("write_thing", PERMISSION_WRITE)
        gate_tool_with_policy(tool, PermissionPolicy(allowed={PERMISSION_WRITE}))
        result = await tool.ainvoke({"text": "yes"})
        assert result == "yes"

    @pytest.mark.asyncio
    async def test_idempotent_wrapping(self):
        tool = _make_tool("write_thing", PERMISSION_WRITE)
        gate_tool_with_policy(tool, PermissionPolicy())
        wrapped_once = tool.coroutine
        gate_tool_with_policy(tool, PermissionPolicy())
        wrapped_twice = tool.coroutine
        # The second wrap reuses the existing wrapper.
        assert wrapped_once is wrapped_twice

    @pytest.mark.asyncio
    async def test_rewrapping_swaps_policy(self):
        tool = _make_tool("write_thing", PERMISSION_WRITE)
        deny = PermissionPolicy()
        allow = PermissionPolicy(allowed={PERMISSION_WRITE})
        gate_tool_with_policy(tool, deny)
        result_blocked = await tool.ainvoke({"text": "x"})
        gate_tool_with_policy(tool, allow)  # update policy in place
        result_ok = await tool.ainvoke({"text": "y"})
        assert "blocked" in result_blocked
        assert result_ok == "y"

    def test_bulk_helper(self):
        tools = [
            _make_tool("a", PERMISSION_READ),
            _make_tool("b", PERMISSION_WRITE),
            _make_tool("c", None),
        ]
        gate_tools_with_policy(tools, PermissionPolicy())
        assert getattr(tools[0], "_alex_permission_gated", None) is not None
        assert getattr(tools[1], "_alex_permission_gated", None) is not None
        assert getattr(tools[2], "_alex_permission_gated", None) is None

    @pytest.mark.asyncio
    async def test_executor_skips_redundant_check_for_gated_tool(self):
        """When a tool is already gated, the executor must not double-prompt."""
        registry = ToolRegistry()
        tool = _make_tool("write_thing", PERMISSION_WRITE)
        registry.register(tool)
        prompt_calls: list[str] = []

        async def _hook(req):
            prompt_calls.append(req.tool_name)
            return True

        # Same policy instance backs both the wrapper and the executor.
        policy = PermissionPolicy(confirm_hook=_hook)
        gate_tool_with_policy(tool, policy)
        executor = ToolExecutor(registry, permissions=policy)
        result = await executor.execute(
            ToolExecutionContext(session_id="s1"), "write_thing", {"text": "hi"},
        )
        assert result == "hi"
        # Exactly one confirm prompt — the wrapper handled it; the
        # executor saw is_gated() and skipped its own check.
        assert prompt_calls == ["write_thing"]
