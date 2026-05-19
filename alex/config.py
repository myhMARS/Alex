"""API configuration loaded from .env file via python-dotenv."""

import os

from dotenv import load_dotenv

from alex.llm.base import LLMConfig

load_dotenv()

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
    provider = os.environ.get("ALEX_PROVIDER", DEFAULT_CONFIG.provider)
    api_key = os.environ.get("ALEX_API_KEY", "")
    base_url = os.environ.get("ALEX_BASE_URL", DEFAULT_CONFIG.base_url)
    model = os.environ.get("ALEX_MODEL", DEFAULT_CONFIG.model)
    max_tokens = int(os.environ.get("ALEX_MAX_TOKENS", DEFAULT_CONFIG.max_tokens))
    temperature = float(os.environ.get("ALEX_TEMPERATURE", DEFAULT_CONFIG.temperature))

    return LLMConfig(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )