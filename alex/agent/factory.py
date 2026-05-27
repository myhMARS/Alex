"""Agent factory — explicit wiring function for creating a fully-assembled Agent.

Extracts the wiring logic from Agent.__init__ so callers get a ready-to-use
Agent without knowing about internal service construction order.
"""

from __future__ import annotations

from typing import Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool as LCBaseTool

from alex.agent.service import Agent
from alex.bus import AsyncEventBus
from alex.config import get_llm_config
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.skill.models import SkillManager


def create_agent(
    *,
    system_prompt: str | None = None,
    max_iterations: int = 5,
    tools: list[LCBaseTool] | None = None,
    callbacks: list[BaseCallbackHandler] | None = None,
    memory: MemoryBase | None = None,
    skill_manager: SkillManager | None = None,
    llm: BaseChatModel | None = None,
    event_bus: AsyncEventBus | None = None,
    llm_factory: Callable[[], BaseChatModel] | None = None,
) -> Agent:
    """Create a fully-wired Agent with all services composed.

    All parameters are optional; sensible defaults are provided for
    memory, skills, and the LLM (via LLMFactory + config).

    Pass *llm_factory* to defer LLM creation until the first Graph
    build rather than creating it eagerly.
    """
    _llm = llm or (llm_factory() if llm_factory else LLMFactory.create(get_llm_config()))
    _memory = memory or BufferMemory()
    _skills = skill_manager or SkillManager()
    _system_prompt = system_prompt or "You are a helpful AI assistant."

    return Agent(
        system_prompt=_system_prompt,
        max_iterations=max_iterations,
        tools=tools,
        callbacks=callbacks,
        memory=_memory,
        skill_manager=_skills,
        llm=_llm,
        event_bus=event_bus,
    )
