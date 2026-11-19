/** Skill consolidation prompt — port of alex/prompts/merge_skills_prompt.j2. */

export interface MergeSkillInput {
  id: string;
  name: string;
  pattern: string;
  instruction: string;
  tags: string[];
}

export function mergeSkillsPrompt(skills: MergeSkillInput[]): string {
  const lines = [
    'You are a skill consolidation engine. Merge redundant skills into a minimal set.',
    '',
    'IMPORTANT: Your response must be ONLY a JSON object. No explanations, no markdown, no code blocks, no thinking — ONLY the raw JSON.',
    '',
    '## Task',
    '',
    '1. Group skills that cover the same scenario into clusters',
    '2. Merge each cluster into ONE consolidated skill',
    '3. Keep genuinely unique skills unchanged',
    '',
    '## Rules',
    '',
    '- Each scenario = at most ONE skill',
    '"Greeting response", "Chinese greeting", "Friendly greeting" = SAME scenario',
    '"Ignore first", "Placeholder ignore", "Ignore trivial" = SAME scenario',
    '- When merging, use the best pattern and instruction from the group',
    '- Combine tags from all merged skills',
    '',
    '## Skills to consolidate:',
    '',
  ];
  for (const skill of skills) {
    lines.push(`[${skill.id}] ${skill.name}`);
    lines.push(`  pattern: ${skill.pattern}`);
    lines.push(`  instruction: ${skill.instruction}`);
    lines.push(`  tags: ${JSON.stringify(skill.tags)}`);
    lines.push('');
  }
  lines.push(
    '## Response format',
    '',
    'Respond with ONLY this JSON structure (no other text):',
    '',
    '{"merged_groups": [{"keep_id": "id_to_keep", "merge_ids": ["id1", "id2"], "updated_name": "name", "updated_pattern": "pattern", "updated_instruction": "instruction", "updated_tags": ["t1", "t2"]}], "deprecate_ids": ["useless_id"]}',
  );
  return lines.join('\n');
}