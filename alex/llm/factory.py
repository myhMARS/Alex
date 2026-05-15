"""LLM Factory with decorator-based provider registration."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from alex.llm.base import LLMConfig

# Per-provider parameter name mapping (LLMConfig field → adapter kwarg name)
_PROVIDER_PARAMS: dict[str, dict[str, str]] = {
    "deepseek": {"api_key": "api_key", "base_url": "api_base"},
    "openai": {"api_key": "api_key", "base_url": "base_url"},
    "anthropic": {"api_key": "api_key", "base_url": "anthropic_api_url"},
}


class LLMFactory:
    """Factory that creates LLM instances from LLMConfig.

    Providers register via the @LLMFactory.register("name") decorator.
    """

    _registry: dict[str, type[BaseChatModel]] = {}

    @classmethod
    def register(cls, name: str):
        """Decorator to register a chat model adapter class."""

        def decorator(adapter_cls: type[BaseChatModel]):
            cls._registry[name] = adapter_cls
            return adapter_cls

        return decorator

    @classmethod
    def create(cls, config: LLMConfig) -> BaseChatModel:
        """Create an LLM instance from configuration.

        Maps LLMConfig fields to provider-specific parameter names.
        """
        if config.provider not in cls._registry:
            available = ", ".join(cls._registry.keys()) or "(none)"
            msg = f"Unknown provider '{config.provider}'. Available: {available}"
            raise ValueError(msg)

        adapter_cls = cls._registry[config.provider]
        mapping = _PROVIDER_PARAMS.get(config.provider, _PROVIDER_PARAMS["openai"])

        kwargs: dict[str, Any] = {
            mapping["api_key"]: config.api_key,
            mapping["base_url"]: config.base_url,
            "model": config.model,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        kwargs.update(config.extra)
        return adapter_cls(**kwargs)

    @classmethod
    def list_providers(cls) -> list[str]:
        return list(cls._registry.keys())
