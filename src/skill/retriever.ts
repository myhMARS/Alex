/** Tag + keyword skill retrieval — TypeScript port of alex/skill/matcher.py. */

import type { SkillStore } from './store';
import type { Skill } from './models';

export function retrieveSkills(store: SkillStore, query: string, topK = 3): Skill[] {
  const candidates = store
    .listAll()
    .filter((skill) => skill.status === 'ACTIVE' || skill.status === 'CANDIDATE');
  if (candidates.length === 0) return [];

  const queryLower = query.toLowerCase();
  const queryWords = new Set(queryLower.split(/\s+/).filter(Boolean));
  const scored: { skill: Skill; score: number }[] = [];

  for (const skill of candidates) {
    let score = 0;
    for (const tag of skill.tags) {
      if (queryLower.includes(tag.toLowerCase())) score += 2;
    }
    const patternWords = new Set(skill.pattern.toLowerCase().split(/\s+/).filter(Boolean));
    for (const word of patternWords) {
      if (queryWords.has(word)) score += 0.5;
    }
    if (skill.name && queryLower.includes(skill.name.toLowerCase())) score += 3;
    score += skill.confidence;
    if (skill.status === 'CANDIDATE') score *= 0.6;
    if (score > 0) scored.push({ skill, score });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, Math.max(1, topK)).map((entry) => entry.skill);
}