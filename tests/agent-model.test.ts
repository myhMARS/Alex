import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import { TuiModule } from '../src/modules/tui/module';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import type { ChatModel, ChatRequest, ChatStreamEvent, ToolCall } from '../src/llm/client';
import {
  TokenEmitted,
  TokenUsageReported,
  TurnCompleted,
  TurnDequeued,
  TurnFailed,
  TurnQueued,
  TurnStarted,
  UserTurnRequested,
} from '../src/kernel/contracts/chat';
import { ToolFinished, ToolStarted } from '../src/kernel/contracts/tools';
import { SettingsUpdated, type SettingsSnapshot } from '../src/kernel/contracts/settings';
import { GetSessionProjection } from '../src/kernel/contracts/session';
import { RuntimeContextNotice } from '../src/kernel/contracts/prompt';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

function toolCall(id: string, name: string, args: string): ToolCall {
  return { id, type: 'function', function: { name, arguments: args } };
}

class ScriptedModel implements ChatModel {
  readonly calls: ChatRequest[] = [];

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    // Snapshot: the agent keeps mutating its messages array between rounds.
    this.calls.push({ ...request, messages: [...request.messages] });
    if (this.calls.length === 1) {
      yield { type: 'content', delta: 'Checking. ' };
      yield {
        type: 'done',
        result: {
          content: 'Checking. ',
          thinking: 'need the clock',
          toolCalls: [toolCall('call_1', 'time', '{"timezone":"UTC"}')],
          finishReason: 'tool_calls',
        },
      };
      return;
    }
    yield { type: 'content', delta: 'It is now. ' };
    yield {
      type: 'done',
      result: {
        content: 'It is now. ',
        thinking: 'done',
        toolCalls: [],
        finishReason: 'stop',
        usage: { promptTokens: 120, completionTokens: 8, totalTokens: 128 },
      },
    };
  }
}

class DelayedModel implements ChatModel {
  async *streamChat(): AsyncIterable<ChatStreamEvent> {
    await new Promise((resolve) => setTimeout(resolve, 40));
    yield { type: 'content', delta: 'delayed answer' };
    yield {
      type: 'done',
      result: { content: 'delayed answer', thinking: '', toolCalls: [], finishReason: 'stop' },
    };
  }
}

async function setup(model: ChatModel, options: { maxIterations?: number } = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-agent-loop-'));
  tempDirs.push(dir);
  const bus = new AsyncEventBus();
  const session = new SessionModule();
  const tools = new ToolsModule({
    roots: [dir],
    policy: new PermissionPolicy({ allowed: ['read', 'network'], env: {} }),
    audit: new AuditLogger(join(dir, 'audit.jsonl')),
    approvalTimeoutMs: 1_000,
    plugins: false,
  });
  const agent = new AgentModule({
    model,
    systemPrompt: 'SYS',
    maxIterations: options.maxIterations ?? 5,
  });
  const tui = new TuiModule();
  await session.start(bus);
  await tools.start(bus);
  await agent.start(bus);
  await tui.start(bus);
  await bus.start();
  return { bus, agent, tui, dir };
}

