"""Skill data model."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from alex.prompts import get_skill_prompt


@dataclass
class Skill:
    """A learned execution methodology — distilled from past problem-solving experience in a specific domain."""

    name: str
    pattern: str  # When to use (trigger scenario)
    instruction: str  # How to respond (strategy)
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    status: str = "CANDIDATE"  # CANDIDATE | ACTIVE | DEPRECATED
    use_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    confidence: float = 0.5
    version: int = 1
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0

    def record_use(self, success: bool) -> None:
        self.use_count += 1
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        self._update_confidence()

    def _update_confidence(self) -> None:
        """Bayesian-smoothed confidence estimate."""
        alpha = 2.0
        beta = 2.0
        n = self.success_count + self.failure_count
        self.confidence = (self.success_count + alpha) / (n + alpha + beta) if n > 0 else 0.5

    def to_prompt_text(self) -> str:
        return get_skill_prompt(
            self.id,
            name=self.name,
            pattern=self.pattern,
            instruction=self.instruction,
        )
