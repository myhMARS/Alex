"""Skill factory — default skill service for backward compatibility.

Prefer using ``SkillModule`` via ``ModuleHost`` for new code.
"""

from __future__ import annotations

from alex.skill import SkillService
from alex.skill.evolution import EvolutionEngine
from alex.skill.matcher import SkillRetriever
from alex.skill.reflector import Reflector
from alex.skill.repository import SkillStore


def create_default_skill_service() -> SkillService:
    """Build the default SkillService dependency graph."""
    store = SkillStore()
    reflector = Reflector()
    retriever = SkillRetriever(store)
    evolution = EvolutionEngine()
    return SkillService(store=store, reflector=reflector, retriever=retriever, evolution=evolution)
