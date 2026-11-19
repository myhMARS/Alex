import { afterEach, describe, expect, test } from 'bun:test';

import { ChatClient, type ChatStreamEvent } from '../src/llm/client';
import { loadLLMConfig, loadDotEnv } from '../src/llm/config';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

const SSE_BODY = [
  ': keep-alive',
  'data: {"choices":[{"delta":{"reasoning_content":"Thinking "}}]}',
  'data: {"choices":[{"delta":{"content":"Hello "}}]}',
  'data: {"choices":[{"delta":{"content":"world"}}]}',
  'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"time","arguments":"{\\"timezone\\":"}}]}}]}',
  'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"UTC\\"}"}}]}}]}',
  'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
  'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"prompt_cache_hit_tokens":8,"prompt_cache_miss_tokens":2}}',
  'data: [DONE]',
  '',
].join('\n');

function sseResponse(body: string, chunkSize = 7): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let index = 0; index < body.length; index += chunkSize) {
        controller.enqueue(encoder.encode(body.slice(index, index + chunkSize)));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    headers: { 'content-type': 'text/event-stream; charset=utf-8' },
  });
}

describe('LLM config', () => {
  test('defaults and overrides', () => {
    const config = loadLLMConfig({});
    expect(config.provider).toBe('deepseek');
    expect(config.baseUrl).toBe('https://api.deepseek.com');
    expect(config.model).toBe('deepseek-chat');
    expect(config.maxTokens).toBe(4096);

    const custom = loadLLMConfig(
      { ALEX_PROVIDER: 'openai', ALEX_BASE_URL: 'https://api.openai.com/v1/', ALEX_MODEL: 'gpt-x', ALEX_API_KEY: 'k', ALEX_MAX_TOKENS: '123', ALEX_TEMPERATURE: '0.5' },
    );
    expect(custom.provider).toBe('openai');
    expect(custom.baseUrl).toBe('https://api.openai.com/v1');
    expect(custom.model).toBe('gpt-x');
    expect(custom.apiKey).toBe('k');
    expect(custom.maxTokens).toBe(123);
    expect(custom.temperature).toBe(0.5);
  });

  test('loads .env without overriding existing env', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-env-'));
    tempDirs.push(dir);
    const path = join(dir, '.env');
    await writeFile(path, '# comment\nexport ALEX_TS_TEST_KEY=from-file\nALEX_TS_SECOND="quoted"\n', 'utf8');
    delete process.env.ALEX_TS_TEST_KEY;
    delete process.env.ALEX_TS_SECOND;
    const loaded = await loadDotEnv([path]);
    expect(loaded).toEqual([path]);
    expect(String(process.env.ALEX_TS_TEST_KEY)).toBe('from-file');
    expect(String(process.env.ALEX_TS_SECOND)).toBe('quoted');
    delete process.env.ALEX_TS_TEST_KEY;
    delete process.env.ALEX_TS_SECOND;
  });
});

describe('ChatClient streaming', () => {
  test('parses content, thinking, tool-call deltas and usage across split chunks', async () => {
    let capturedBody: any = null;
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        capturedBody = await request.json();
        return sseResponse(SSE_BODY);
      },
    });
    try {
      const client = new ChatClient(
        loadLLMConfig({}, { baseUrl: `http://127.0.0.1:${server.port}`, apiKey: 'test-key', model: 'mock' }),
      );
      const events: ChatStreamEvent[] = [];
      for await (const event of client.streamChat({
        messages: [{ role: 'user', content: 'hi' }],
        systemPrompt: 'SYS',
        thinking: true,
        effort: 'max',
        tools: [
          {
            type: 'function',
            function: { name: 'time', description: 'time', parameters: { type: 'object', properties: {} } },
          },
        ],
      })) {
        events.push(event);
      }

      expect(capturedBody.stream).toBe(true);
      expect(capturedBody.stream_options).toEqual({ include_usage: true });
      expect(capturedBody.thinking).toEqual({ type: 'enabled' });
      expect(capturedBody.reasoning_effort).toBe('max');
      expect(capturedBody.messages[0]).toEqual({ role: 'system', content: 'SYS' });
      expect(capturedBody.tools[0].function.name).toBe('time');

      const content = events.filter((event) => event.type === 'content').map((event) => (event as any).delta).join('');
      const thinking = events.filter((event) => event.type === 'thinking').map((event) => (event as any).delta).join('');
      expect(content).toBe('Hello world');
      expect(thinking).toBe('Thinking ');

      const done = events.find((event) => event.type === 'done') as any;
      expect(done.result.toolCalls).toHaveLength(1);
      expect(done.result.toolCalls[0].function.name).toBe('time');
      expect(done.result.toolCalls[0].function.arguments).toBe('{"timezone":"UTC"}');
      expect(done.result.finishReason).toBe('tool_calls');
      expect(done.result.usage.totalTokens).toBe(15);
      expect(done.result.usage.promptTokens).toBe(10);
      expect(done.result.usage.cacheHitTokens).toBe(8);
      expect(done.result.usage.cacheMissTokens).toBe(2);

      for await (const _event of client.streamChat({
        messages: [{ role: 'user', content: 'again' }],
        thinking: false,
      })) {
        // consume
      }
      expect(capturedBody.thinking).toEqual({ type: 'disabled' });
      expect(capturedBody.reasoning_effort).toBeUndefined();
    } finally {
      server.stop(true);
    }
  });

  test('converts DSML content into tool calls and hides the raw markup', async () => {
    const dsml = `<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="pwsh">
<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem -Force</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`;
    const chunks = [dsml.slice(0, 12), dsml.slice(12, 55), dsml.slice(55)];
    const body = [
      ...chunks.map(
        (chunk) =>
          `data: ${JSON.stringify({ choices: [{ delta: { content: chunk } }] })}`,
      ),
      'data: [DONE]',
      '',
    ].join('\n');
    const server = Bun.serve({
      port: 0,
      fetch() {
        return sseResponse(body, 5);
      },
    });
    try {
      const client = new ChatClient(
        loadLLMConfig({}, {
          baseUrl: `http://127.0.0.1:${server.port}`,
          apiKey: 'test-key',
          model: 'mock',
        }),
      );
      const events: ChatStreamEvent[] = [];
      for await (const event of client.streamChat({
        messages: [{ role: 'user', content: 'hi' }],
      })) {
        events.push(event);
      }
      const content = events
        .filter((event) => event.type === 'content')
        .map((event) => (event as { delta: string }).delta)
        .join('');
      expect(content).not.toContain('invoke');
      expect(content).not.toContain('calls>');
      const done = events.find((event) => event.type === 'done') as {
        result: { toolCalls: Array<{ function: { name: string; arguments: string } }> };
      };
      expect(done.result.toolCalls).toHaveLength(1);
      expect(done.result.toolCalls[0]?.function.name).toBe('pwsh');
      expect(JSON.parse(done.result.toolCalls[0]!.function.arguments)).toEqual({
        command: 'Get-ChildItem -Force',
      });
    } finally {
      server.stop(true);
    }
  });

  test('throws on non-2xx responses', async () => {
    const server = Bun.serve({
      port: 0,
      fetch() {
        return new Response('bad key', { status: 401 });
      },
    });
    try {
      const client = new ChatClient(
        loadLLMConfig({}, { baseUrl: `http://127.0.0.1:${server.port}`, apiKey: 'nope' }),
      );
      await expect(async () => {
        for await (const _event of client.streamChat({ messages: [{ role: 'user', content: 'x' }] })) {
          // consume
        }
      }).toThrow(/HTTP 401/);
    } finally {
      server.stop(true);
    }
  });
});