import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { SkillStore } from '../src/skill/store';
import { evolveSkills } from '../src/skill/evolution';
import { createSkill, type Skill } from '../src/skill/models';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function storeWith(skills: Skill[]): Promise<SkillStore> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-evolution-'));
  tempDirs.push(dir);
  const store = new SkillStore({ path: join(dir, 'skills.json') });
  for (const skill of skills) await store.add(skill);
  return store;
}

function skillWith(
  name: string,
  status: Skill['status'],
  success: number,
  failure: number,
): Skill {
  const skill = createSkill({ name, pattern: 'p', instruction: 'i' });
  skill.status = status;
  skill.successCount = success;
  skill.failureCount = failure;
  skill.useCount = success + failure;
  return skill;
}

describe('skill evolution', () => {
  test('promotes a proven candidate to ACTIVE', async () => {
    const skill = skillWith('proven', 'CANDIDATE', 3, 0);
    const store = await storeWith([skill]);
    const result = await evolveSkills(store);
    expect(result.promoted).toBe(1);
    expect(store.get(skill.id)?.status).toBe('ACTIVE');
  });

  test('deprecates a failing candidate and a failing active skill', async () => {
    const candidate = skillWith('failing-candidate', 'CANDIDATE', 0, 5);
    const active = skillWith('failing-active', 'ACTIVE', 1, 4);
    const store = await storeWith([candidate, active]);
    const result = await evolveSkills(store);
    expect(result.deprecated).toBe(2);
    expect(store.get(candidate.id)?.status).toBe('DEPRECATED');
    expect(store.get(active.id)?.status).toBe('DEPRECATED');
  });

  test('enforces the max active cap by deprecating lowest confidence', async () => {
    const skills = [0.9, 0.2, 0.5].map((confidence, index) => {
      const skill = skillWith(`active-${index}`, 'ACTIVE', 1, 0);
      skill.confidence = confidence;
      return skill;
    });
    const store = await storeWith(skills);
    const result = await evolveSkills(store, 2);
    expect(result.deprecated).toBe(1);
    const active = store.listActive();
    expect(active).toHaveLength(2);
    expect(active.map((skill) => skill.name)).not.toContain('active-1');
  });
});
