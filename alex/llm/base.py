"""LLM configuration data model."""

from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    """Unified LLM configuration across all providers."""

    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    max_tokens: int = 8192
    temperature: float = 0.0
    extra: dict = field(default_factory=dict)
