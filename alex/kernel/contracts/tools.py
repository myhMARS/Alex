"""Tool contracts — catalog, execution, approval events, and registries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alex.kernel.bus import Event, Request
from alex.kernel.dto.tool import ToolResult, ToolSpec


# ── Requests (agent → tools gateway) ─────────────────────────────────────────

@dataclass
class GetToolCatalog(Request[list[ToolSpec]]):
    """Request the merged tool catalog (builtin + mcp + plugin)."""


@dataclass
class ExecuteTool(Request[ToolResult]):
    """Request execution of a named tool."""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    ctx: Any = None
    timeout: float = 180.0  # 调用方可指定超时（秒）


@dataclass
class RegisterTool(Request[None]):
    """Register a tool with the tools gateway."""
    name: str = ""
    description: str = ""
    json_schema: dict[str, Any] = field(default_factory=dict)
    callable_ref: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnregisterTool(Request[None]):
    """Remove a tool from the catalog."""
    name: str = ""


# ── Events (broadcast) ───────────────────────────────────────────────────────

@dataclass
class ToolsProvided(Event):
    """Published by mcp / plugin loaders to announce available tools.

    The tools gateway subscribes to this and merges specs into its catalog.
    ``metadata`` carries optional status info (e.g. MCP connection state).
    """
    provider: str = ""  # "mcp" | "plugin"
    specs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolStarted(Event):
    """A tool execution has started (UI notification)."""
    tool_id: str = ""
    tool_name: str = ""
    tool_input: Any = None
    stream_id: str = ""


@dataclass
class ToolFinished(Event):
    """A tool execution has finished (UI notification)."""
    tool_id: str = ""
    output: Any = None
    stream_id: str = ""


# ── Tool approval (event + correlation) ──────────────────────────────────────

@dataclass
class ToolApprovalRequested(Event):
    """Published by tools when a gated tool needs user approval.

    The TUI shows a confirmation modal and replies with ToolApprovalResolved.
    """
    req_id: str = ""
    tool_name: str = ""
    preview: str = ""
    permission: str = ""


@dataclass
class ToolApprovalResolved(Event):
    """Published by TUI when the user grants or denies a tool approval request."""
    req_id: str = ""
    granted: bool = False
    remember: bool = False


# ── MCP status (TUI → MCP module) ────────────────────────────────────────────

@dataclass
class GetMCPStatus(Request[dict[str, Any]]):
    """Request current MCP connection state from the MCP module.

    Returns ``{"servers": [...], "status_message": "..."}``.
    """

# ── Provider-level tool invocation (mcp / plugin → tools gateway) ────────────

@dataclass
class InvokeProviderTool(Request[ToolResult]):
    """Request the tools gateway to execute a tool via a specific provider.

    Used when the tools gateway routes an ExecuteTool to mcp or a plugin.
    Returns ``ToolResult``.
    """
    provider: str = ""  # "mcp" | "plugin"
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    ctx: Any = None
