"""MCP (Model Context Protocol) client adapter.

Bridges MCP servers — launched as stdio subprocesses or reached over
HTTP transports — into lightweight tool wrappers so the
MCP module can treat them as ordinary tools without importing ``alex.tools``.

The ``mcp`` SDK is part of Alex's main dependencies.  If it is missing
from the active environment, ``MCPUnavailableError`` explains that the
installation is incomplete.

Configuration file (``~/.alex/mcp.json``)::

    {
      "mcpServers": {
        "local-server": {
          "command": "your-mcp-command",
          "args": ["--your-arg", "value"],
          "env": {}
        },
        "http-server": {
          "url": "http://localhost:8000/mcp",
          "headers": {"Authorization": "Bearer token-value"},
          "transport": "streamable-http"
        }
      }
    }

Tools are listed once at connect time and cached for the life of the
session. Cleanup on shutdown is the host's responsibility (call
``MCPClientPool.aclose()`` during agent teardown).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import httpx
from pydantic import BaseModel

from alex.config import is_mcp_debug_enabled, load_mcp_config, MCP_CONFIG_PATH, MCPServerConfig

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
DEFAULT_CONFIG_PATH = MCP_CONFIG_PATH

# ── lightweight MCP tool wrapper (no alex.tools dependency) ──────────────

# Permission constant — network access is required for MCP server communication.
_PERMISSION_NETWORK = "network"


class MCPTool:
    """Lightweight wrapper around an MCP tool callable.

    Replaces ``AlexTool`` so the MCP module can store and invoke tools
    without importing ``alex.tools``.  The tools gateway in ``alex.tools``
    is responsible for converting these into full ``AlexTool`` instances
    if needed.
    """

    __slots__ = ("name", "description", "parameters", "metadata", "_invoke")

    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        invoke: Callable[..., Any],
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters or {}
        self.metadata = metadata or {}
        self._invoke = invoke

    async def invoke(self, args: dict[str, Any]) -> Any:
        """Execute the tool with the given arguments."""
        return await self._invoke(self.name, args)

    async def ainvoke(self, args: dict[str, Any]) -> str:
        """Async invoke — returns string result (compatible with AlexTool API)."""
        result = await self.invoke(args)
        return str(result)


def _debug_enabled() -> bool:
    return is_mcp_debug_enabled()


def _redact_header_value(name: str, value: str) -> str:
    lower = name.strip().lower()
    if lower in {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }:
        text = str(value or "")
        if lower == "authorization" and " " in text:
            scheme = text.split(" ", 1)[0]
            return f"{scheme} ***"
        return "***"
    return str(value)


def _sanitize_headers(headers: Any) -> dict[str, str]:
    try:
        items = headers.items()
    except Exception:
        return {}
    return {str(k): _redact_header_value(str(k), str(v)) for k, v in items}


class MCPUnavailableError(RuntimeError):
    """Raised when the required ``mcp`` SDK is not installed."""


def _import_mcp():
    try:
        import mcp  # noqa: F401
        from mcp import ClientSession, StdioServerParameters  # type: ignore
        from mcp.client.stdio import stdio_client  # type: ignore
        try:
            from mcp.client.streamable_http import streamable_http_client  # type: ignore
        except ImportError:
            from mcp.client.streamable_http import streamablehttp_client as streamable_http_client  # type: ignore
        try:
            from mcp.client.sse import sse_client  # type: ignore
        except ImportError:
            sse_client = None
    except ImportError as e:
        raise MCPUnavailableError(
            "MCP support requires the 'mcp' package. "
            "Current environment is missing a required dependency; run `uv sync`."
        ) from e
    return ClientSession, StdioServerParameters, stdio_client, streamable_http_client, sse_client


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
        if raw_name in required:
            fields[py_name] = (py_type, ...)
        else:
            fields[py_name] = (py_type | None, prop.get("default", None))

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
) -> MCPTool:
    """Wrap an MCP ``call_tool`` invocation as a lightweight ``MCPTool``."""
    args_schema = _schema_to_pydantic(schema, model_name=f"MCP_{server_name}_{tool_name}_Input")

    async def _call(_tool_name: str, kwargs: dict):
        try:
            result = await invoke(tool_name, kwargs)
        except Exception as e:
            return f"Error calling MCP tool {server_name}.{tool_name}: {type(e).__name__}: {e}"
        return _format_mcp_result(result)

    qualified_name = f"mcp__{server_name}__{tool_name}".lower()
    return MCPTool(
        name=qualified_name,
        description=f"[MCP:{server_name}] {description}".strip(),
        parameters=_schema_to_dict(args_schema) if args_schema else {},
        metadata={
            "required_permission": _PERMISSION_NETWORK,
            "mcp_server": server_name,
            "mcp_tool": tool_name,
            "args_schema": args_schema,
        },
        invoke=_call,
    )


def _schema_to_dict(model: type[BaseModel]) -> dict[str, Any]:
    """Extract JSON schema dict from a pydantic model."""
    try:
        return model.model_json_schema()
    except Exception:
        return {}


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
    tools: list[MCPTool] = field(default_factory=list)
    error: str | None = None


class MCPClientPool:
    """Manages MCP sessions across multiple transports.

    The pool keeps an ``AsyncExitStack`` so all subprocesses and
    transport / ``ClientSession`` async-context-managers are torn down together
    when ``aclose()`` is called.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._connections: list[MCPConnection] = []
        self._closed = False

    @property
    def connections(self) -> list[MCPConnection]:
        return list(self._connections)

    @staticmethod
    def _build_tools(*, config: MCPServerConfig, listed: Any, invoke) -> list[MCPTool]:
        tools: list[MCPTool] = []
        for spec in getattr(listed, "tools", []) or []:
            tool = _build_mcp_tool(
                server_name=config.name,
                tool_name=spec.name,
                description=getattr(spec, "description", "") or "",
                schema=getattr(spec, "inputSchema", None),
                invoke=invoke,
            )
            tools.append(tool)
        return tools

    @staticmethod
    @asynccontextmanager
    async def _httpx_client_factory(
        cfg: MCPServerConfig,
        *,
        headers: dict[str, Any] | None = None,
        auth: Any = None,
        timeout: Any = None,
        **_: Any,
    ):
        merged_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        merged_headers.update(cfg.headers)
        effective_timeout = timeout
        if effective_timeout is None and cfg.timeout is not None:
            if cfg.sse_read_timeout is not None:
                effective_timeout = httpx.Timeout(cfg.timeout, read=cfg.sse_read_timeout)
            else:
                effective_timeout = cfg.timeout
        event_hooks = None
        if _debug_enabled():
            async def _log_request(request: httpx.Request) -> None:
                logger.warning(
                    "MCP HTTP request server=%s transport=%s method=%s url=%s headers=%s",
                    cfg.name,
                    cfg.transport,
                    request.method,
                    request.url,
                    _sanitize_headers(request.headers),
                )

            async def _log_response(response: httpx.Response) -> None:
                req = response.request
                logger.warning(
                    "MCP HTTP response server=%s transport=%s status=%s method=%s url=%s",
                    cfg.name,
                    cfg.transport,
                    response.status_code,
                    getattr(req, "method", "?"),
                    getattr(req, "url", "?"),
                )

            event_hooks = {
                "request": [_log_request],
                "response": [_log_response],
            }
        async with httpx.AsyncClient(
            headers=merged_headers or None,
            auth=auth,
            timeout=effective_timeout,
            event_hooks=event_hooks,
        ) as client:
            yield client

    async def _connect_stdio(self, cfg: MCPServerConfig, sdk: tuple) -> list[MCPTool]:
        ClientSession, StdioServerParameters, stdio_client, _, _ = sdk
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=cfg.env or None,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        return self._build_tools(config=cfg, listed=listed, invoke=session.call_tool)

    async def _build_http_transport_kwargs(self, cfg: MCPServerConfig, sig: inspect.Signature) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        transport_mode = "sdk-default"
        if "http_client" in sig.parameters:
            transport_mode = "http_client"
            kwargs["http_client"] = await self._stack.enter_async_context(
                self._httpx_client_factory(cfg)
            )
        elif "httpx_client" in sig.parameters:
            transport_mode = "httpx_client"
            kwargs["httpx_client"] = await self._stack.enter_async_context(
                self._httpx_client_factory(cfg)
            )
        elif "httpx_client_factory" in sig.parameters:
            transport_mode = "httpx_client_factory"
            kwargs["httpx_client_factory"] = lambda **kw: self._httpx_client_factory(cfg, **kw)
        elif "headers" in sig.parameters and cfg.headers:
            transport_mode = "headers"
            kwargs["headers"] = cfg.headers
        if "timeout" in sig.parameters and cfg.timeout is not None:
            kwargs["timeout"] = cfg.timeout
        if "sse_read_timeout" in sig.parameters and cfg.sse_read_timeout is not None:
            kwargs["sse_read_timeout"] = cfg.sse_read_timeout
        if _debug_enabled():
            logger.warning(
                "MCP HTTP transport setup server=%s transport=%s mode=%s url=%s headers=%s timeout=%s sse_read_timeout=%s",
                cfg.name,
                cfg.transport,
                transport_mode,
                cfg.url,
                _sanitize_headers(cfg.headers),
                cfg.timeout,
                cfg.sse_read_timeout,
            )
        return kwargs

    async def _connect_streamable_http(self, cfg: MCPServerConfig, sdk: tuple) -> list[MCPTool]:
        ClientSession, _, _, streamable_http_client, _ = sdk
        if streamable_http_client is None:
            raise MCPUnavailableError("Installed MCP SDK does not provide streamable-http client support")
        sig = inspect.signature(streamable_http_client)
        kwargs = await self._build_http_transport_kwargs(cfg, sig)
        streams = await self._stack.enter_async_context(streamable_http_client(cfg.url, **kwargs))
        read, write = streams[0], streams[1]
        get_session_id = streams[2] if len(streams) > 2 and callable(streams[2]) else None
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        if _debug_enabled():
            logger.warning(
                "MCP streamable-http initialized server=%s session_id=%s",
                cfg.name,
                get_session_id() if get_session_id else None,
            )
        listed = await session.list_tools()
        if _debug_enabled():
            logger.warning(
                "MCP streamable-http listed tools server=%s session_id=%s tool_count=%s",
                cfg.name,
                get_session_id() if get_session_id else None,
                len(getattr(listed, "tools", []) or []),
            )

        async def _invoke_tool(name: str, arguments: dict[str, Any]) -> Any:
            if _debug_enabled():
                logger.warning(
                    "MCP streamable-http call server=%s tool=%s session_id=%s",
                    cfg.name,
                    name,
                    get_session_id() if get_session_id else None,
                )
            return await session.call_tool(name, arguments)

        return self._build_tools(config=cfg, listed=listed, invoke=_invoke_tool)

    async def _connect_sse(self, cfg: MCPServerConfig, sdk: tuple) -> list[MCPTool]:
        ClientSession, _, _, _, sse_client = sdk
        if sse_client is None:
            raise MCPUnavailableError("Installed MCP SDK does not provide SSE client support")
        sig = inspect.signature(sse_client)
        kwargs = await self._build_http_transport_kwargs(cfg, sig)
        streams = await self._stack.enter_async_context(sse_client(cfg.url, **kwargs))
        read, write = streams[0], streams[1]
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        return self._build_tools(config=cfg, listed=listed, invoke=session.call_tool)

    async def _connect_single(self, cfg: MCPServerConfig, sdk: tuple) -> list[MCPTool]:
        if cfg.transport == "stdio":
            return await self._connect_stdio(cfg, sdk)
        if cfg.transport == "streamable-http":
            return await self._connect_streamable_http(cfg, sdk)
        if cfg.transport == "sse":
            return await self._connect_sse(cfg, sdk)
        raise ValueError(f"unsupported MCP transport: {cfg.transport}")

    async def connect_all(self, configs: list[MCPServerConfig]) -> list[MCPConnection]:
        sdk: tuple | None = None

        for cfg in configs:
            if not cfg.enabled:
                self._connections.append(MCPConnection(config=cfg, error="disabled"))
                continue
            if sdk is None:
                sdk = _import_mcp()
            connection = MCPConnection(config=cfg)
            try:
                per_server_timeout = cfg.timeout if cfg.timeout is not None else 10.0
                connection.tools = await asyncio.wait_for(
                    self._connect_single(cfg, sdk),
                    timeout=per_server_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s' (%s) connection timed out after %.0fs",
                    cfg.name, cfg.transport, cfg.timeout if cfg.timeout else 10.0,
                )
                connection.error = f"TimeoutError: connection timed out after {cfg.timeout if cfg.timeout else 10.0:.0f}s"
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                logger.warning("MCP server '%s' (%s) failed to connect: %s", cfg.name, cfg.transport, e)
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
) -> tuple[MCPClientPool, list[MCPTool]]:
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

    tools: list[MCPTool] = []
    for conn in pool.connections:
        tools.extend(conn.tools)
    return pool, tools
