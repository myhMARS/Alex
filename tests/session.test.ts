import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { StoreModule } from '../src/modules/store/module';
import { TuiModule } from '../src/modules/tui/module';
import { SessionModule } from '../src/session/module';
import {
  projectSessionEvents,
  projectSessionSurface,
  messageEvent,
  replaceMessagesEvent,
} from '../src/session/projector';
import { GetToolCatalog } from '../src/kernel/contracts/tools';
import { TokenUsageReported } from '../src/kernel/contracts/chat';
import {
  AppendSessionEvents,
  ForkSession,
  ListSessions,
  GetSessionProjection,
  LoadSession,
  type SessionEventRecord,
} from '../src/kernel/contracts/session';
import { UserTurnRequested } from '../src/kernel/contracts/chat';
import { assistantMessage, userMessage } from '../src/kernel/dto/message';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-session-'));
  tempDirs.push(dir);
  return dir;
}

class OneShotModel implements ChatModel {
  readonly calls: ChatRequest[] = [];

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls.push({ ...request, messages: [...request.messages] });
    yield {
      type: 'done',
      result: {
        content: 'projected answer',
        thinking: '',
        toolCalls: [],
        finishReason: 'stop',
        usage: { promptTokens: 12, completionTokens: 3, totalTokens: 15 },
      },
    };
  }
}

describe('session projector', () => {
  test('projects message events and applies replacements', () => {
    const events: SessionEventRecord[] = [
      messageEvent('user_message', userMessage('one')),
      messageEvent('assistant_message', assistantMessage('two')),
      replaceMessagesEvent([userMessage('summary'), assistantMessage('tail')]),
      messageEvent('user_message', userMessage('three')),
    ];
    const messages = projectSessionEvents(events);
    expect(messages.map((message) => message.content)).toEqual([
      'summary',
      'tail',
      'three',
    ]);
    const raw = projectSessionEvents(events, { applyCompaction: false });
    expect(raw.map((message) => message.content)).toEqual(['one', 'two', 'three']);
  });

  test('projects turn and request-header surface', () => {
    const events: SessionEventRecord[] = [
      {
        seq: 1,
        type: 'turn_started',
        at: '2026-01-01T00:00:00Z',
        data: { turnId: 't1', userInput: 'hello', kind: 'user' },
      },
      {
        seq: 2,
        type: 'request_header',
        at: '2026-01-01T00:00:01Z',
        data: { systemPrompt: 'SYS', tools: ['read'], effort: 'high' },
      },
    ];
    const surface = projectSessionSurface(events);
    expect(surface.turns[0]?.turnId).toBe('t1');
    expect(surface.lastRequestHeader?.systemPrompt).toBe('SYS');
  });

  test('SessionModule appends events and exposes projections', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    await session.start(bus);
    await bus.start();
    const events = [
      messageEvent('user_message', userMessage('hello')),
      {
        seq: 0,
        type: 'plugin_record',
        at: '2026-01-01T00:00:00Z',
        data: { plugin: 'demo', value: 1 },
      },
    ];
    await bus.request(
      new AppendSessionEvents({ sessionId: 's1', events }),
    );
    const projection = await bus.request(
      new GetSessionProjection({ sessionId: 's1' }),
    );
    expect(projection.messages[0]?.content).toBe('hello');
    expect(projection.events).toHaveLength(2);
    expect(projection.customEvents).toHaveLength(1);
    expect(projection.customEvents[0]?.type).toBe('plugin_record');
    await bus.shutdown();
  });
});

describe('event-sourced persistence', () => {
  test('persists sidecar events immediately without waiting for TurnCompleted', async () => {
    const dir = await tempDir();
    const sessionsDir = join(dir, 'sessions');
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const store = new StoreModule({ sessionsDir });
    await session.start(bus);
    await store.start(bus);
    await bus.start();
    bus.publish(
      new TokenUsageReported({
        sessionId: 's1',
        turnId: 't1',
        promptTokens: 10,
        completionTokens: 2,
        totalTokens: 12,
      }),
    );
    await bus.idle();
    const lines = (await readFile(join(sessionsDir, 's1.jsonl'), 'utf8'))
      .trim()
      .split('\n');
    const events = lines.slice(1).map((line) => JSON.parse(line));
    expect(events.some((event: { type: string }) => event.type === 'usage')).toBe(
      true,
    );
    await bus.shutdown();
  });
});

