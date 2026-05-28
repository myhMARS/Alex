"""JSON-file-based skill persistence with atomic writes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from alex.prompts import SKILLS_DIR, save_skill_template, remove_skill_template
from alex.skill.models import Skill

logger = logging.getLogger(__name__)


class SkillStore:
    """Persist skills to a JSON file with atomic writes.

    On load, corrupt or unparseable data is discarded and a warning is
    logged rather than crashing the process.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or (SKILLS_DIR / "skills.json"))
        self._skills: dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                raw = f.read()
        except OSError:
            logger.warning("SkillStore: cannot read %s, starting empty", self._path)
            return

        if not raw.strip():
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("SkillStore: corrupt JSON in %s, starting empty", self._path)
            return

        if not isinstance(data, list):
            logger.warning("SkillStore: unexpected top-level type %s in %s, starting empty",
                           type(data).__name__, self._path)
            return

        for item in data:
            if not isinstance(item, dict):
                logger.warning("SkillStore: skipping non-dict entry in %s", self._path)
                continue
            try:
                s = Skill(**item)
                self._skills[s.id] = s
            except Exception:
                logger.warning("SkillStore: skipping corrupt skill entry in %s", self._path, exc_info=True)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [s.__dict__ for s in self._skills.values()],
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self._path)

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
