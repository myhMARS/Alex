"""Unified chat client — wraps AsyncOpenAI for all LLM providers.

DeepSeek, OpenAI, and Anthropic (via compatible proxy) all speak the OpenAI
Chat Completions API.  This module replaces the langchain provider adapters
(ChatDeepSeek / ChatOpenAI / ChatAnthropic) with a single client.

Streaming::
    async for event in client.stream_chat(messages, tools):
        if isinstance(event, ContentDelta): ...
        elif isinstance(event, ToolCallRequest): ...
        elif isinstance(event, StreamEnd): ...

JSON mode::
    text = await client.json_completion(messages)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from alex.llm.base import LLMConfig

logger = logging.getLogger(__name__)


def _import_async_openai() -> type[AsyncOpenAI]:
    """Lazy-import AsyncOpenAI — defers the ~0.7s openai package load."""
    from openai import AsyncOpenAI as _AsyncOpenAI
    return _AsyncOpenAI

# ── stream event types ────────────────────────────────────────────────────


@dataclass
class ContentDelta:
    """A text token from the LLM stream."""
    content: str


@dataclass
class ThinkingDelta:
    """A reasoning / thinking token (DeepSeek thinking mode)."""
    content: str


@dataclass
class ToolCallRequest:
    """The LLM has decided to call one or more tools.

    ``tool_calls`` is a list of dicts in OpenAI tool-call format:
        {"id": ..., "type": "function", "function": {"name": ..., "arguments": ...}}
    """
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamEnd:
    """End of a streaming turn — carries the complete response metadata."""
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] | None = None
    finish_reason: str = ""


# Type for any stream event
StreamEvent = ContentDelta | ThinkingDelta | ToolCallRequest | StreamEnd


# ── tool accumulation helpers ─────────────────────────────────────────────


def _accumulate_tool_calls(
    existing: dict[int, dict[str, Any]],
    delta_tool_calls: list[Any],
) -> dict[int, dict[str, Any]]:
    """Merge streaming tool-call deltas into accumulated tool-call state.

    OpenAI streams tool calls as incremental deltas indexed by position.
    Each chunk may carry a partial ``function.name`` or ``function.arguments``.
    """
    for tc in delta_tool_calls:
        idx = getattr(tc, "index", None)
        if idx is None:
            continue
        idx = int(idx)
        if idx not in existing:
            existing[idx] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        entry = existing[idx]
        tc_id = getattr(tc, "id", None)
        if tc_id:
            entry["id"] = str(tc_id)
        fn = getattr(tc, "function", None)
        if fn is not None:
            name = getattr(fn, "name", None)
            if name:
                entry["function"]["name"] = str(name)
            args = getattr(fn, "arguments", None)
            if args:
                entry["function"]["arguments"] += str(args)
    return existing


def _finalize_tool_calls(acc: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return accumulated tool calls sorted by index."""
    return [acc[i] for i in sorted(acc)]


# ── ChatClient ────────────────────────────────────────────────────────────


