/** Reflection prompt builder — port of alex/prompts/reflection_prompt.j2. */

import type { Skill } from '../skill/models';

export const REFLECTION_RULES = `Analyze the accumulated problem-solving experience below and extract reusable execution methodologies.

A "skill" is a distilled execution methodology — a step-by-step guide for handling a specific problem domain. Skills are formed from repeated experience.

## What makes a good skill:
- name: kebab-case methodology name (e.g. "debug-python-import-error")
- pattern: when this skill applies (problem domain / user intent)
- instruction: numbered step-by-step playbook (tools, order, branches, verification)
- tags: domain keywords for retrieval

## RULES:
1. Capture the process, not the answer.
2. UPDATE FIRST: prefer improving existing skills over creating new ones.
3. One skill per problem domain.
4. Conservative extraction: only genuinely reusable, non-trivial workflows.
5. Empty arrays are valid.

## Output format (ONLY valid JSON):
{"new_skills":[{"name":"...","pattern":"...","instruction":"...","tags":["..."]}],
 "updated_skills":[{"id":"existing_id","pattern":"...","instruction":"...","tags":["..."]}],
 "deprecated_ids":["id"]}`;

export interface ReflectionEpisode {
  query?: string;
  skillsLoaded?: string[];
  toolsUsed?: string[];
  outcome?: string;
}

export function buildReflectionPrompt(
  existing: Skill[],
  episodes: ReflectionEpisode[] = [],
): string {
  const lines = [REFLECTION_RULES, '', '## Existing skills (prefer updating these):'];
  if (existing.length === 0) lines.push('(none)');
  for (const skill of existing) {
    lines.push(`- [${skill.id}] ${skill.name}: ${skill.pattern}`);
  }
  lines.push('', `## Accumulated experience episodes (${episodes.length} rounds):`);
  if (episodes.length === 0) {
    lines.push('(none)');
  } else {
    episodes.forEach((episode, index) => {
      lines.push(`### Round ${index + 1}`);
      if (episode.query) lines.push(`- **User query**: ${episode.query}`);
      lines.push(
        `- **Skills loaded**: ${(episode.skillsLoaded ?? []).join(', ') || '(none)'}`,
      );
      lines.push(`- **Tools used**: ${(episode.toolsUsed ?? []).join(', ') || '(none)'}`);
      if (episode.outcome) lines.push(`- **Outcome**: ${episode.outcome}`);
    });
  }
  return lines.join('\n');
}