import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { ToolsModule } from '../src/tools/module';
import { AuditLogger, PermissionPolicy } from '../src/tools/permissions';
import { ExecuteTool, SetAgentPermissions } from '../src/kernel/contracts/tools';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import { UserTurnRequested } from '../src/kernel/contracts/chat';
import { GetSessionProjection } from '../src/kernel/contracts/session';
import type { ChatModel } from '../src/llm/client';

const dirs: string[] = [];

afterEach(async () => {
  while (dirs.length > 0) {
    const dir = dirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function setup(): Promise<{
  bus: AsyncEventBus;
  dir: string;
  file: string;
}> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-agent-perm-'));
  dirs.push(dir);
  const file = join(dir, 'note.txt');
  await writeFile(file, 'allowed content', 'utf8');
  const bus = new AsyncEventBus();
  const tools = new ToolsModule({
    roots: [dir],
    plugins: false,
    policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
    audit: new AuditLogger(join(dir, 'audit.jsonl')),
  });
  await tools.start(bus);
  await bus.start();
  return { bus, dir, file };
}

describe('per-agent tool permissions', () => {
  test('denies a tool for one agent without affecting others', async () => {
    const { bus, file, dir } = await setup();
    const main = await bus.request(
      new ExecuteTool({ name: 'read', args: { path: file }, sessionId: 'main' }),
    );
    expect(main.output).toContain('allowed content');

    await bus.request(
      new SetAgentPermissions({
        agentId: 'worker',
        allowed: [],
        denied: ['read'],
      }),
    );
    const worker = await bus.request(
      new ExecuteTool({
        name: 'read',
        args: { path: file },
        sessionId: 'worker-session',
        agentId: 'worker',
      }),
    );
    expect(worker.error).toContain('blocked');
    expect(worker.error).toContain("permission 'read'");

    const audit = await readFile(join(dir, 'audit.jsonl'), 'utf8');
    expect(audit).toContain('"agentId":"worker"');
    await bus.shutdown();
  });

  test('clears an agent policy and falls back to the global policy', async () => {
    const { bus, file } = await setup();
    await bus.request(
      new SetAgentPermissions({ agentId: 'worker', allowed: [], denied: ['read'] }),
    );
    expect(
      (
        await bus.request(
          new ExecuteTool({
            name: 'read',
            args: { path: file },
            sessionId: 's1',
            agentId: 'worker',
          }),
        )
      ).error,
    ).toContain('blocked');

    await bus.request(new SetAgentPermissions({ agentId: 'worker' }));
    const result = await bus.request(
      new ExecuteTool({
        name: 'read',
        args: { path: file },
        sessionId: 's1',
        agentId: 'worker',
      }),
    );
    expect(result.output).toContain('allowed content');
    await bus.shutdown();
  });
});

test('Agent turns pass agentId so per-agent policies actually apply', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-agent-perm-turn-'));
  dirs.push(dir);
  const target = join(dir, 'team_demo.txt');
  const bus = new AsyncEventBus();
  const session = new SessionModule();
  const tools = new ToolsModule({
    roots: [dir],
    plugins: false,
    policy: new PermissionPolicy({ allowed: ['read', 'write'], env: {} }),
    audit: new AuditLogger(join(dir, 'audit.jsonl')),
  });
  let call = 0;
  const model: ChatModel = {
    async *streamChat() {
      call += 1;
      if (call === 1) {
        const toolCall = {
          id: 'call_write',
          type: 'function' as const,
          function: {
            name: 'write',
            arguments: JSON.stringify({ path: 'team_demo.txt', content: 'hello' }),
          },
        };
        yield { type: 'tool_calls' as const, toolCalls: [toolCall] };
        yield {
          type: 'done' as const,
          result: {
            content: '',
            thinking: '',
            toolCalls: [toolCall],
            finishReason: 'tool_calls',
          },
        };
        return;
      }
      yield { type: 'content' as const, delta: 'finished' };
      yield {
        type: 'done' as const,
        result: { content: 'finished', thinking: '', toolCalls: [], finishReason: 'stop' },
      };
    },
  };
  const agent = new AgentModule({
    agentId: 'outsider',
    teamId: 'default',
    model,
    autoReflect: false,
  });
  await session.start(bus);
  await tools.start(bus);
  await agent.start(bus);
  await bus.start();
  await bus.request(
    new SetAgentPermissions({ agentId: 'outsider', allowed: ['read'], denied: ['write'] }),
  );

  bus.publish(
    new UserTurnRequested({
      sessionId: 'outsider-session',
      userText: 'write the demo file',
      targetAgentId: 'outsider',
    }),
  );
  await bus.idle();
  await agent.whenIdle();
  await bus.idle();

  expect(await readFile(target, 'utf8').catch(() => null)).toBeNull();
  const projection = await bus.request(
    new GetSessionProjection({
      sessionId: 'outsider-session',
    }),
  );
  // The blocked tool output must be visible in the session log.
  expect(
    projection.messages.some(
      (message) =>
        message.role === 'tool' &&
        String(message.content).includes("permission 'write'"),
    ),
  ).toBe(true);
  await agent.stop();
  await bus.shutdown();
});
