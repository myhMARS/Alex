"""Prompt assembly — system prompt with skill injection."""

from __future__ import annotations

from alex.skill.models import SkillManager


class PromptAssembler:
    """Builds the augmented system prompt with matched skill instructions."""

    def __init__(self, system_prompt: str, skill_manager: SkillManager) -> None:
        self._system_prompt = system_prompt
        self._skills = skill_manager
        self._current_augmented_prompt = system_prompt

    @property
    def augmented_prompt(self) -> str:
        return self._current_augmented_prompt

    def ensure_skills_prompt(self, query: str) -> bool:
        """Inject matched skill instructions into the system prompt.

        Returns True if the prompt changed (caller should rebuild the graph).
        """
        skills_text = self._skills.inject_skills_prompt(query)
        augmented = self._system_prompt + skills_text if skills_text else self._system_prompt
        if augmented != self._current_augmented_prompt:
            self._current_augmented_prompt = augmented
            return True
        return False
