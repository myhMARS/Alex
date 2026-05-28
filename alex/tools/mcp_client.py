"""MCP (Model Context Protocol) client adapter.

Bridges remote MCP servers — launched as stdio subprocesses — into
LangChain ``StructuredTool`` instances so the rest of the agent can
treat them as ordinary tools.

The ``mcp`` SDK is an optional dependency.  When it is missing this
module degrades gracefully: ``load_mcp_tools_from_config`` returns an
empty list and ``MCPUnavailableError`` describes how to install it.

Configuration file (``~/.alex/mcp.json``)::

    {
      "mcpServers": {
        "filesystem": {
          "command": "uvx",
          "args": ["mcp-server-filesystem", "/Users/me/Notes"],
          "env": {}
        }
      }
    }

Each entry spawns a long-lived subprocess; tools are listed once at
connect time and cached for the life of the session.  Cleanup on
shutdown is the host's responsibility (call ``MCPClientPool.aclose()``
during agent teardown).
"""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from alex.tools.permissions import PERMISSION_NETWORK

logger = logging.getLogger(__name__)


class MCPUnavailableError(RuntimeError):
    """Raised when the optional ``mcp`` SDK is not installed."""


def _import_mcp():
    try:
        import mcp  # noqa: F401
        from mcp import ClientSession, StdioServerParameters  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
    except ImportError as e:
        raise MCPUnavailableError(
            "MCP support requires the 'mcp' package. "
            "Install it via: pip install mcp"
        ) from e
    return ClientSession, StdioServerParameters, stdio_client


# ── config loading ────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path.home() / ".alex" / "mcp.json"


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def load_mcp_config(path: Path | None = None) -> list[MCPServerConfig]:
    """Parse ``~/.alex/mcp.json`` into a list of server configs.

    Returns an empty list when the file does not exist; raises
    :class:`ValueError` for malformed payloads.
    """
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        return []
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"failed to parse {target}: {e}") from e

    raw = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return []

    configs: list[MCPServerConfig] = []
    for name, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        command = str(payload.get("command", "")).strip()
        if not command:
            continue
        args = payload.get("args", [])
        if not isinstance(args, list):
            args = []
        env = payload.get("env", {})
        if not isinstance(env, dict):
            env = {}
        enabled = payload.get("disabled") is not True
        configs.append(MCPServerConfig(
            name=name,
            command=command,
            args=[str(a) for a in args],
            env={str(k): str(v) for k, v in env.items()},
            enabled=enabled,
        ))
    return configs


# ── tool adaptation ───────────────────────────────────────────────────

def _safe_field_name(raw: str) -> str:
    """Make a JSON-schema property name safe to use as a Python attribute."""
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def _schema_to_pydantic(schema: dict[str, Any] | None, model_name: str) -> type[BaseModel]:
    """Best-effort conversion of a JSON schema ``object`` into a pydantic model.

    Unknown / complex shapes fall back to ``Any`` so the agent can still
    invoke the tool — schema validation lives on the MCP server side.
    """
    from pydantic import create_model

    if not isinstance(schema, dict) or schema.get("type") != "object":
        return create_model(model_name, __base__=BaseModel)

    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    fields: dict[str, tuple[Any, Any]] = {}
    for raw_name, prop in properties.items():
        py_name = _safe_field_name(raw_name)
        py_type = _json_type_to_python(prop)
        default = ... if raw_name in required else prop.get("default", None)
        # pydantic v2 distinguishes required from default-None by ``...``.
        if raw_name in required:
            fields[py_name] = (py_type, ...)
        else:
            fields[py_name] = (py_type | None, default)

    if not fields:
        return create_model(model_name, __base__=BaseModel)
    return create_model(model_name, __base__=BaseModel, **fields)  # type: ignore[arg-type]


def _json_type_to_python(prop: dict[str, Any]) -> Any:
    if not isinstance(prop, dict):
        return Any
    t = prop.get("type")
    if t == "string":
        return str
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        return list
    if t == "object":
        return dict
    return Any


