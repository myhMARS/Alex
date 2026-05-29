"""Centralized environment-backed configuration for Alex."""

import os
from typing import Iterable

from dotenv import load_dotenv

from alex.llm.base import LLMConfig

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
