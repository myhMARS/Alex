/**
 * OpenAI-compatible streaming chat client — TypeScript port of
 * alex/llm/client.py.
 *
 * Emits content / thinking deltas and accumulates streamed tool-call deltas
 * (OpenAI sends them incrementally by index).
 */

import type { MessageDTO } from '../kernel/dto/message';
import type { LLMConfig } from './config';

export interface ToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

export interface ToolSchema {
  type: 'function';
  function: { name: string; description: string; parameters: Record<string, unknown> };
}

export interface ChatUsage {
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
  /** Prompt tokens served from cache (DeepSeek / OpenAI-compatible). */
  cacheHitTokens?: number;
  /** Prompt tokens that missed the cache. */
  cacheMissTokens?: number;
}

export interface ChatResult {
  content: string;
  thinking: string;
  toolCalls: ToolCall[];
  finishReason: string;
  usage?: ChatUsage;
}

export interface ChatRequest {
  messages: MessageDTO[];
  tools?: ToolSchema[];
  systemPrompt?: string;
  temperature?: number;
  maxTokens?: number;
  thinking?: boolean;
  effort?: 'low' | 'high' | 'max';
}

export type ChatStreamEvent =
  | { type: 'content'; delta: string }
  | { type: 'thinking'; delta: string }
  | { type: 'tool_calls'; toolCalls: ToolCall[] }
  | { type: 'done'; result: ChatResult };

/** Minimal model interface so the agent can be tested without HTTP. */
export interface ChatModel {
  streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent>;
}

export interface ChatClientOptions {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

interface ToolCallAccumulator {
  id: string;
  name: string;
  arguments: string;
}

interface ChunkDelta {
  content?: unknown;
  reasoning_content?: unknown;
  reasoning?: unknown;
  additional_kwargs?: { reasoning_content?: unknown };
  tool_calls?: Array<{
    index?: number;
    id?: string;
    type?: string;
    function?: { name?: string; arguments?: string };
  }>;
}

interface StreamChunk {
  choices?: Array<{
    delta?: ChunkDelta;
    finish_reason?: string | null;
    message?: ChunkDelta;
  }>;
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
    prompt_cache_hit_tokens?: number;
    prompt_cache_miss_tokens?: number;
    prompt_tokens_details?: { cached_tokens?: number };
  };
}

export class ChatClient implements ChatModel {
  private readonly config: LLMConfig;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(config: LLMConfig, options: ChatClientOptions = {}) {
    this.config = config;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = options.timeoutMs ?? config.timeoutMs ?? 180_000;
  }

  get model(): string {
    return this.config.model;
  }

  async *streamChat(request: ChatRequest): AsyncGenerator<ChatStreamEvent> {
    const messages = this.buildMessages(request);
    const body: Record<string, unknown> = {
      model: this.config.model,
      messages,
      stream: true,
      stream_options: { include_usage: true },
      temperature: request.temperature ?? this.config.temperature,
      max_tokens: request.maxTokens ?? this.config.maxTokens,
    };
    if (request.thinking === false) {
      body.thinking = { type: 'disabled' };
    } else if (request.thinking === true) {
      body.thinking = { type: 'enabled' };
      if (request.effort) body.reasoning_effort = request.effort;
    } else if (request.effort) {
      body.reasoning_effort = request.effort;
    }
    if (request.tools && request.tools.length > 0) {
      body.tools = request.tools;
      body.tool_choice = 'auto';
    }

    const response = await this.fetchImpl(`${this.config.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${this.config.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (!response.ok) {
      const detail = await response.text().catch(() => '');
      throw new Error(
        `LLM request failed (HTTP ${response.status}): ${detail.slice(0, 500) || response.statusText}`,
      );
    }
    if (!response.body) throw new Error('LLM response has no body');

    const state = {
      content: '',
      thinking: '',
      toolCalls: new Map<number, ToolCallAccumulator>(),
      finishReason: '',
      usage: undefined as ChatResult['usage'],
    };

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    const handleLine = (line: string): ChatStreamEvent[] => {
      if (!line.startsWith('data:')) return [];
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') return [];
      let chunk: StreamChunk;
      try {
        chunk = JSON.parse(payload) as StreamChunk;
      } catch {
        return [];
      }
      const events: ChatStreamEvent[] = [];
      if (chunk.usage) {
        const prompt = chunk.usage.prompt_tokens ?? 0;
        const cacheHit =
          chunk.usage.prompt_cache_hit_tokens ??
          chunk.usage.prompt_tokens_details?.cached_tokens ??
          0;
        const cacheMiss =
          chunk.usage.prompt_cache_miss_tokens ??
          (prompt > 0 ? Math.max(0, prompt - cacheHit) : 0);
        state.usage = {
          promptTokens: chunk.usage.prompt_tokens,
          completionTokens: chunk.usage.completion_tokens,
          totalTokens: chunk.usage.total_tokens,
          cacheHitTokens: cacheHit,
          cacheMissTokens: cacheMiss,
        };
      }
      const choice = chunk.choices?.[0];
      if (!choice) return events;
      const delta = choice.delta ?? choice.message ?? {};
      if (typeof delta.content === 'string' && delta.content) {
        state.content += delta.content;
        events.push({ type: 'content', delta: delta.content });
      }
      const reasoning =
        typeof delta.reasoning_content === 'string'
          ? delta.reasoning_content
          : typeof delta.reasoning === 'string'
            ? delta.reasoning
            : typeof delta.additional_kwargs?.reasoning_content === 'string'
              ? delta.additional_kwargs.reasoning_content
              : '';
      if (reasoning) {
        state.thinking += reasoning;
        events.push({ type: 'thinking', delta: reasoning });
      }
      if (Array.isArray(delta.tool_calls)) {
        for (const [position, call] of delta.tool_calls.entries()) {
          const index = typeof call.index === 'number' ? call.index : position;
          const current = state.toolCalls.get(index) ?? { id: '', name: '', arguments: '' };
          if (call.id) current.id = call.id;
          if (call.function?.name) current.name += call.function.name;
          if (call.function?.arguments) current.arguments += call.function.arguments;
          state.toolCalls.set(index, current);
        }
      }
      if (choice.finish_reason) state.finishReason = choice.finish_reason;
      return events;
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf('\n');
      while (newline >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, '');
        buffer = buffer.slice(newline + 1);
        for (const event of handleLine(line)) yield event;
        newline = buffer.indexOf('\n');
      }
    }
    if (buffer.trim()) {
      for (const event of handleLine(buffer.trim())) yield event;
    }

    const toolCalls: ToolCall[] = [...state.toolCalls.entries()]
      .sort(([a], [b]) => a - b)
      .map(([index, call]) => ({
        id: call.id || `call_${index}`,
        type: 'function' as const,
        function: { name: call.name, arguments: call.arguments || '{}' },
      }));
    if (toolCalls.length > 0) {
      yield { type: 'tool_calls', toolCalls };
    }
    yield {
      type: 'done',
      result: {
        content: state.content,
        thinking: state.thinking,
        toolCalls,
        finishReason: state.finishReason,
        usage: state.usage,
      },
    };
  }

  private buildMessages(request: ChatRequest): MessageDTO[] {
    const messages = [...request.messages];
    if (request.systemPrompt && messages[0]?.role !== 'system') {
      messages.unshift({ role: 'system', content: request.systemPrompt });
    }
    return messages;
  }
}