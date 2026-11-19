/** Skill lifecycle evolution — port of alex/skill/evolution.py. */

import { skillSuccessRate, type Skill } from './models';
import type { SkillStore } from './store';

export interface EvolutionSummary {
  promoted: number;
  deprecated: number;
}

export async function evolveSkills(
  store: SkillStore,
  maxActive = 50,
): Promise<EvolutionSummary> {
  let promoted = 0;
  let deprecated = 0;

  for (const skill of store.listAll()) {
    const rate = skillSuccessRate(skill);
    if (skill.status === 'CANDIDATE') {
      if (skill.useCount >= 5 && rate < 0.3) {
        skill.status = 'DEPRECATED';
        await store.update(skill);
        deprecated += 1;
      } else if (skill.useCount >= 3 && rate >= 0.7) {
        skill.status = 'ACTIVE';
        await store.update(skill);
        promoted += 1;
      }
    } else if (skill.status === 'ACTIVE') {
      if (skill.useCount >= 5 && rate < 0.3) {
        skill.status = 'DEPRECATED';
        await store.update(skill);
        deprecated += 1;
      }
    }
  }

  const active = store.listActive();
  if (active.length > maxActive) {
    active.sort((a: Skill, b: Skill) => a.confidence - b.confidence);
    for (const skill of active.slice(0, active.length - maxActive)) {
      skill.status = 'DEPRECATED';
      await store.update(skill);
      deprecated += 1;
    }
  }

  return { promoted, deprecated };
}