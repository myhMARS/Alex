/** Skill model — TypeScript port of alex/skill/models.py. */

import type { SkillCard } from '../kernel/dto/skill';
import { skillCardPrompt } from '../prompts/skill-card';

export type SkillStatus = 'CANDIDATE' | 'ACTIVE' | 'DEPRECATED';

export interface Skill {
  id: string;
  name: string;
  pattern: string;
  instruction: string;
  tags: string[];
  examples: string[];
  status: SkillStatus;
  useCount: number;
  successCount: number;
  failureCount: number;
  confidence: number;
  version: number;
}

export interface SkillDraft {
  name: string;
  pattern: string;
  instruction: string;
  tags: string[];
}

export interface SkillUpdate extends Partial<SkillDraft> {
  id: string;
}

let idCounter = 0;

export function newSkillId(): string {
  idCounter += 1;
  return `${Date.now().toString(36)}${idCounter.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

export function createSkill(draft: Partial<SkillDraft> = {}): Skill {
  return {
    id: newSkillId(),
    name: (draft.name ?? '').trim(),
    pattern: (draft.pattern ?? '').trim(),
    instruction: (draft.instruction ?? '').trim(),
    tags: Array.isArray(draft.tags) ? draft.tags.map(String) : [],
    examples: [],
    status: 'CANDIDATE',
    useCount: 0,
    successCount: 0,
    failureCount: 0,
    confidence: 0.5,
    version: 1,
  };
}

/** Bayesian-smoothed confidence, mirroring Python's Skill._update_confidence. */
export function recordSkillUse(skill: Skill, success: boolean): void {
  skill.useCount += 1;
  if (success) skill.successCount += 1;
  else skill.failureCount += 1;
  const alpha = 2.0;
  const beta = 2.0;
  const total = skill.successCount + skill.failureCount;
  skill.confidence =
    total > 0 ? (skill.successCount + alpha) / (total + alpha + beta) : 0.5;
}

export function skillSuccessRate(skill: Skill): number {
  const total = skill.successCount + skill.failureCount;
  return total > 0 ? skill.successCount / total : 0;
}

export function skillToCard(skill: Skill): SkillCard {
  return {
    id: skill.id,
    name: skill.name,
    pattern: skill.pattern,
    instruction: skill.instruction,
    tags: [...skill.tags],
    status: skill.status,
    useCount: skill.useCount,
    successCount: skill.successCount,
    failureCount: skill.failureCount,
  };
}

export function renderSkillPrompt(skill: Skill): string {
  // Tool output keeps the Python `load_skill` tool shape.
  return [
    `[Skill: ${skill.name}]`,
    '',
    `When to apply: ${skill.pattern}`,
    '',
    `Execution methodology:\n${skill.instruction}`,
  ].join('\n');
}

/** Prompt-injection shape (alex/prompts/skill_card.j2). */
export function renderSkillCard(skill: Skill): string {
  return skillCardPrompt({
    name: skill.name,
    pattern: skill.pattern,
    instruction: skill.instruction,
  });
}