/** Per-skill prompt templates — Python-compatible `prompts/<id>.j2` files. */

import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';

import { skillCardPrompt } from '../prompts/skill-card';
import type { Skill } from './models';

export function promptsDirFor(skillsPath: string): string {
  return join(dirname(skillsPath), 'prompts');
}

export function skillTemplatePath(promptsDir: string, skillId: string): string {
  return join(promptsDir, `${skillId}.j2`);
}

/** Write the same static template text as alex/prompts/__init__.py. */
export async function saveSkillTemplate(promptsDir: string, skill: Skill): Promise<void> {
  await mkdir(promptsDir, { recursive: true });
  const content = `${skillCardPrompt({
    name: skill.name,
    pattern: skill.pattern,
    instruction: skill.instruction,
  })}\n`;
  await writeFile(skillTemplatePath(promptsDir, skill.id), content, 'utf8');
}

export async function readSkillTemplate(
  promptsDir: string,
  skillId: string,
): Promise<string | null> {
  try {
    return await readFile(skillTemplatePath(promptsDir, skillId), 'utf8');
  } catch {
    return null;
  }
}

export async function removeSkillTemplate(
  promptsDir: string,
  skillId: string,
): Promise<void> {
  await unlink(skillTemplatePath(promptsDir, skillId)).catch(() => undefined);
}