/** Skill card prompt — port of alex/prompts/skill_card.j2. */

export interface SkillCardInput {
  name: string;
  pattern: string;
  instruction: string;
}

export function skillCardPrompt(skill: SkillCardInput): string {
  return [
    `## Skill: ${skill.name}`,
    `**Applies when**: ${skill.pattern}`,
    '**Execution methodology**:',
    skill.instruction,
  ].join('\n');
}