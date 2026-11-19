/** Skills section prompt — port of alex/prompts/skills_section.j2. */

export interface SkillSummary {
  name: string;
  pattern?: string;
}

export function skillsSectionPrompt(skills: SkillSummary[]): string {
  if (skills.length === 0) return '';
  const lines = [
    '',
    '## Available Skills',
    '',
    'Below are learned execution methodologies — each is a skill distilled from past problem-solving experience in a specific domain.',
    "To load a skill's full execution guide, call the `load_skill` tool with the skill name.",
    '',
  ];
  for (const skill of skills) {
    lines.push(`- **${skill.name}** — ${skill.pattern ?? ''}`);
  }
  lines.push(
    '',
    'Only call `load_skill` when a skill is genuinely relevant to the user\'s task. If no skill matches, respond normally.',
  );
  return lines.join('\n');
}