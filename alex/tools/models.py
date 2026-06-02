"""AlexTool — custom tool definition replacing langchain_core.tools.StructuredTool.

Each tool carries:
- ``name`` / ``description`` — surfaced to the LLM
- ``parameters`` — JSON Schema dict for function-calling
- ``coroutine`` — async callable that receives ``**kwargs`` and returns ``str``
- ``metadata`` — optional dict for permission, MCP server info, etc.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass
class AlexTool:
    """A callable tool that the agent can invoke.

    Replaces ``langchain_core.tools.StructuredTool``.  The OpenAI-format
    schema is built from *parameters* (a JSON Schema dict), typically
    generated from a pydantic model via ``model_json_schema()``.
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the tool's input
    coroutine: Callable[..., Awaitable[str]]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> dict[str, Any]:
        """Return an OpenAI function-calling tool descriptor."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def invoke(self, args: dict[str, Any]) -> str:
        """Call the tool's coroutine with **args."""
        return await self.coroutine(**args)

    async def ainvoke(self, args: dict[str, Any]) -> str:
        """Alias for invoke — backward compatible with langchain BaseTool API."""
        return await self.invoke(args)

    @staticmethod
    def from_function(
        *,
        name: str,
        description: str,
        coroutine: Callable[..., Awaitable[str]],
        args_schema: type[BaseModel] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "AlexTool":
        """Factory matching the langchain ``StructuredTool.from_function`` signature.

        *args_schema* must be a pydantic ``BaseModel`` subclass; its
        ``model_json_schema()`` becomes the tool's ``parameters``.
        """
        if args_schema is not None:
            # pydantic v2: model_json_schema() returns the JSON Schema
            try:
                parameters = args_schema.model_json_schema()
            except AttributeError:
                # pydantic v1 fallback
                parameters = args_schema.schema()
        else:
            parameters = {
                "type": "object",
                "properties": {},
                "required": [],
            }

        # Detect whether coroutine is async or sync
        if not inspect.iscoroutinefunction(coroutine):
            original = coroutine

            async def _wrapper(**kw: Any) -> str:
                return str(original(**kw))

            coroutine = _wrapper

        return AlexTool(
            name=name,
            description=description,
            parameters=parameters,
            coroutine=coroutine,
            metadata=dict(metadata or {}),
        )
