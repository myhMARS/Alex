"""LangChain callbacks — bridges LangChain events to the display renderer."""

from __future__ import annotations

import ast
import json

from json_repair import repair_json
from langchain_core.callbacks import BaseCallbackHandler

from alex.display import DisplayEvent, EventType, renderer


class ToolDisplayCallback(BaseCallbackHandler):
    """Bridge LangChain tool callbacks to the event-driven renderer.

    All tool_start/tool_end events are pushed into the renderer's queue.
    The renderer handles batching, parallel display, and live updates.
    """

    def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        **kwargs,
    ) -> None:
        tool_name = serialized.get("name", "unknown")
        args = self._parse_input(input_str)
        # Unwrap single-key "input" wrapper
        if len(args) == 1 and "input" in args and isinstance(args["input"], str):
            nested = self._parse_input(args["input"])
            if nested:
                args = nested
        run_id = str(kwargs.get("run_id") or "")
        renderer.emit(DisplayEvent(
            type=EventType.TOOL_START,
            data={"id": run_id, "name": tool_name, "args": args},
        ))

    @staticmethod
    def _parse_input(raw: str) -> dict:
        # Try standard JSON first, then repair and retry
        for parser in (json.loads, ast.literal_eval):
            try:
                result = parser(raw)
                if isinstance(result, dict):
                    return result
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
        try:
            result = json.loads(repair_json(raw))
            if isinstance(result, dict):
                return result
        except (ValueError, json.JSONDecodeError):
            pass
        return {"input": raw}

    def on_tool_end(
        self,
        output: str,
        **kwargs,
    ) -> None:
        run_id = str(kwargs.get("run_id") or "")
        renderer.emit(DisplayEvent(
            type=EventType.TOOL_END,
            data={"id": run_id, "output": str(output)},
        ))
