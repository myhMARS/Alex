/** DSH-style heuristic token pricing for compaction and context sizing. */

import type { MessageDTO } from '../kernel/dto/message';

export const CHARS_PER_TOKEN = 4;
export const BLOCK_OVERHEAD = 4;
export const ROLE_OVERHEAD = 4;

export function estimateTextTokens(text: string): number {
  return textTokens(text);
}

/** Heuristic price of the tool-schema JSON (same shape as DSH estimateToolsTokens). */
export function estimateToolsTokens(tools: unknown): number {
  if (!tools) return 0;
  const json = JSON.stringify(tools);
  if (!json || json === '[]') return 0;
  return textTokens(json) + BLOCK_OVERHEAD;
}

function textTokens(text: string): number {
  let narrow = 0;
  let wide = 0;
  for (const char of text) {
    if ((char.codePointAt(0) ?? 0) <= 0x7f) narrow += 1;
    else wide += 1;
  }
  return Math.ceil(narrow / CHARS_PER_TOKEN + wide / 1.5);
}

export function estimateMessageTokens(message: MessageDTO): number {
  let tokens = ROLE_OVERHEAD;
  if (typeof message.content === 'string' && message.content.length > 0) {
    tokens += textTokens(message.content) + BLOCK_OVERHEAD;
  } else if (Array.isArray(message.content) && message.content.length > 0) {
    for (const part of message.content as Array<Record<string, unknown>>) {
      if (part.type === 'text' && typeof part.text === 'string') {
        tokens += textTokens(part.text) + BLOCK_OVERHEAD;
      } else if (part.type === 'image_url') {
        // Fixed conservative image price (provider routing owns exact pricing).
        tokens += 1_024;
      } else {
        tokens += BLOCK_OVERHEAD + textTokens(JSON.stringify(part));
      }
    }
  }
  if (message.reasoning_content) {
    tokens += textTokens(message.reasoning_content) + BLOCK_OVERHEAD;
  }
  for (const call of message.tool_calls ?? []) {
    tokens +=
      Math.ceil((call.function?.name ?? '').length / CHARS_PER_TOKEN) +
      Math.ceil((call.function?.arguments ?? '').length / CHARS_PER_TOKEN) +
      BLOCK_OVERHEAD;
  }
  return Math.max(1, tokens);
}