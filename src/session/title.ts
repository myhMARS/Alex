/** Shared one-shot session title generation. */

import type { ChatModel } from '../llm/client';

export function normalizeGeneratedTitle(raw: string): string {
  let title = raw.replace(/\s+/g, ' ').trim();
  title = title.replace(/^title\s*[:：]\s*/i, '');
  title = title.replace(/^[\s"'`“”‘’#*]+|[\s"'`“”‘’]+$/g, '');
  title = title.replace(/[。.!！?？,，;；:：]+$/g, '').trim();
  return title.length > 48 ? `${title.slice(0, 47).trimEnd()}…` : title;
}

/** Generate a short title; returns null on empty output or any model error. */
export async function generateSessionTitle(
  model: ChatModel,
  userText: string,
  assistantText: string,
): Promise<string | null> {
  const user = userText.replace(/\s+/g, ' ').trim().slice(0, 1_200);
  const assistant = assistantText.replace(/\s+/g, ' ').trim().slice(0, 1_800);
  if (!user) return null;

  let raw = '';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15_000);
  try {
    for await (const event of model.streamChat({
      messages: [
        {
          role: 'user',
          content: `First user message:\n${user}\n\nAssistant reply:\n${assistant}`,
        },
      ],
      systemPrompt:
        'Generate a concise title for this conversation. Use the same language as the user. ' +
        'Return only the title: no quotes, no markdown, no trailing punctuation. ' +
        'Aim for 3-8 words and at most 40 characters.',
      thinking: false,
      temperature: 0.2,
      maxTokens: 48,
      signal: controller.signal,
    })) {
      if (event.type === 'content') raw += event.delta;
    }
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
  const title = normalizeGeneratedTitle(raw);
  return title || null;
}
