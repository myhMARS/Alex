"""Tag + keyword-based skill retriever."""

from alex.skill.models import Skill
from alex.skill.repository import SkillStore


class SkillRetriever:
    """Match active skills by tag overlap and pattern keyword relevance."""

    def __init__(self, store: SkillStore) -> None:
        self._store = store

    def retrieve(self, query: str, top_k: int = 3) -> list[Skill]:
        # Include both ACTIVE and CANDIDATE skills (candidates get a penalty)
        candidates = [s for s in self._store.list_all() if s.status in ("ACTIVE", "CANDIDATE")]
        if not candidates:
            return []

        query_lower = query.lower()
        scored = []

        for skill in candidates:
            score = 0.0

            # tag overlap
            for tag in skill.tags:
                if tag.lower() in query_lower:
                    score += 2.0

            # pattern keyword match
            pattern_words = set(skill.pattern.lower().split())
            query_words = set(query_lower.split())
            overlap = pattern_words & query_words
            score += len(overlap) * 0.5

            # name match
            if skill.name.lower() in query_lower:
                score += 3.0

            # confidence bonus
            score += skill.confidence

            # CANDIDATE penalty — lower priority than ACTIVE
            if skill.status == "CANDIDATE":
                score *= 0.6

            if score > 0:
                scored.append((skill, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in scored[:top_k]]
