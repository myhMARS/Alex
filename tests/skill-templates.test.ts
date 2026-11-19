import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { SkillStore } from '../src/skill/store';
import { createSkill, type Skill } from '../src/skill/models';
import { skillTemplatePath } from '../src/skill/templates';
import { skillCardPrompt } from '../src/prompts/skill-card';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function newStore(): Promise<{ store: SkillStore; dir: string }> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-templates-'));
  tempDirs.push(dir);
  const store = new SkillStore({ path: join(dir, 'skills', 'skills.json') });
  return { store, dir };
}

function newSkill(name: string, instruction: string): Skill {
  const skill = createSkill({ name, pattern: 'when testing', instruction });
  skill.status = 'ACTIVE';
  return skill;
}

describe('per-skill prompt templates', () => {
  test('writing a skill creates the Python-compatible .j2 template', async () => {
    const { store } = await newStore();
    const skill = newSkill('debug-import', '1. read traceback\n2. fix imports');
    await store.add(skill);

    const path = skillTemplatePath(store.promptsDir, skill.id);
    const content = await readFile(path, 'utf8');
    expect(content).toBe(
      `${skillCardPrompt({
        name: skill.name,
        pattern: skill.pattern,
        instruction: skill.instruction,
      })}\n`,
    );
    expect(content).toContain('## Skill: debug-import');
    expect(content).toContain('**Applies when**: when testing');
    expect(content).toContain('1. read traceback');
    expect(content).not.toContain('{{');

    expect(await store.templateFor(skill.id)).toBe(content);
  });

  test('updates rewrite the template and deprecation removes it', async () => {
    const { store } = await newStore();
    const skill = newSkill('evolve-me', 'old instruction');
    await store.add(skill);
    const path = skillTemplatePath(store.promptsDir, skill.id);

    skill.instruction = 'new instruction';
    skill.name = 'evolved';
    await store.update(skill);
    const updated = await readFile(path, 'utf8');
    expect(updated).toContain('## Skill: evolved');
    expect(updated).toContain('new instruction');

    await store.deprecate(skill.id);
    expect(await store.templateFor(skill.id)).toBeNull();
  });

  test('removing a skill deletes the template file', async () => {
    const { store } = await newStore();
    const skill = newSkill('remove-me', 'x');
    await store.add(skill);
    expect(await store.templateFor(skill.id)).not.toBeNull();

    await store.remove(skill.id);
    expect(await store.templateFor(skill.id)).toBeNull();
  });

  test('supports an explicit prompts directory', async () => {
    const { dir } = await newStore();
    const store = new SkillStore({
      path: join(dir, 'custom', 'skills.json'),
      promptsDir: join(dir, 'custom-prompts'),
    });
    const skill = newSkill('custom', 'y');
    await store.add(skill);
    expect(store.promptsDir).toBe(join(dir, 'custom-prompts'));
    expect(await store.templateFor(skill.id)).toContain('## Skill: custom');
  });
});