class ChatClient:
    """Unified async chat client for OpenAI-compatible LLM APIs.

    Handles DeepSeek reasoning_content, tool-call streaming deltas, and
    JSON-mode completions for reflection / skill merging.

    Usage::

        client = ChatClient(config)
        async for event in client.stream_chat(messages, tools):
            ...
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        AsyncOpenAI = _import_async_openai()
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=120.0,
        )
        self._model = config.model

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def model(self) -> str:
        return self._model

    # ── streaming chat ──────────────────────────────────────────────────

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat completion, yielding typed events.

        Parameters
        ----------
        messages:
            List of ``{"role": ..., "content": ...}`` dicts.
        tools:
            Optional list of OpenAI-format tool schemas.
        system_prompt:
            Optional system prompt (prepended to messages as a system role).
        max_tokens:
            Override the config default.
        temperature:
            Override the config default.
        """
        api_messages: list[dict[str, Any]] = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "stream": True,
            "max_tokens": max_tokens if max_tokens is not None else self._config.max_tokens,
            "temperature": temperature if temperature is not None else self._config.temperature,
        }
        if tools:
            kwargs["tools"] = tools

        # DeepSeek-specific: enable thinking mode via extra_body
        if self._config.provider == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        collected_content = ""
        collected_thinking = ""
        tool_call_acc: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage: dict[str, int] | None = None

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            logger.warning("LLM stream creation failed: %s", e)
            yield StreamEnd(content=f"Error: {type(e).__name__}: {e}")
            return

        async for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue

            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            # ── text content ──────────────────────────────────────────
            content = getattr(delta, "content", None)
            if content:
                collected_content += str(content)
                yield ContentDelta(content=str(content))

            # ── reasoning / thinking (DeepSeek) ───────────────────────
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                collected_thinking += str(reasoning)
                yield ThinkingDelta(content=str(reasoning))

            # Also check additional_kwargs for reasoning (some proxies)
            ak = getattr(delta, "additional_kwargs", None) or {}
            if isinstance(ak, dict):
                ak_reasoning = ak.get("reasoning_content", "")
                if ak_reasoning and not reasoning:
                    collected_thinking += str(ak_reasoning)

            # ── tool calls ────────────────────────────────────────────
            tc_delta = getattr(delta, "tool_calls", None)
            if tc_delta:
                _accumulate_tool_calls(tool_call_acc, tc_delta)

            # ── finish reason ─────────────────────────────────────────
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = str(fr)

            # ── usage (may appear in final chunk) ─────────────────────
            u = getattr(chunk, "usage", None)
            if u is not None:
                usage = {
                    "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(u, "total_tokens", 0) or 0,
                }

        # ── emit final event ──────────────────────────────────────────────
        tool_calls = _finalize_tool_calls(tool_call_acc)
        if tool_calls:
            yield ToolCallRequest(tool_calls=tool_calls)

        yield StreamEnd(
            content=collected_content,
            thinking=collected_thinking,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )

    # ── JSON-mode completion ─────────────────────────────────────────────

    async def json_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        config: LLMConfig | None = None,
    ) -> str:
        """Call the LLM with JSON mode enabled, return the content string.

        Automatically disables DeepSeek thinking mode for JSON output
        (reasoning can interfere with structured output parsing).

        Parameters
        ----------
        messages:
            List of ``{"role": ..., "content": ...}`` dicts.
        max_tokens:
            Maximum tokens in the response.
        temperature:
            Sampling temperature (default 0 for deterministic output).
        config:
            Optional override config (uses ``self._config`` when omitted).
        """
        cfg = config or self._config
        client = self._client
        if config is not None and config is not self._config:
            # Different config — create a one-shot client
            AsyncOpenAI = _import_async_openai()
            client = AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=120.0,
            )

        create_kwargs: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        # DeepSeek: disable thinking for JSON mode (it can inject non-JSON)
        if cfg.provider == "deepseek":
            create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        try:
            response = await client.chat.completions.create(**create_kwargs)
        finally:
            if config is not None and config is not self._config:
                await client.close()

        return response.choices[0].message.content or ""

    # ── close ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.close()


# ── standalone JSON completion ────────────────────────────────────────────

async def create_json_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 8192,
    temperature: float = 0,
    config: LLMConfig | None = None,
) -> str:
    """Call LLM with JSON mode and return the content string.

    Convenience wrapper around :meth:`ChatClient.json_completion` for
    callers that don't have a ``ChatClient`` instance (reflection,
    skill merging).  Disables DeepSeek thinking mode automatically.

    Args:
        messages: List of ``{"role": ..., "content": ...}`` dicts.
        max_tokens: Maximum tokens for the response.
        temperature: Sampling temperature.
        config: Optional LLMConfig. When None, resolved from environment.

    Returns:
        The JSON string from the model's response content.
    """
    if config is None:
        from alex.config import get_llm_config
        config = get_llm_config()

    client = ChatClient(config)
    try:
        return await client.json_completion(
            messages, max_tokens=max_tokens, temperature=temperature,
        )
    finally:
        await client.close()
