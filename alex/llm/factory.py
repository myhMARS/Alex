"""LLM Factory — creates ChatClient instances from LLMConfig.

The factory preserves the same public API as before but returns
a unified :class:`ChatClient` instead of a langchain BaseChatModel.
"""

from __future__ import annotations

from alex.llm.base import LLMConfig
from alex.llm.client import ChatClient


class LLMFactory:
    """Factory that creates ChatClient instances from LLMConfig.

    Maintained for backward compatibility.  Callers can also construct
    ``ChatClient(config)`` directly.
    """

    @staticmethod
    def create(config: LLMConfig) -> ChatClient:
        """Create a ChatClient for *config*.

        All providers (deepseek, openai, anthropic) use the same
        OpenAI-compatible client.
        """
        return ChatClient(config)

    @staticmethod
    def list_providers() -> list[str]:
        """Supported provider identifiers."""
        return ["deepseek", "openai", "anthropic"]
