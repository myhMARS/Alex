"""LLM Factory layer — provider-agnostic model creation."""

from alex.llm.base import LLMConfig
from alex.llm.factory import LLMFactory
from alex.llm.json_client import create_json_completion

# Load adapters to trigger @LLMFactory.register decorators
import alex.llm.deepseek  # noqa: F401
import alex.llm.openai  # noqa: F401
import alex.llm.anthropic  # noqa: F401

__all__ = ["LLMConfig", "LLMFactory", "create_json_completion"]
