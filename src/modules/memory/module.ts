/** In-memory conversation memory — TypeScript port of alex/memory. */

import type { Module } from '../../kernel/runtime';
import type { AsyncEventBus } from '../../kernel/bus';
import type { MessageDTO } from '../../kernel/dto/message';
import {
  AppendMessages,
  ClearMemory,
  GetContext,
  GetMemoryStatus,
  ReplaceMemory,
} from '../../kernel/contracts/memory';

export interface BufferMemoryOptions {
  /** Optional message cap; Infinity by default (token budget governs). */
  maxMessages?: number;
  /** Approximate token budget (0 = disabled). */
  maxTokens?: number;
}

export interface MemoryModuleOptions {
  maxMessages?: number;
  maxContextTokens?: number;
  reserveOutputTokens?: number;
}

/**
 * Heuristic token pricing modeled after DSH's token meter:
 * 4 characters/token for narrow text, 4 tokens per content block and
 * 4 tokens role framing. DSH documents that 4 chars/token underestimates
 * CJK, so wide characters are priced at 1.5 chars/token here.
 */
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

/** Sliding-window, per-session message store. */
export class BufferMemory {
  private readonly sessions = new Map<string, MessageDTO[]>();
  private readonly maxMessages: number;
  private readonly maxTokens: number;

  constructor(options: BufferMemoryOptions = {}) {
    this.maxMessages =
      options.maxMessages === undefined
        ? Number.POSITIVE_INFINITY
        : Math.max(1, options.maxMessages);
    this.maxTokens = Math.max(0, options.maxTokens ?? 0);
  }

  get windowSize(): number {
    return this.maxMessages;
  }

  get maxTokenBudget(): number {
    return this.maxTokens;
  }

  get(sessionId: string): MessageDTO[] {
    const stored = this.sessions.get(sessionId) ?? [];
    let selected = Number.isFinite(this.maxMessages)
      ? stored.slice(-this.maxMessages)
      : [...stored];
    if (this.maxTokens > 0) {
      // Keep the newest messages that fit the approximate token budget.
      const fitted: MessageDTO[] = [];
      let used = 0;
      for (let index = selected.length - 1; index >= 0; index -= 1) {
        const message = selected[index]!;
        const cost = estimateMessageTokens(message);
        if (fitted.length > 0 && used + cost > this.maxTokens) break;
        fitted.unshift(message);
        used += cost;
      }
      selected = fitted;
    }
    return selected.map((message) => ({ ...message }));
  }

  estimatedTokens(sessionId: string): number {
    return this.get(sessionId).reduce(
      (sum, message) => sum + estimateMessageTokens(message),
      0,
    );
  }

  append(sessionId: string, messages: MessageDTO[]): void {
    if (!sessionId) return;
    const next = [...(this.sessions.get(sessionId) ?? []), ...messages];
    if (next.length > this.maxMessages) {
      next.splice(0, next.length - this.maxMessages);
    }
    this.sessions.set(sessionId, next);
  }

  replace(sessionId: string, messages: MessageDTO[]): void {
    if (!sessionId) return;
    const next = Number.isFinite(this.maxMessages)
      ? messages.slice(-this.maxMessages)
      : [...messages];
    this.sessions.set(sessionId, next);
  }

  clear(sessionId: string): void {
    this.sessions.delete(sessionId);
  }

  sessionIds(): string[] {
    return [...this.sessions.keys()];
  }
}

/** Bus module exposing memory operations. */
export class MemoryModule implements Module {
  readonly name = 'memory';
  readonly dependencies: readonly string[] = [];
  readonly memory: BufferMemory;
  private readonly contextWindowTokens: number;

  constructor(memoryOrOptions?: BufferMemory | MemoryModuleOptions) {
    if (memoryOrOptions instanceof BufferMemory) {
      this.memory = memoryOrOptions;
      this.contextWindowTokens = 0;
    } else {
      const options = memoryOrOptions ?? {};
      this.contextWindowTokens = options.maxContextTokens ?? 0;
      this.memory = new BufferMemory({
        // Token budget is primary when configured; the message cap is a safety net.
        maxMessages: options.maxMessages,
        maxTokens:
          options.maxContextTokens !== undefined
            ? Math.max(
                1_000,
                options.maxContextTokens - (options.reserveOutputTokens ?? 4_096),
              )
            : 0,
      });
    }
  }

  async start(bus: AsyncEventBus): Promise<void> {
    bus.provide(GetContext, (req) => this.memory.get(req.sessionId));
    bus.provide(GetMemoryStatus, (req) => ({
      messageCount: this.memory.get(req.sessionId).length,
      maxTokens: this.memory.maxTokenBudget,
      estimatedTokens: this.memory.estimatedTokens(req.sessionId),
      maxContextTokens: this.contextWindowTokens,
    }));
    bus.provide(AppendMessages, (req) => {
      this.memory.append(req.sessionId, req.messages);
    });
    bus.provide(ReplaceMemory, (req) => {
      this.memory.replace(req.sessionId, req.messages);
    });
    bus.provide(ClearMemory, (req) => {
      this.memory.clear(req.sessionId);
    });
  }
}