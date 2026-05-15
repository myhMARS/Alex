"""Skill lifecycle evolution engine."""

from alex.skills.store import SkillStore


class EvolutionEngine:
    """Evaluates and transitions skills through their lifecycle states.

    CANDIDATE → ACTIVE      when use_count >= 3 AND success_rate >= 0.7
    CANDIDATE → DEPRECATED  when use_count >= 5 AND success_rate < 0.3
    ACTIVE → DEPRECATED     when use_count >= 5 AND success_rate < 0.3
    """

    def evolve(self, store: SkillStore, max_active: int = 50) -> None:
        for skill in store.list_all():
            if skill.status == "CANDIDATE":
                if skill.use_count >= 5 and skill.success_rate < 0.3:
                    skill.status = "DEPRECATED"
                    store.update(skill)
                elif skill.use_count >= 3 and skill.success_rate >= 0.7:
                    skill.status = "ACTIVE"
                    store.update(skill)

            elif skill.status == "ACTIVE":
                if skill.use_count >= 5 and skill.success_rate < 0.3:
                    skill.status = "DEPRECATED"
                    store.update(skill)

        # enforce max active
        active = store.list_active()
        if len(active) > max_active:
            active.sort(key=lambda s: s.confidence)
            for s in active[:len(active) - max_active]:
                s.status = "DEPRECATED"
                store.update(s)