describe('event-sourced resume', () => {
  test('agent appends events and resume projects them without using the messages snapshot', async () => {
    const dir = await tempDir();
    const sessionsDir = join(dir, 'sessions');

    const bus1 = new AsyncEventBus();
    bus1.provide(GetToolCatalog, () => []);
    const store1 = new StoreModule({ sessionsDir });
    const session1 = new SessionModule();
    const agent1 = new AgentModule({ model: new OneShotModel(), systemPrompt: 'SYS' });
    await session1.start(bus1);
    await store1.start(bus1);
    await agent1.start(bus1);
    await bus1.start();

    bus1.publish(new UserTurnRequested({ sessionId: 's1', userText: 'hi' }));
    await agent1.whenIdle();
    await bus1.idle();
    const liveProjection = await bus1.request(
      new GetSessionProjection({ sessionId: 's1' }),
    );
    expect(liveProjection.messages.length).toBeGreaterThan(0);
    expect(
      liveProjection.messageHeaderSeqs.some((seq) => typeof seq === 'number'),
    ).toBe(true);
    await bus1.shutdown();

    const lines = (await readFile(join(sessionsDir, 's1.jsonl'), 'utf8'))
      .trim()
      .split('\n');
    const stored = JSON.parse(lines[0]!);
    const events = lines.slice(1).map((line) => JSON.parse(line));
    expect(stored.kind).toBe('session');
    expect(stored.format_version).toBe(3);
    expect(
      events.some((event: { type: string }) => event.type === 'user_message'),
    ).toBe(true);
    const assistantEvent = events.find(
      (event: { type: string }) => event.type === 'assistant_message',
    );
    const headerEvent = events.find(
      (event: { type: string }) => event.type === 'request_header',
    );
    expect(assistantEvent.type).toBe('assistant_message');
    expect(headerEvent.type).toBe('request_header');
    expect(assistantEvent.data.headerSeq).toBe(headerEvent.seq);

    const bus2 = new AsyncEventBus();
    bus2.provide(GetToolCatalog, () => []);
    const store2 = new StoreModule({ sessionsDir });
    const session2 = new SessionModule();
    const tui2 = new TuiModule();
    const model2 = new OneShotModel();
    const agent2 = new AgentModule({
      model: model2,
      systemPrompt: 'DIFFERENT',
      effort: 'low',
      thinkingEnabled: false,
    });
    await session2.start(bus2);
    await store2.start(bus2);
    await agent2.start(bus2);
    await tui2.start(bus2);
    await bus2.start();
    expect(await tui2.loadSession('s1')).toBe(true);

    bus2.publish(new UserTurnRequested({ sessionId: 's1', userText: 'after resume' }));
    await agent2.whenIdle();
    await bus2.idle();
    expect(model2.calls[0]?.systemPrompt).toBe('SYS');
    expect(model2.calls[0]?.effort).toBe('high');
    expect(model2.calls[0]?.thinking).toBe(true);

    const projection = await bus2.request(
      new GetSessionProjection({ sessionId: 's1' }),
    );
    expect(projection.messages.map((message) => message.content)).toContain(
      'projected answer',
    );
    expect(projection.messageHeaderSeqs.length).toBe(projection.messages.length);
    expect(projection.messageHeaderSeqs.some((seq) => typeof seq === 'number')).toBe(
      true,
    );
    const bundle = await bus2.request(new LoadSession({ targetSessionId: 's1' }));
    expect(bundle?.events?.length).toBeGreaterThan(0);
    expect(
      bundle?.events?.some((event) => event.type === 'turn_started'),
    ).toBe(true);
    expect(
      bundle?.events?.some((event) => event.type === 'request_header'),
    ).toBe(true);

    const prefix = await bus2.request(
      new ForkSession({
        sourceSessionId: 's1',
        targetSessionId: 's1-prefix',
        boundarySeq: 1,
      }),
    );
    expect(prefix?.events).toHaveLength(1);
    expect(prefix?.events?.[0]?.seq).toBe(1);
    expect(prefix?.parentSessionId).toBe('s1');
    expect(prefix?.forkBoundarySeq).toBe(1);

    const metas = await bus2.request(new ListSessions());
    const metadata = metas.find((meta) => meta.sessionId === 's1-prefix');
    expect(metadata?.parentSessionId).toBe('s1');
    expect(metadata?.forkBoundarySeq).toBe(1);
    expect(metadata?.forkedAt).toBeTruthy();
    await bus2.shutdown();
  });
});
