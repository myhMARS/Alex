"""Structured JSON completion — provider-agnostic factory for reliable JSON output.

Handles provider-specific quirks:
- DeepSeek: disables thinking mode via extra_body
- OpenAI: uses response_format natively
- Anthropic: uses response_format (supported since Claude 3.5)
"""

from __future__ import annotations

from openai import AsyncOpenAI


async def create_json_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 8192,
    temperature: float = 0,
) -> str:
    """Call LLM with JSON mode and return the content string.

    Automatically handles provider-specific parameters (e.g., disabling
    DeepSeek thinking mode) to ensure reliable JSON output.

    Args:
        messages: List of {"role": ..., "content": ...} dicts.
        max_tokens: Maximum tokens for the response.
        temperature: Sampling temperature.

    Returns:
        The JSON string from the model's response content.
    """
    from alex.config import get_llm_config  # lazy import to avoid circular

    config = get_llm_config()
    client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

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

    try:
        response = await client.chat.completions.create(**create_kwargs)
        return response.choices[0].message.content or ""
    finally:
        await client.close()
