"""Anthropic adapter."""

from langchain_anthropic import ChatAnthropic

from alex.llm.factory import LLMFactory


@LLMFactory.register("anthropic")
class AnthropicAdapter(ChatAnthropic):
    """Anthropic chat model adapter."""
