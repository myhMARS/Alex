"""Alex - An AI agent with tools, memory, skills, and streaming."""

__all__ = ["Agent"]


def __getattr__(name: str):
    if name == "Agent":
        from alex.agent import Agent
        return Agent
    raise AttributeError(name)
