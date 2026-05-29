from __future__ import annotations

from alex.config import (
    DEFAULT_CONFIG,
    get_allowed_permissions,
    get_denied_permissions,
    get_env_bool,
    get_env_csv_set,
    get_env_float,
    get_env_int,
    get_llm_config,
    get_log_backup_count,
    get_log_max_bytes,
    is_cron_debug_enabled,
    is_mcp_debug_enabled,
    is_tui_markdown_enabled_by_default,
)


def test_get_env_bool(monkeypatch):
    monkeypatch.setenv("ALEX_BOOL_FLAG", "true")
    assert get_env_bool("ALEX_BOOL_FLAG") is True
    monkeypatch.setenv("ALEX_BOOL_FLAG", "0")
    assert get_env_bool("ALEX_BOOL_FLAG", default=True) is False
    monkeypatch.setenv("ALEX_BOOL_FLAG", "weird")
    assert get_env_bool("ALEX_BOOL_FLAG", default=True) is True


def test_get_env_int_and_float_fallback(monkeypatch):
    monkeypatch.setenv("ALEX_INT_VALUE", "12")
    monkeypatch.setenv("ALEX_FLOAT_VALUE", "0.75")
    assert get_env_int("ALEX_INT_VALUE", 3) == 12
    assert get_env_float("ALEX_FLOAT_VALUE", 0.1) == 0.75

    monkeypatch.setenv("ALEX_INT_VALUE", "bad")
    monkeypatch.setenv("ALEX_FLOAT_VALUE", "bad")
    assert get_env_int("ALEX_INT_VALUE", 3) == 3
    assert get_env_float("ALEX_FLOAT_VALUE", 0.1) == 0.1


def test_get_env_csv_set(monkeypatch):
    monkeypatch.setenv("ALEX_LIST", " read, write ,, Shell ")
    assert get_env_csv_set("ALEX_LIST") == {"read", "write", "shell"}


def test_feature_flags(monkeypatch):
    monkeypatch.setenv("ALEX_TUI_MARKDOWN", "0")
    monkeypatch.setenv("ALEX_CRON_DEBUG", "1")
    monkeypatch.setenv("ALEX_MCP_DEBUG", "yes")
    assert is_tui_markdown_enabled_by_default() is False
    assert is_cron_debug_enabled() is True
    assert is_mcp_debug_enabled() is True


def test_permission_sets(monkeypatch):
    monkeypatch.setenv("ALEX_TOOL_PERMISSIONS", "read,write")
    monkeypatch.setenv("ALEX_TOOL_DENY", "write,shell")
    assert get_allowed_permissions({"read", "network"}) == {"read", "write"}
    assert get_denied_permissions() == {"write", "shell"}


def test_log_limits(monkeypatch):
    monkeypatch.setenv("ALEX_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("ALEX_LOG_BACKUP_COUNT", "7")
    assert get_log_max_bytes(1) == 1024
    assert get_log_backup_count(1) == 7


def test_get_llm_config_uses_centralized_helpers(monkeypatch):
    monkeypatch.delenv("ALEX_PROVIDER", raising=False)
    monkeypatch.delenv("ALEX_API_KEY", raising=False)
    monkeypatch.delenv("ALEX_BASE_URL", raising=False)
    monkeypatch.delenv("ALEX_MODEL", raising=False)
    monkeypatch.delenv("ALEX_MAX_TOKENS", raising=False)
    monkeypatch.delenv("ALEX_TEMPERATURE", raising=False)

    default = get_llm_config()
    assert default.provider == DEFAULT_CONFIG.provider
    assert default.base_url == DEFAULT_CONFIG.base_url

    monkeypatch.setenv("ALEX_PROVIDER", "openai")
    monkeypatch.setenv("ALEX_API_KEY", "sk-test")
    monkeypatch.setenv("ALEX_BASE_URL", "https://example.test")
    monkeypatch.setenv("ALEX_MODEL", "gpt-test")
    monkeypatch.setenv("ALEX_MAX_TOKENS", "1234")
    monkeypatch.setenv("ALEX_TEMPERATURE", "0.25")

    cfg = get_llm_config()
    assert cfg.provider == "openai"
    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://example.test"
    assert cfg.model == "gpt-test"
    assert cfg.max_tokens == 1234
    assert cfg.temperature == 0.25
