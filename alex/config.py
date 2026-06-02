"""Centralized environment-backed configuration for Alex."""

import json as _json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from alex.llm.base import LLMConfig

# Search .env in:  CWD first, then ~/.alex/.env, then default dotenv search
_load_paths = [Path.cwd() / ".env", Path.home() / ".alex" / ".env"]
_loaded = False
for _p in _load_paths:
    if _p.exists():
        load_dotenv(dotenv_path=str(_p))
        _loaded = True
        break
if not _loaded:
    load_dotenv()

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}

DEFAULT_CONFIG = LLMConfig(
    provider="deepseek",
    base_url="https://api.deepseek.com",
    model="deepseek-chat",
)


def get_llm_config() -> LLMConfig:
    """Build LLMConfig from environment variables loaded from .env file.

    Environment variables:
        ALEX_PROVIDER  — LLM provider name (default: deepseek)
        ALEX_API_KEY   — API key for the provider
        ALEX_BASE_URL  — Base URL for the provider API
        ALEX_MODEL     — Model name (default: deepseek-chat)
        ALEX_MAX_TOKENS— Max tokens (default: 4096)
        ALEX_TEMPERATURE— Temperature (default: 0.0)
    """
    provider = get_env_str("ALEX_PROVIDER", DEFAULT_CONFIG.provider)
    api_key = get_env_str("ALEX_API_KEY", "")
    base_url = get_env_str("ALEX_BASE_URL", DEFAULT_CONFIG.base_url)
    model = get_env_str("ALEX_MODEL", DEFAULT_CONFIG.model)
    max_tokens = get_env_int("ALEX_MAX_TOKENS", DEFAULT_CONFIG.max_tokens)
    temperature = get_env_float("ALEX_TEMPERATURE", DEFAULT_CONFIG.temperature)

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def get_env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def get_env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value


def get_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value


def get_env_csv_set(name: str) -> set[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set()
    return {tok.strip().lower() for tok in raw.split(",") if tok.strip()}


def is_tui_markdown_enabled_by_default() -> bool:
    return get_env_bool("ALEX_TUI_MARKDOWN", default=True)


def is_cron_debug_enabled() -> bool:
    return get_env_bool("ALEX_CRON_DEBUG", default=False)


def is_mcp_debug_enabled() -> bool:
    return get_env_bool("ALEX_MCP_DEBUG", default=False)


def get_allowed_permissions(defaults: Iterable[str]) -> set[str]:
    values = get_env_csv_set("ALEX_TOOL_PERMISSIONS")
    return values or set(defaults)


def get_denied_permissions() -> set[str]:
    return get_env_csv_set("ALEX_TOOL_DENY")


def get_log_max_bytes(default: int) -> int:
    value = get_env_int("ALEX_LOG_MAX_BYTES", default)
    return value if value > 0 else default


def get_log_backup_count(default: int) -> int:
    value = get_env_int("ALEX_LOG_BACKUP_COUNT", default)
    return value if value > 0 else default


# ── MCP configuration (pure config, no MCP SDK dependency) ───────────────────

MCP_CONFIG_PATH: Path = Path.home() / ".alex" / "mcp.json"


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server — pure data, no SDK dependency."""

    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    sse_read_timeout: float | None = None
    enabled: bool = True


def _normalize_mcp_transport(raw: Any, *, has_command: bool, has_url: bool) -> str | None:
    value = str(raw or "").strip().lower().replace("_", "-")
    if not value:
        if has_url:
            return "streamable-http"
        if has_command:
            return "stdio"
        return None
    aliases = {
        "stdio": "stdio",
        "http": "streamable-http",
        "streamable-http": "streamable-http",
        "streamablehttp": "streamable-http",
        "sse": "sse",
        "http-sse": "sse",
    }
    return aliases.get(value)


def load_mcp_config(path: Path | None = None) -> list[MCPServerConfig]:
    """Parse ``~/.alex/mcp.json`` into a list of server configs.

    Returns an empty list when the file does not exist; raises
    :class:`ValueError` for malformed payloads.

    This is a pure config loader — no MCP SDK dependency.  It lives in
    ``alex.config`` so the TUI can read MCP server status without
    importing from the ``alex.mcp`` module.
    """
    target = path or MCP_CONFIG_PATH
    if not target.exists():
        return []
    try:
        with open(target, encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        raise ValueError(f"failed to parse {target}: {e}") from e

    raw_servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(raw_servers, dict):
        return []

    configs: list[MCPServerConfig] = []
    for name, payload in raw_servers.items():
        if not isinstance(payload, dict):
            continue
        command = str(payload.get("command", "")).strip()
        url = str(payload.get("url", "")).strip()
        transport = _normalize_mcp_transport(
            payload.get("transport"),
            has_command=bool(command),
            has_url=bool(url),
        )
        if not transport:
            continue
        if transport == "stdio" and not command:
            continue
        if transport in {"streamable-http", "sse"} and not url:
            continue
        args = payload.get("args", [])
        if not isinstance(args, list):
            args = []
        env = payload.get("env", {})
        if not isinstance(env, dict):
            env = {}
        headers = payload.get("headers", {})
        if not isinstance(headers, dict):
            headers = {}
        enabled = payload.get("disabled") is not True
        timeout = payload.get("timeout")
        sse_read_timeout = payload.get("sse_read_timeout")
        configs.append(MCPServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=[str(a) for a in args],
            env={str(k): str(v) for k, v in env.items()},
            url=url,
            headers={str(k): str(v) for k, v in headers.items()},
            timeout=float(timeout) if isinstance(timeout, (int, float)) else None,
            sse_read_timeout=float(sse_read_timeout) if isinstance(sse_read_timeout, (int, float)) else None,
            enabled=enabled,
        ))
    return configs
