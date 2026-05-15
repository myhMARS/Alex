"""OpenAI adapter."""

from langchain_openai import ChatOpenAI

from alex.llm.factory import LLMFactory


@LLMFactory.register("openai")
class OpenAIAdapter(ChatOpenAI):
    """OpenAI chat model adapter."""