describe('AgentModule model loop', () => {
  test('streams, executes a tool, feeds the result back and finishes', async () => {
    const model = new ScriptedModel();
    const { bus, agent, tui } = await setup(model);

    const tokens: string[] = [];
    const started: ToolStarted[] = [];
    const finished: ToolFinished[] = [];
    const completed: TurnCompleted[] = [];
    const usage: TokenUsageReported[] = [];
    await bus.subscribe(TokenEmitted, (event) => { tokens.push(event.delta); });
    await bus.subscribe(ToolStarted, (event) => { started.push(event); });
    await bus.subscribe(ToolFinished, (event) => { finished.push(event); });
    await bus.subscribe(TurnCompleted, (event) => { completed.push(event); });
    await bus.subscribe(TokenUsageReported, (event) => { usage.push(event); });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'what time is it' }));
    await agent.whenIdle();
    await bus.idle();

    expect(model.calls).toHaveLength(2);
    const first = model.calls[0]!;
    expect(first.systemPrompt).toBe('SYS');
    expect(first.messages.at(-1)).toMatchObject({ role: 'user', content: 'what time is it' });
    expect(first.tools?.map((tool) => tool.function.name)).toEqual(
      expect.arrayContaining(['time', 'read', 'write']),
    );

    const second = model.calls[1]!;
    const assistantWithCalls = second.messages.find(
      (message) => message.role === 'assistant' && Array.isArray(message.tool_calls) && message.tool_calls.length > 0,
    );
    expect(assistantWithCalls).toBeTruthy();
    const toolMessage = second.messages.find((message) => message.role === 'tool');
    expect(toolMessage?.tool_call_id).toBe('call_1');
    expect(String(toolMessage?.content)).toContain('Current time');

    expect(tokens.join('')).toBe('Checking. It is now. ');
    expect(started).toHaveLength(1);
    expect(started[0]?.toolName).toBe('time');
    expect(finished).toHaveLength(1);
    expect(completed[0]?.content).toBe('It is now. ');
    expect(usage[0]).toMatchObject({ promptTokens: 120, completionTokens: 8, totalTokens: 128 });
    expect(tui.viewState.lastUsage?.promptTokens).toBe(120);

    const history = (
      await bus.request(new GetSessionProjection({ sessionId: 's1' }))
    ).messages;
    expect(history.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
    ]);

    // The view keeps all streamed segments in order (pre-tool + post-tool).
    expect(tui.viewState.turns[0]?.content).toBe('Checking. It is now. ');
    expect(tui.viewState.turns[0]?.parts.map((part) => part.kind)).toEqual([
      'thinking',
      'text',
      'tool',
      'text',
    ]);
    expect(tui.viewState.turns[0]?.tools[0]?.name).toBe('time');
    expect(tui.viewState.turns[0]?.tools[0]?.done).toBe(true);

    await bus.shutdown();
  }, 20_000);

  test('publishes TurnFailed when the model throws', async () => {
    const broken: ChatModel = {
      async *streamChat() {
        throw new Error('model down');
      },
    };
    const { bus, agent } = await setup(broken);
    const failures: TurnFailed[] = [];
    await bus.subscribe(TurnFailed, (event) => { failures.push(event); });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'hi' }));
    await agent.whenIdle();
    await bus.idle();

    expect(failures[0]?.error).toContain('model down');
    await bus.shutdown();
  }, 20_000);

  test('forces a final answer after max iterations', async () => {
    class LoopModel implements ChatModel {
      calls = 0;

      async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
        this.calls += 1;
        if (request.tools && request.tools.length > 0) {
          yield {
            type: 'done',
            result: {
              content: '',
              thinking: '',
              toolCalls: [toolCall('call_x', 'time', '{"timezone":"UTC"}')],
              finishReason: 'tool_calls',
            },
          };
          return;
        }
        yield { type: 'content', delta: 'forced final' };
        yield {
          type: 'done',
          result: { content: 'forced final', thinking: '', toolCalls: [], finishReason: 'stop' },
        };
      }
    }

    const alwaysTools = new LoopModel();
    const { bus, agent } = await setup(alwaysTools, { maxIterations: 2 });
    const completed: TurnCompleted[] = [];
    await bus.subscribe(TurnCompleted, (event) => { completed.push(event); });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'loop' }));
    await agent.whenIdle();
    await bus.idle();

    expect(completed[0]?.content).toBe('forced final');
    expect(alwaysTools.calls).toBe(3);
    await bus.shutdown();
  }, 20_000);

  test('emits queued/dequeued events for messages sent during a turn', async () => {
    const { bus, agent } = await setup(new DelayedModel());
    const queued: TurnQueued[] = [];
    const dequeued: TurnDequeued[] = [];
    await bus.subscribe(TurnQueued, (event) => queued.push(event));
    await bus.subscribe(TurnDequeued, (event) => dequeued.push(event));
    bus.publish(new UserTurnRequested({ sessionId: 's-queue', userText: 'first' }));
    bus.publish(new UserTurnRequested({ sessionId: 's-queue', userText: 'second' }));
    await agent.whenIdle();
    await bus.idle();
    expect(queued).toHaveLength(1);
    expect(queued[0]?.userInput).toBe('second');
    expect(dequeued[0]?.queueId).toBe(queued[0]?.queueId);
    await bus.shutdown();
  });

  test('injects runtime context notices into the next model request', async () => {
    const model = new ScriptedModel();
    const { bus, agent } = await setup(model);
    bus.publish(
      new RuntimeContextNotice({
        key: 'mcp:demo',
        source: 'mcp',
        text: "MCP server 'demo' is now available.",
      }),
    );
    await bus.idle();
    bus.publish(new UserTurnRequested({ sessionId: 's-notice', userText: 'hello' }));
    await agent.whenIdle();
    await bus.idle();
    expect(model.calls[0]?.systemPrompt).not.toContain('Runtime context');
    const last = model.calls[0]?.messages.at(-1) as { role?: string; content?: unknown };
    expect(last?.role).toBe('user');
    expect(String(last?.content)).toContain('<system-reminder>');
    expect(String(last?.content)).toContain("MCP server 'demo' is now available.");
    await bus.shutdown();
  });

  test('applies maxIterations changes from settings at runtime', async () => {
    class LoopModel implements ChatModel {
      calls = 0;

      async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
        this.calls += 1;
        if (request.tools && request.tools.length > 0) {
          yield {
            type: 'done',
            result: {
              content: '',
              thinking: '',
              toolCalls: [toolCall('call_runtime', 'time', '{"timezone":"UTC"}')],
              finishReason: 'tool_calls',
            },
          };
          return;
        }
        yield { type: 'content', delta: 'runtime final' };
        yield {
          type: 'done',
          result: { content: 'runtime final', thinking: '', toolCalls: [], finishReason: 'stop' },
        };
      }
    }

    const model = new LoopModel();
    const { bus, agent } = await setup(model, { maxIterations: 8 });
    bus.publish(
      new SettingsUpdated({
        settings: { maxIterations: 2 } as SettingsSnapshot,
      }),
    );
    await bus.idle();
    bus.publish(new UserTurnRequested({ sessionId: 's-runtime', userText: 'loop' }));
    await agent.whenIdle();
    await bus.idle();
    expect(model.calls).toBe(3);
    await bus.shutdown();
  }, 20_000);
});