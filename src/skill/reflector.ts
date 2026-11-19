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

function contentText(message: MessageDTO): string {
  if (typeof message.content === 'string') return message.content;
  if (!Array.isArray(message.content)) return '';
  return message.content
    .map((part) =>
      part && typeof part === 'object' && (part as { type?: string }).type === 'text'
        ? String((part as { text?: unknown }).text ?? '')
        : '[attachment]',
    )
    .join('\n');
}

function toolName(call: unknown): string {
  if (!call || typeof call !== 'object') return '';
  const fn = (call as { function?: { name?: unknown } }).function;
  return typeof fn?.name === 'string' ? fn.name : '';
}

function toolArgs(call: unknown): string {
  if (!call || typeof call !== 'object') return '';
  const fn = (call as { function?: { arguments?: unknown } }).function;
  return typeof fn?.arguments === 'string' ? fn.arguments : '';
}

/**
 * Build a tool-aware transcript. The reflection model needs to see which
 * commands/files were used and whether they succeeded, not just chat text.
 */
export function buildReflectionTranscript(messages: MessageDTO[]): string {
  const lines: string[] = [];
  for (const message of messages) {
    if (message.role === 'assistant') {
      const content = contentText(message).trim();
      if (content) lines.push(`[assistant] ${content}`);
      for (const call of message.tool_calls ?? []) {
        lines.push(
          `[tool_call name=${toolName(call)} args=${toolArgs(call).slice(0, 800)}]`,
        );
      }
      continue;
    }
    if (message.role === 'tool') {
      const content = contentText(message).trim();
      const failed = /^\s*Error:/i.test(content);
      lines.push(
        `[tool_result id=${String(message.tool_call_id ?? '')} ${failed ? 'error' : 'ok'}] ${content.slice(0, 1_200)}`,
      );
      continue;
    }
    const content = contentText(message).trim();
    if (content) lines.push(`[${message.role}] ${content}`);
  }
  return lines.join('\n');
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
    const transcript = buildReflectionTranscript(input.messages.slice(-20));
    const messages: MessageDTO[] = [
      { role: 'system', content: prompt },
      {
        role: 'user',
        content:
          'Conversation episode (tool-aware transcript):\n' +
          transcript +
          '\n\nExtract only reusable execution workflows. Output only JSON.',
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