def _build_mcp_tool(
    *,
    server_name: str,
    tool_name: str,
    description: str,
    schema: dict[str, Any] | None,
    invoke,
) -> StructuredTool:
    """Wrap an MCP ``call_tool`` invocation as a LangChain ``StructuredTool``."""
    args_schema = _schema_to_pydantic(schema, model_name=f"MCP_{server_name}_{tool_name}_Input")

    async def _call(**kwargs):
        try:
            result = await invoke(tool_name, kwargs)
        except Exception as e:
            return f"Error calling MCP tool {server_name}.{tool_name}: {type(e).__name__}: {e}"
        return _format_mcp_result(result)

    qualified_name = f"mcp__{server_name}__{tool_name}".lower()
    return StructuredTool.from_function(
        coroutine=_call,
        name=qualified_name,
        description=f"[MCP:{server_name}] {description}".strip(),
        args_schema=args_schema,
        metadata={
            "required_permission": PERMISSION_NETWORK,
            "mcp_server": server_name,
            "mcp_tool": tool_name,
        },
    )


def _format_mcp_result(result: Any) -> str:
    """Convert an MCP CallToolResult into a plain string."""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        data = getattr(item, "data", None)
        if data is not None:
            parts.append(f"<binary {len(data)} bytes>")
            continue
        parts.append(str(item))
    return "\n".join(parts) if parts else ""


# ── client pool ───────────────────────────────────────────────────────

@dataclass
class MCPConnection:
    """A single live MCP session plus its discovered tools."""

    config: MCPServerConfig
    tools: list[StructuredTool] = field(default_factory=list)
    error: str | None = None


class MCPClientPool:
    """Manages stdio MCP sessions and exposes them as LangChain tools.

    The pool keeps an ``AsyncExitStack`` so all subprocesses and
    ``ClientSession`` async-context-managers are torn down together
    when ``aclose()`` is called.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._connections: list[MCPConnection] = []
        self._closed = False

    @property
    def connections(self) -> list[MCPConnection]:
        return list(self._connections)

    async def connect_all(self, configs: list[MCPServerConfig]) -> list[MCPConnection]:
        ClientSession, StdioServerParameters, stdio_client = _import_mcp()

        for cfg in configs:
            if not cfg.enabled:
                self._connections.append(MCPConnection(config=cfg, error="disabled"))
                continue
            connection = MCPConnection(config=cfg)
            try:
                params = StdioServerParameters(
                    command=cfg.command,
                    args=cfg.args,
                    env=cfg.env or None,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                tools = []
                for spec in getattr(listed, "tools", []) or []:
                    tool = _build_mcp_tool(
                        server_name=cfg.name,
                        tool_name=spec.name,
                        description=getattr(spec, "description", "") or "",
                        schema=getattr(spec, "inputSchema", None),
                        invoke=session.call_tool,
                    )
                    tools.append(tool)
                connection.tools = tools
            except Exception as e:
                logger.warning("MCP server '%s' failed to connect: %s", cfg.name, e)
                connection.error = f"{type(e).__name__}: {e}"
            self._connections.append(connection)
        return self._connections

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stack.aclose()


async def load_mcp_tools_from_config(
    *,
    config_path: Path | None = None,
) -> tuple[MCPClientPool, list[StructuredTool]]:
    """Convenience: load + connect everything described by ``mcp.json``.

    Returns the live pool (for shutdown) and the flat list of tools.
    Raises :class:`MCPUnavailableError` if the SDK is not installed and
    the configuration file actually requested any servers.
    """
    configs = load_mcp_config(config_path)
    if not configs:
        return MCPClientPool(), []

    pool = MCPClientPool()
    try:
        await pool.connect_all(configs)
    except MCPUnavailableError:
        await pool.aclose()
        raise

    tools: list[StructuredTool] = []
    for conn in pool.connections:
        tools.extend(conn.tools)
    return pool, tools
