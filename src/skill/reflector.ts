/** LLM-based skill reflection — TypeScript port of alex/skill/reflector.py. */

import type { MessageDTO } from '../kernel/dto/message';
import type { ChatModel } from '../llm/client';
import type { Skill, SkillDraft, SkillUpdate } from './models';

export interface ReflectionInput {
  messages: MessageDTO[];
  existing: Skill[];
  episodes?: ReflectionEpisode[];
}

export { buildReflectionPrompt };

export interface ReflectionResult {
  newSkills: SkillDraft[];
  updatedSkills: SkillUpdate[];
  deprecatedIds: string[];
}

export interface SkillReflectorOptions {
  maxTokens?: number;
}

import { buildReflectionPrompt, type ReflectionEpisode } from '../prompts/reflection';

function mapMessages(messages: MessageDTO[]): MessageDTO[] {
  const mapped: MessageDTO[] = [];
  for (const message of messages) {
    const content = typeof message.content === 'string' ? message.content : '';
    if (message.role === 'tool') {
      mapped.push({
        role: 'user',
        content: `[tool_result id=${String(message.tool_call_id ?? '')}] ${content}`,
      });
    } else if (message.role === 'system') {
      mapped.push({ role: 'system', content });
    } else if (message.role === 'assistant') {
      mapped.push({ role: 'assistant', content });
    } else {
      mapped.push({ role: 'user', content });
    }
  }
  return mapped;
}

export class SkillReflector {
  private readonly model: ChatModel;
  private readonly maxTokens: number;

  constructor(model: ChatModel, options: SkillReflectorOptions = {}) {
    this.model = model;
    this.maxTokens = options.maxTokens ?? 4096;
  }

  async reflect(input: ReflectionInput): Promise<ReflectionResult> {
    const prompt = buildReflectionPrompt(input.existing, input.episodes ?? []);
    const messages: MessageDTO[] = [
      { role: 'system', content: prompt },
      ...mapMessages(input.messages.slice(-20)),
      {
        role: 'user',
        content: 'Extract execution workflows from this conversation. Output only JSON.',
      },
    ];

    let text = '';
    for await (const event of this.model.streamChat({
      messages,
      maxTokens: this.maxTokens,
    })) {
      if (event.type === 'content') text += event.delta;
      else if (event.type === 'done' && event.result.content) text = event.result.content;
    }
    return parseReflection(text);
  }
}

/** Tolerant JSON parsing of the reflection response. */
export function parseReflection(text: string): ReflectionResult {
  const empty: ReflectionResult = { newSkills: [], updatedSkills: [], deprecatedIds: [] };
  if (!text.trim()) return empty;

  let cleaned = text.trim();
  const fence = cleaned.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence?.[1]) cleaned = fence[1].trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) return empty;
  cleaned = cleaned.slice(start, end + 1);

  let data: unknown;
  try {
    data = JSON.parse(cleaned);
  } catch {
    try {
      data = JSON.parse(cleaned.replace(/,\s*([}\]])/g, '$1'));
    } catch {
      return empty;
    }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return empty;
  const record = data as Record<string, unknown>;

  const newSkills: SkillDraft[] = [];
  for (const item of Array.isArray(record.new_skills) ? record.new_skills : []) {
    if (!item || typeof item !== 'object') continue;
    const entry = item as Record<string, unknown>;
    const name = String(entry.name ?? '').trim();
    if (!name) continue;
    newSkills.push({
      name,
      pattern: String(entry.pattern ?? ''),
      instruction: String(entry.instruction ?? ''),
      tags: Array.isArray(entry.tags) ? entry.tags.map(String) : [],
    });
  }

  const updatedSkills: SkillUpdate[] = [];
  for (const item of Array.isArray(record.updated_skills) ? record.updated_skills : []) {
    if (!item || typeof item !== 'object') continue;
    const entry = item as Record<string, unknown>;
    const id = String(entry.id ?? '');
    if (!id) continue;
    const update: SkillUpdate = { id };
    if (typeof entry.pattern === 'string') update.pattern = entry.pattern;
    if (typeof entry.instruction === 'string') update.instruction = entry.instruction;
    if (typeof entry.name === 'string') update.name = entry.name;
    if (Array.isArray(entry.tags)) update.tags = entry.tags.map(String);
    updatedSkills.push(update);
  }

  const deprecatedIds = Array.isArray(record.deprecated_ids)
    ? record.deprecated_ids.map(String)
    : [];

  return { newSkills, updatedSkills, deprecatedIds };
}