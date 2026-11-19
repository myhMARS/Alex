import { afterEach, describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import {
  AppendSessionEvents,
  GetSessionProjection,
} from '../src/kernel/contracts/session';
import { messageEvent } from '../src/session/projector';
import { GetToolCatalog } from '../src/kernel/contracts/tools';
import {
  CompactContext,
  ContextCompacted,
} from '../src/kernel/contracts/compaction';
import { UserTurnRequested } from '../src/kernel/contracts/chat';
import { assistantMessage, userMessage } from '../src/kernel/dto/message';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';

class SummaryModel implements ChatModel {
  readonly calls: ChatRequest[] = [];

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls.push({ ...request, messages: [...request.messages] });
    yield {
      type: 'done',
      result: {
        content: 'CONDENSED EARLIER CONTEXT',
        thinking: '',
        toolCalls: [],
        finishReason: 'stop',
        usage: { promptTokens: 600, completionTokens: 10, totalTokens: 610 },
      },
    };
  }
}

async function setup(
  model: ChatModel,
  options: { contextWindowTokens?: number; compactThresholdRatio?: number } = {},
) {
  const bus = new AsyncEventBus();
  bus.provide(GetToolCatalog, () => []);
  const session = new SessionModule();
  const agent = new AgentModule({
    model,
    systemPrompt: 'SYS',
    contextWindowTokens: options.contextWindowTokens ?? 0,
    compactThresholdRatio: options.compactThresholdRatio ?? 0.8,
  });
  await session.start(bus);
  await agent.start(bus);
  await bus.start();
  return { bus, agent };
}

afterEach(() => {});

describe('context compaction', () => {
  test('manual compact replaces old history with a summary and keeps the tail', async () => {
    const model = new SummaryModel();
    const { bus, agent } = await setup(model, { contextWindowTokens: 500 });
    const events: ContextCompacted[] = [];
    await bus.subscribe(ContextCompacted, (event) => {
      events.push(event);
    });

    const history = Array.from({ length: 10 }, (_, index) =>
      index % 2 === 0
        ? userMessage(`question ${index} `.repeat(12))
        : assistantMessage(`answer ${index} `.repeat(12)),
    );
    await bus.request(
      new AppendSessionEvents({
        sessionId: 's1',
        events: history.map((message) =>
          messageEvent(
            message.role === 'user'
              ? 'user_message'
              : message.role === 'assistant'
                ? 'assistant_message'
                : 'tool_message',
            message,
          ),
        ),
      }),
    );

    const result = await bus.request(new CompactContext({ sessionId: 's1' }));
    await bus.idle();
    expect(result.skipped).toBe(false);
    expect(result.compactedMessages).toBeGreaterThan(1);
    expect(result.keptMessages).toBeGreaterThan(0);
    expect(result.summary).toBe('CONDENSED EARLIER CONTEXT');
    expect(events).toHaveLength(1);
    expect(events[0]?.result.id).toBe(result.id);

    const context = (
      await bus.request(new GetSessionProjection({ sessionId: 's1' }))
    ).messages;
    expect(context[0]?.content).toContain('<compacted-summary>');
    expect(context[0]?.content).toContain('CONDENSED EARLIER CONTEXT');
    expect(context.length).toBe(result.keptMessages + 1);
    await bus.shutdown();
  });

  test('auto compacts once provider prompt pressure crosses the threshold', async () => {
    const model = new SummaryModel();
    const { bus, agent } = await setup(model, {
      contextWindowTokens: 2_000,
      compactThresholdRatio: 0.1,
    });

    const history = Array.from({ length: 20 }, (_, index) =>
      index % 2 === 0
        ? userMessage(`history user ${index} `.repeat(8))
        : assistantMessage(`history assistant ${index} `.repeat(8)),
    );
    await bus.request(
      new AppendSessionEvents({
        sessionId: 's1',
        events: history.map((message) =>
          messageEvent(
            message.role === 'user'
              ? 'user_message'
              : message.role === 'assistant'
                ? 'assistant_message'
                : 'tool_message',
            message,
          ),
        ),
      }),
    );

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'first' }));
    await agent.whenIdle();
    await bus.idle();

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'second' }));
    await agent.whenIdle();
    await bus.idle();

    expect(model.calls.length).toBeGreaterThan(2);
    expect(
      model.calls.slice(1).some((call) =>
        call.messages.some(
          (message) =>
            typeof message.content === 'string' &&
            message.content.includes('<compacted-summary>'),
        ),
      ),
    ).toBe(true);
    await bus.shutdown();
  });
});