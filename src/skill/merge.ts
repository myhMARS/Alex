/** LLM-driven skill consolidation — port of alex/skill/service.py merge_skills. */

import type { ChatModel } from '../llm/client';
import type { MergeSkillsResult } from '../kernel/dto/skill';
import { mergeSkillsPrompt } from '../prompts/merge-skills';
import type { Skill } from './models';

export interface MergeGroup {
  keepId: string;
  mergeIds: string[];
  updatedName?: string;
  updatedPattern?: string;
  updatedInstruction?: string;
  updatedTags?: string[];
}

export interface ParsedMerge {
  groups: MergeGroup[];
  deprecateIds: string[];
}

/** Tolerant parse of the consolidation JSON (fences / trailing commas). */
export function parseMergeResponse(text: string): ParsedMerge | null {
  let cleaned = text.trim();
  if (!cleaned) return null;
  const fence = cleaned.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence?.[1]) cleaned = fence[1].trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  cleaned = cleaned.slice(start, end + 1);

  let data: unknown;
  try {
    data = JSON.parse(cleaned);
  } catch {
    try {
      data = JSON.parse(cleaned.replace(/,\s*([}\]])/g, '$1'));
    } catch {
      return null;
    }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const record = data as Record<string, unknown>;
  const groupsRaw = Array.isArray(record.merged_groups) ? record.merged_groups : [];
  const groups: MergeGroup[] = [];
  for (const item of groupsRaw) {
    if (!item || typeof item !== 'object') continue;
    const entry = item as Record<string, unknown>;
    const keepId = String(entry.keep_id ?? '');
    if (!keepId) continue;
    const mergeIds = Array.isArray(entry.merge_ids) ? entry.merge_ids.map(String) : [];
    groups.push({
      keepId,
      mergeIds,
      updatedName: typeof entry.updated_name === 'string' ? entry.updated_name : undefined,
      updatedPattern:
        typeof entry.updated_pattern === 'string' ? entry.updated_pattern : undefined,
      updatedInstruction:
        typeof entry.updated_instruction === 'string'
          ? entry.updated_instruction
          : undefined,
      updatedTags: Array.isArray(entry.updated_tags)
        ? entry.updated_tags.map(String)
        : undefined,
    });
  }
  const deprecateIds = Array.isArray(record.deprecate_ids)
    ? record.deprecate_ids.map(String)
    : [];
  if (groups.length === 0 && deprecateIds.length === 0) return null;
  return { groups, deprecateIds };
}

export async function requestMerge(
  model: ChatModel,
  skills: Skill[],
): Promise<ParsedMerge | null> {
  const prompt = mergeSkillsPrompt(
    skills.map((skill) => ({
      id: skill.id,
      name: skill.name,
      pattern: skill.pattern,
      instruction: skill.instruction,
      tags: skill.tags,
    })),
  );
  let text = '';
  for await (const event of model.streamChat({
    messages: [{ role: 'user', content: prompt }],
    maxTokens: 4096,
  })) {
    if (event.type === 'content') text += event.delta;
    else if (event.type === 'done' && event.result.content) text = event.result.content;
  }
  return parseMergeResponse(text);
}

export type { MergeSkillsResult };