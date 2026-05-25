"""JSON-file-based skill persistence."""

from __future__ import annotations

import json
from pathlib import Path

from alex.prompts import SKILLS_DIR, save_skill_template, remove_skill_template
from alex.skill.models import Skill


class SkillStore:
    """Persist skills to a JSON file."""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or (SKILLS_DIR / "skills.json"))
        self._skills: dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = json.load(f)
                for item in data:
                    s = Skill(**item)
                    self._skills[s.id] = s
            except Exception:
                pass

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(
                [s.__dict__ for s in self._skills.values()],
                f,
                ensure_ascii=False,
                indent=2,
            )

    def add(self, skill: Skill) -> None:
        self._skills[skill.id] = skill
        self._save()
        save_skill_template(skill.id, skill.name, skill.pattern, skill.instruction)

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def update(self, skill: Skill) -> None:
        if skill.id in self._skills:
            self._skills[skill.id] = skill
            self._save()
            save_skill_template(skill.id, skill.name, skill.pattern, skill.instruction)

    def deprecate(self, skill_id: str) -> None:
        s = self._skills.get(skill_id)
        if s:
            s.status = "DEPRECATED"
            self._save()
            remove_skill_template(skill_id)

    def list_active(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.status == "ACTIVE"]

    def list_all(self) -> list[Skill]:
        return list(self._skills.values())

    def remove(self, skill_id: str) -> None:
        self._skills.pop(skill_id, None)
        self._save()
        remove_skill_template(skill_id)
