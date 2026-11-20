import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import { AgentRegistryModule } from '../src/agents/module';
import { SettingsModule } from '../src/settings/module';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { AppendSessionEvents, GetSessionProjection, RegenerateSessionTitle } from '../src/kernel/contracts/session';
import { UserTurnRequested } from '../src/kernel/contracts/chat';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

class TitleModel implements ChatModel {
  calls = 0;

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls += 1;
    if (request.systemPrompt?.includes('Generate a concise title')) {
      yield { type: 'content', delta: '  "Project Setup"  ' };
      yield {
        type: 'done',
        result: {
          content: '  "Project Setup"  ',
          thinking: '',
          toolCalls: [],
          finishReason: 'stop',
        },
      };
      return;
    }
    yield { type: 'content', delta: 'I can help with that.' };
    yield {
      type: 'done',
      result: {
        content: 'I can help with that.',
        thinking: '',
        toolCalls: [],
        finishReason: 'stop',
      },
    };
  }
}

describe('automatic session titles', () => {
  test('generates and stores a title when the setting is enabled', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-title-'));
    tempDirs.push(dir);
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const tools = new ToolsModule({
      roots: [dir],
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
      approvalTimeoutMs: 1_000,
      plugins: false,
    });
    const settings = new SettingsModule({
      settings: {
        provider: 'deepseek',
        baseUrl: 'https://api.deepseek.com',
        model: 'deepseek-chat',
        apiKey: 'sk-test',
        maxContextTokens: 131_072,
        maxOutputTokens: 4_096,
        compactThresholdRatio: 0.8,
        imageMaxDimension: 2_048,
        thinkingEnabled: true,
        effort: 'high',
        maxIterations: 10,
        sessionTitleGeneration: true,
        autoReflect: false,
        reflectionMinTools: 3,
        reflectionMinWriteTools: 2,
      },
    });
    const model = new TitleModel();
    const agent = new AgentModule({ model, systemPrompt: 'SYS' });
    await session.start(bus);
    await tools.start(bus);
    await settings.start(bus);
    await agent.start(bus);
    await bus.start();

    bus.publish(
      new UserTurnRequested({ sessionId: 's-title', userText: 'Set up the project' }),
    );
    await agent.whenIdle();
    await bus.idle();

    const projection = await bus.request(
      new GetSessionProjection({ sessionId: 's-title' }),
    );
    expect(projection.title).toBe('Project Setup');
    expect(projection.events.some((event) => event.type === 'session_title')).toBe(true);
    expect(model.calls).toBe(2);
    await agent.stop();
    await bus.shutdown();
  });

  test('manually regenerates a title even when auto titles are off', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-retitle-'));
    tempDirs.push(dir);
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const model = new TitleModel();
    const registry = new AgentRegistryModule({
      model,
      registerTools: false,
      defaultAgentId: 'main',
    });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(
      new AppendSessionEvents({
        sessionId: 's-manual',
        events: [
          {
            seq: 0,
            type: 'user_message',
            at: new Date().toISOString(),
            data: { message: { role: 'user', content: 'Set up the project' } },
          },
          {
            seq: 0,
            type: 'assistant_message',
            at: new Date().toISOString(),
            data: { message: { role: 'assistant', content: 'I can help.' } },
          },
        ],
      }),
    );
    const result = await bus.request(
      new RegenerateSessionTitle({ targetSessionId: 's-manual' }),
    );
    expect(result?.title).toBe('Project Setup');
    const projection = await bus.request(
      new GetSessionProjection({ sessionId: 's-manual' }),
    );
    expect(projection.title).toBe('Project Setup');
    await registry.stop();
    await bus.shutdown();
  });
});
