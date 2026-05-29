"""Shared dependency construction helpers for Agent wiring."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from alex.config import get_llm_config
from alex.llm.base import LLMConfig
from alex.llm.factory import LLMFactory
from alex.memory.base import MemoryBase
from alex.memory.buffer import BufferMemory
from alex.skill import SkillService
from alex.skill.evolution import EvolutionEngine
from alex.skill.matcher import SkillRetriever
from alex.skill.reflector import Reflector
from alex.skill.repository import SkillStore


def create_default_config() -> LLMConfig:
    """Resolve the LLM configuration once, to be injected wherever JSON
    completions are needed (reflection, skill merging, etc.)."""
    return get_llm_config()


def create_default_llm() -> BaseChatModel:
    """Build the default chat model from the central config."""
    return LLMFactory.create(get_llm_config())


def create_default_memory() -> MemoryBase:
    """Build the default session-scoped memory backend."""
    return BufferMemory()


def create_default_skill_service() -> SkillService:
    """Build the default SkillService dependency graph."""
    store = SkillStore()
    reflector = Reflector()
    retriever = SkillRetriever(store)
    evolution = EvolutionEngine()
    return SkillService(store=store, reflector=reflector, retriever=retriever, evolution=evolution)
