"""Structured JSON completion — provider-agnostic factory for reliable JSON output.

Handles provider-specific quirks:
- DeepSeek: disables thinking mode via extra_body
- OpenAI: uses response_format natively
- Anthropic: uses response_format (supported since Claude 3.5)

The underlying ``AsyncOpenAI`` client is cached per config so that
reflection and skill-merging (which may fire every few turns) reuse the
same connection pool rather than paying a TCP+TLS handshake each time.
"""

from __future__ import annotations

import hashlib
from threading import Lock as ThreadLock

from openai import AsyncOpenAI

from alex.llm.base import LLMConfig


# ── cached client ────────────────────────────────────────────────────────

_client: AsyncOpenAI | None = None
_client_digest: str = ""
_client_lock = ThreadLock()


def _config_digest(config: LLMConfig) -> str:
    """Stable fingerprint of the config fields that affect the client."""
    key = f"{config.api_key}|{config.base_url}|{config.provider}"
    return hashlib.sha256(key.encode()).hexdigest()


def _get_client(config: LLMConfig) -> AsyncOpenAI:
    """Return a cached AsyncOpenAI for *config*, reusing the connection pool.

    Thread-safe lazy init.  When *config* differs from the cached instance
    the old client is closed and a fresh one is built.
    """
    global _client, _client_digest
    digest = _config_digest(config)
    with _client_lock:
        if _client is not None and digest == _client_digest:
            return _client
        # Config changed (or first call) — tear down old, build new.
        if _client is not None:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(_client.close())
        _client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
        _client_digest = digest
        return _client


# ── public API ───────────────────────────────────────────────────────────

async def create_json_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 8192,
    temperature: float = 0,
    config: LLMConfig | None = None,
) -> str:
    """Call LLM with JSON mode and return the content string.

    Automatically handles provider-specific parameters (e.g., disabling
    DeepSeek thinking mode) to ensure reliable JSON output.

    Args:
        messages: List of {"role": ..., "content": ...} dicts.
        max_tokens: Maximum tokens for the response.
        temperature: Sampling temperature.
        config: Optional LLMConfig. When None, resolved from environment.

    Returns:
        The JSON string from the model's response content.
    """
    if config is None:
        from alex.config import get_llm_config  # lazy import to avoid circular
        config = get_llm_config()

    client = _get_client(config)

    create_kwargs: dict = {
        "model": config.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    # Provider-specific adjustments
    if config.provider == "deepseek":
        create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    response = await client.chat.completions.create(**create_kwargs)
    return response.choices[0].message.content or ""
