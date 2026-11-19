import { describe, expect, test } from 'bun:test';

import { buildReflectionPrompt } from '../src/prompts/reflection';
import { skillsSectionPrompt } from '../src/prompts/skills-section';
import { mergeSkillsPrompt } from '../src/prompts/merge-skills';
import { skillCardPrompt } from '../src/prompts/skill-card';
import { createSkill } from '../src/skill/models';

describe('prompt builders', () => {
  test('reflection prompt lists existing skills and rules', () => {
    const skill = createSkill({ name: 'debug-import', pattern: 'python import error' });
    const prompt = buildReflectionPrompt([skill], [
      { query: 'fix import', toolsUsed: ['read'], outcome: 'fixed' },
    ]);
    expect(prompt).toContain(`[${skill.id}] debug-import: python import error`);
    expect(prompt).toContain('new_skills');
    expect(prompt).toContain('Round 1');
    expect(prompt).toContain('fix import');
  });

  test('skills section lists available skills and load_skill guidance', () => {
    const prompt = skillsSectionPrompt([
      { name: 'debug-import', pattern: 'python import error' },
    ]);
    expect(prompt).toContain('## Available Skills');
    expect(prompt).toContain('**debug-import** — python import error');
    expect(prompt).toContain('load_skill');
    expect(skillsSectionPrompt([])).toBe('');
  });

  test('merge prompt embeds the skills and JSON contract', () => {
    const prompt = mergeSkillsPrompt([
      {
        id: 'abc',
        name: 'greet',
        pattern: 'greeting',
        instruction: 'say hi',
        tags: ['chat'],
      },
    ]);
    expect(prompt).toContain('[abc] greet');
    expect(prompt).toContain('merged_groups');
    expect(prompt).toContain('deprecate_ids');
    expect(prompt).toContain('tags: ["chat"]');
  });

  test('skill card has the applies-when / methodology sections', () => {
    const card = skillCardPrompt({
      name: 'x',
      pattern: 'when y',
      instruction: 'do z',
    });
    expect(card).toContain('## Skill: x');
    expect(card).toContain('**Applies when**: when y');
    expect(card).toContain('do z');
  });
});