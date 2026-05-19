"""Alex - An AI agent with tools, memory, skills, and streaming."""

__all__ = ["Agent", "ChatResponse"]


def __getattr__(name: str):
    if name in ("Agent", "ChatResponse"):
        from alex.agent import Agent, ChatResponse
        return {"Agent": Agent, "ChatResponse": ChatResponse}[name]
    raise AttributeError(name)
