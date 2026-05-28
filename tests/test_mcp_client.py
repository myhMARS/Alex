"""Tests for the MCP client adapter — uses local stubs, no live servers."""

from __future__ import annotations

import json
from pathlib import Path

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
                "off": {
                    "command": "noop",
                    "disabled": True,
                },
            },
        }), encoding="utf-8")
        configs = load_mcp_config(path)
        names = {c.name for c in configs}
        assert names == {"fs", "off"}
        fs = next(c for c in configs if c.name == "fs")
        assert fs.command == "uvx"
        assert fs.args == ["mcp-server-filesystem", "/tmp"]
        assert fs.env == {"FOO": "bar"}
        assert fs.enabled is True
        off = next(c for c in configs if c.name == "off")
        assert off.enabled is False

    def test_skips_invalid_entries(self, tmp_path: Path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "ok": {"command": "uvx"},
                "missing_command": {"args": []},
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
        # Even with sdk available we should skip disabled entries.
        sdk_called = []

        def _fake_import():
            sdk_called.append(True)

            class _Session:
                async def initialize(self): ...
                async def list_tools(self): ...
                async def call_tool(self, *a, **kw): ...

            class _Params:
                def __init__(self, **kw): self.kw = kw

            class _Stdio:
                pass

            return _Session, _Params, _Stdio

        monkeypatch.setattr(mcp_client, "_import_mcp", _fake_import)

        pool = MCPClientPool()
        results = await pool.connect_all([
            MCPServerConfig(name="off", command="noop", enabled=False),
        ])
        assert len(results) == 1
        assert results[0].error == "disabled"
        assert results[0].tools == []
        # The fake import is consulted but no live session is opened for
        # disabled servers.
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
