"""API configuration loading — supports multi-provider configuration."""

import os
from pathlib import Path

from alex.llm.base import LLMConfig

DEFAULT_CONFIG = LLMConfig(
    provider="deepseek",
    base_url="https://api.deepseek.com",
    model="deepseek-chat",
)


def load_config(apikey_path: str | None = None) -> dict[str, str]:
    """Load raw configuration from .apikey file.

    Format:
        provider:deepseek
        baseurl:https://api.deepseek.com
        apikey:sk-xxx
        models:deepseek-chat,deepseek-reasoner
    """
    if apikey_path is None:
        apikey_path = Path(__file__).parent.parent / ".apikey"

    config: dict[str, str] = {}
    try:
        with open(apikey_path) as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    key, value = line.split(":", 1)
                    config[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return config


def get_llm_config() -> LLMConfig:
    """Build LLMConfig from .apikey file and environment variables."""
    raw = load_config()

    provider = os.environ.get("ALEX_PROVIDER") or raw.get("provider", DEFAULT_CONFIG.provider)
    api_key = os.environ.get("ALEX_API_KEY") or raw.get("apikey", "")
    base_url = os.environ.get("ALEX_BASE_URL") or raw.get("baseurl", DEFAULT_CONFIG.base_url)
    models_str = raw.get("models", DEFAULT_CONFIG.model)
    model = models_str.split(",")[0].strip()

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
