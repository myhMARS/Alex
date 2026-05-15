"""DeepSeek adapter with reasoning_content echo-back for thinking mode."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek

from alex.llm.factory import LLMFactory


@LLMFactory.register("deepseek")
class DeepSeekAdapter(ChatDeepSeek):
    """ChatDeepSeek with reasoning_content round-trip for thinking mode."""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        if isinstance(input_, list):
            messages = input_
        elif hasattr(input_, "messages"):
            messages = input_.messages
        else:
            return payload

        for msg, payload_msg in zip(messages, payload["messages"]):
            if isinstance(msg, AIMessage):
                reasoning = msg.additional_kwargs.get("reasoning_content")
                if reasoning:
                    payload_msg["reasoning_content"] = reasoning

        return payload
