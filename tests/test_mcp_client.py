"""Tests for the MCP client adapter — uses local stubs, no live servers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from alex.tools import mcp_client
from alex.tools.mcp_client import (
    MCPClientPool,
    MCPServerConfig,
    _format_mcp_result,
    _schema_to_pydantic,
    load_mcp_config,
    load_mcp_tools_from_config,
)
from alex.tools.permissions import PERMISSION_NETWORK, required_permission


class TestLoadMCPConfig:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert load_mcp_config(tmp_path / "missing.json") == []

    def test_parses_servers(self, tmp_path: Path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "fs": {
                    "command": "uvx",
                    "args": ["mcp-server-filesystem", "/tmp"],
                    "env": {"FOO": "bar"},
                },
                "memory": {
                    "url": "http://localhost:8000/v1/mcp/memory",
                    "headers": {"Authorization": "Bearer token"},
                    "timeout": 12,
                },
                "off": {
                    "command": "noop",
                    "disabled": True,
                },
            },
        }), encoding="utf-8")
        configs = load_mcp_config(path)
        names = {c.name for c in configs}
        assert names == {"fs", "memory", "off"}
        fs = next(c for c in configs if c.name == "fs")
        assert fs.transport == "stdio"
        assert fs.command == "uvx"
        assert fs.args == ["mcp-server-filesystem", "/tmp"]
        assert fs.env == {"FOO": "bar"}
        assert fs.enabled is True
        memory = next(c for c in configs if c.name == "memory")
        assert memory.transport == "streamable-http"
        assert memory.url == "http://localhost:8000/v1/mcp/memory"
        assert memory.headers == {"Authorization": "Bearer token"}
        assert memory.timeout == 12.0
        off = next(c for c in configs if c.name == "off")
        assert off.enabled is False

    def test_parses_explicit_sse_transport(self, tmp_path: Path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "legacy-http": {
                    "transport": "sse",
                    "url": "http://localhost:8123/sse",
                    "headers": {"X-User": "alice"},
                    "sse_read_timeout": 99,
                },
            },
        }), encoding="utf-8")
        configs = load_mcp_config(path)
        assert len(configs) == 1
        cfg = configs[0]
        assert cfg.transport == "sse"
        assert cfg.url == "http://localhost:8123/sse"
        assert cfg.headers == {"X-User": "alice"}
        assert cfg.sse_read_timeout == 99.0

    def test_skips_invalid_entries(self, tmp_path: Path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "ok": {"command": "uvx"},
                "missing_command": {"args": []},
                "missing_url": {"transport": "streamable-http"},
                "bogus": "not a dict",
            },
        }), encoding="utf-8")
        configs = load_mcp_config(path)
        assert [c.name for c in configs] == ["ok"]

    def test_malformed_json_raises(self, tmp_path: Path):
        path = tmp_path / "mcp.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_mcp_config(path)


class TestSchemaConversion:
    def test_simple_object_schema(self):
        model = _schema_to_pydantic({
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name"],
        }, model_name="X")
        fields = model.model_fields
        assert "name" in fields
        assert "count" in fields

    def test_unknown_schema_falls_back(self):
        model = _schema_to_pydantic(None, model_name="Empty")
        # Should still be a valid pydantic model with no fields.
        instance = model()
        assert instance.model_dump() == {}


class TestFormatResult:
    def test_text_content(self):
        class _Item:
            text = "hello"

        class _Result:
            content = [_Item()]

        assert _format_mcp_result(_Result()) == "hello"

    def test_binary_content(self):
        class _Item:
            text = None
            data = b"\x00" * 16

        class _Result:
            content = [_Item()]

        out = _format_mcp_result(_Result())
        assert "binary" in out


class TestLoadFromConfig:
    @pytest.mark.asyncio
    async def test_returns_empty_pool_when_no_config(self, tmp_path: Path):
        pool, tools = await load_mcp_tools_from_config(config_path=tmp_path / "missing.json")
        assert isinstance(pool, MCPClientPool)
        assert tools == []
        await pool.aclose()

    @pytest.mark.asyncio
    async def test_unavailable_sdk_propagates_when_servers_configured(
        self, tmp_path: Path, monkeypatch
    ):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"x": {"command": "uvx"}},
        }), encoding="utf-8")

        def _raise():
            raise mcp_client.MCPUnavailableError("missing")

        monkeypatch.setattr(mcp_client, "_import_mcp", _raise)
        with pytest.raises(mcp_client.MCPUnavailableError):
            await load_mcp_tools_from_config(config_path=path)

    @pytest.mark.asyncio
    async def test_pool_records_disabled_servers(self, monkeypatch):
        sdk_called = []

        def _fake_import():
            sdk_called.append(True)
            raise AssertionError("sdk should not be imported for disabled-only configs")

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)

        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(name="off", command="noop", enabled=False),
        ])
        assert len(results) == 1
        assert results[0].error == "disabled"
        assert results[0].tools == []
        assert sdk_called == []
        await pool.aclose()

    @pytest.mark.asyncio
    async def test_connect_streamable_http_uses_url_transport(self, monkeypatch):
        captured: dict[str, object] = {}

        class _Session:
            def __init__(self, read, write):
                captured["session_streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                captured["initialized"] = True

            async def list_tools(self):
                class _Spec:
                    name = "lookup"
                    description = "look things up"
                    inputSchema = {"type": "object", "properties": {"q": {"type": "string"}}}

                class _Listed:
                    tools = [_Spec()]

                return _Listed()

            async def call_tool(self, *a, **kw):
                class _Item:
                    text = "ok"

                class _Result:
                    content = [_Item()]

                return _Result()

        def _streamable_http_client(url, httpx_client_factory=None, **kwargs):
            captured["url"] = url
            captured["kwargs"] = {"httpx_client_factory": httpx_client_factory, **kwargs}

            class _CM:
                async def __aenter__(self):
                    return ("read-http", "write-http", "meta")

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _CM()

        def _fake_import():
            class _Params:
                def __init__(self, **kw): self.kw = kw

            def _stdio(_params):
                raise AssertionError("stdio transport should not be used")

            return _Session, _Params, _stdio, _streamable_http_client, None

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)

        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(
                name="memory-bear",
                transport="streamable-http",
                url="http://localhost:8000/v1/mcp/memory",
                headers={"Authorization": "Bearer token"},
                timeout=15,
                sse_read_timeout=45,
            ),
        ])
        assert len(results) == 1
        assert results[0].error is None
        assert len(results[0].tools) == 1
        assert captured["url"] == "http://localhost:8000/v1/mcp/memory"
        kwargs = captured["kwargs"]
        assert "httpx_client_factory" in kwargs
        assert captured["initialized"] is True
        await pool.aclose()

    @pytest.mark.asyncio
    async def test_connect_streamable_http_uses_httpx_client_when_supported(self, monkeypatch):
        captured: dict[str, object] = {}

        class _Session:
            def __init__(self, read, write):
                captured["session_streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                captured["initialized"] = True

            async def list_tools(self):
                class _Listed:
                    tools = []

                return _Listed()

            async def call_tool(self, *a, **kw):
                raise AssertionError("not used")

        def _streamable_http_client(url, httpx_client=None, **kwargs):
            captured["url"] = url
            captured["kwargs"] = {"httpx_client": httpx_client, **kwargs}
            captured["auth_header"] = None if httpx_client is None else httpx_client.headers.get("Authorization")

            class _CM:
                async def __aenter__(self):
                    return ("read-http", "write-http", "meta")

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _CM()

        def _fake_import():
            class _Params:
                def __init__(self, **kw): self.kw = kw

            def _stdio(_params):
                raise AssertionError("stdio transport should not be used")

            return _Session, _Params, _stdio, _streamable_http_client, None

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)

        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(
                name="memory-bear",
                transport="streamable-http",
                url="http://localhost:8000/v1/mcp/memory",
                headers={"Authorization": "Bearer token"},
                timeout=15,
            ),
        ])
        assert len(results) == 1
        assert results[0].error is None
        assert captured["url"] == "http://localhost:8000/v1/mcp/memory"
        kwargs = captured["kwargs"]
        assert isinstance(kwargs["httpx_client"], httpx.AsyncClient)
        assert captured["auth_header"] == "Bearer token"
        assert captured["initialized"] is True
        await pool.aclose()

    @pytest.mark.asyncio
    async def test_connect_streamable_http_uses_http_client_when_supported(self, monkeypatch):
        captured: dict[str, object] = {}

        class _Session:
            def __init__(self, read, write):
                captured["session_streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                captured["initialized"] = True

            async def list_tools(self):
                class _Listed:
                    tools = []

                return _Listed()

            async def call_tool(self, *a, **kw):
                raise AssertionError("not used")

        def _streamable_http_client(url, http_client=None, **kwargs):
            captured["url"] = url
            captured["kwargs"] = {"http_client": http_client, **kwargs}
            captured["auth_header"] = None if http_client is None else http_client.headers.get("Authorization")

            class _CM:
                async def __aenter__(self):
                    return ("read-http", "write-http", "meta")

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _CM()

        def _fake_import():
            class _Params:
                def __init__(self, **kw): self.kw = kw

            def _stdio(_params):
                raise AssertionError("stdio transport should not be used")

            return _Session, _Params, _stdio, _streamable_http_client, None

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)

        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(
                name="memory-bear",
                transport="streamable-http",
                url="http://localhost:8000/v1/mcp/memory",
                headers={"Authorization": "Bearer token"},
                timeout=15,
            ),
        ])
        assert len(results) == 1
        assert results[0].error is None
        assert captured["url"] == "http://localhost:8000/v1/mcp/memory"
        kwargs = captured["kwargs"]
        assert isinstance(kwargs["http_client"], httpx.AsyncClient)
        assert captured["auth_header"] == "Bearer token"
        assert captured["initialized"] is True
        await pool.aclose()

    @pytest.mark.asyncio
    async def test_httpx_factory_merges_headers_and_timeout(self):
        cfg = MCPServerConfig(
            name="memory-bear",
            transport="streamable-http",
            url="http://localhost:8000/v1/mcp/memory",
            headers={"Authorization": "Bearer token", "X-End-User-Other-Id": "Eternity"},
            timeout=12,
            sse_read_timeout=50,
        )
        async with MCPClientPool._httpx_client_factory(
            cfg,
            headers={"accept": "application/json"},
        ) as client:
            assert isinstance(client, httpx.AsyncClient)
            assert client.headers["Authorization"] == "Bearer token"
            assert client.headers["X-End-User-Other-Id"] == "Eternity"
            assert client.headers["accept"] == "application/json"

    @pytest.mark.asyncio
    async def test_connect_sse_uses_legacy_transport(self, monkeypatch):
        captured: dict[str, object] = {}

        class _Session:
            def __init__(self, read, write):
                captured["session_streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                captured["initialized"] = True

            async def list_tools(self):
                class _Listed:
                    tools = []

                return _Listed()

            async def call_tool(self, *a, **kw):
                raise AssertionError("not used")

        def _sse_client(url, httpx_client_factory=None, **kwargs):
            captured["url"] = url
            captured["kwargs"] = {"httpx_client_factory": httpx_client_factory, **kwargs}

            class _CM:
                async def __aenter__(self):
                    return ("read-sse", "write-sse")

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _CM()

        def _fake_import():
            class _Params:
                def __init__(self, **kw): self.kw = kw

            def _stdio(_params):
                raise AssertionError("stdio transport should not be used")

            async def _streamable(*a, **kw):
                raise AssertionError("streamable-http transport should not be used")

            return _Session, _Params, _stdio, _streamable, _sse_client

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)
        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(
                name="legacy",
                transport="sse",
                url="http://localhost:8123/sse",
                headers={"Authorization": "Bearer token"},
            ),
        ])
        assert len(results) == 1
        assert results[0].error is None
        assert captured["url"] == "http://localhost:8123/sse"
        assert "httpx_client_factory" in captured["kwargs"]
        assert captured["initialized"] is True
        await pool.aclose()

    @pytest.mark.asyncio
    async def test_connect_sse_uses_httpx_client_when_supported(self, monkeypatch):
        captured: dict[str, object] = {}

        class _Session:
            def __init__(self, read, write):
                captured["session_streams"] = (read, write)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def initialize(self):
                captured["initialized"] = True

            async def list_tools(self):
                class _Listed:
                    tools = []

                return _Listed()

            async def call_tool(self, *a, **kw):
                raise AssertionError("not used")

        def _sse_client(url, httpx_client=None, **kwargs):
            captured["url"] = url
            captured["kwargs"] = {"httpx_client": httpx_client, **kwargs}
            captured["auth_header"] = None if httpx_client is None else httpx_client.headers.get("Authorization")

            class _CM:
                async def __aenter__(self):
                    return ("read-sse", "write-sse")

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _CM()

        def _fake_import():
            class _Params:
                def __init__(self, **kw): self.kw = kw

            def _stdio(_params):
                raise AssertionError("stdio transport should not be used")

            async def _streamable(*a, **kw):
                raise AssertionError("streamable-http transport should not be used")

            return _Session, _Params, _stdio, _streamable, _sse_client

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)
        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(
                name="legacy",
                transport="sse",
                url="http://localhost:8123/sse",
                headers={"Authorization": "Bearer token"},
            ),
        ])
        assert len(results) == 1
        assert results[0].error is None
        assert captured["url"] == "http://localhost:8123/sse"
        assert isinstance(captured["kwargs"]["httpx_client"], httpx.AsyncClient)
        assert captured["auth_header"] == "Bearer token"
        assert captured["initialized"] is True
        await pool.aclose()


class TestBuildMCPTool:
    @pytest.mark.asyncio
    async def test_tool_invokes_callback(self):
        captured: list[tuple[str, dict]] = []

        async def _invoke(tool_name: str, kwargs: dict):
            captured.append((tool_name, kwargs))

            class _Item:
                text = f"result for {kwargs}"

            class _Result:
                content = [_Item()]

            return _Result()

        tool = mcp_client._build_mcp_tool(
            server_name="srv",
            tool_name="echo",
            description="echo back",
            schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            invoke=_invoke,
        )
        assert tool.name.startswith("mcp__srv__echo")
        assert required_permission(tool) == PERMISSION_NETWORK
        result = await tool.ainvoke({"text": "hi"})
        assert "result for" in result
        assert captured == [("echo", {"text": "hi"})]
