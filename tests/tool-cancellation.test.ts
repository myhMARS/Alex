import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { ToolsModule } from '../src/tools/module';
import { AuditLogger, PermissionPolicy } from '../src/tools/permissions';
import { ExecuteTool, RegisterTool } from '../src/kernel/contracts/tools';

const dirs: string[] = [];

afterEach(async () => {
  while (dirs.length > 0) {
    const dir = dirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

describe('tool cancellation', () => {
  test('propagates the turn abort signal into tool execution', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-tool-cancel-'));
    dirs.push(dir);
    const bus = new AsyncEventBus();
    const tools = new ToolsModule({
      roots: [dir],
      builtin: false,
      plugins: false,
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
    });
    await tools.start(bus);
    await bus.start();

    let sawAbort = false;
    await bus.request(
      new RegisterTool({
        name: 'slow_tool',
        description: 'waits until cancelled',
        jsonSchema: { type: 'object', properties: {} },
        callable: (_args: Record<string, unknown>, ctx: { signal?: AbortSignal }) =>
          new Promise<string>((_resolve, reject) => {
            if (ctx.signal?.aborted) {
              sawAbort = true;
              reject(new Error('cancelled before start'));
              return;
            }
            ctx.signal?.addEventListener(
              'abort',
              () => {
                sawAbort = true;
                reject(new Error('cancelled'));
              },
              { once: true },
            );
          }),
      }),
    );

    const controller = new AbortController();
    const pending = bus.request(
      new ExecuteTool({
        name: 'slow_tool',
        args: {},
        sessionId: 's1',
        signal: controller.signal,
      }),
    );
    setTimeout(() => controller.abort(), 10);
    const result = await pending;
    expect(sawAbort).toBe(true);
    expect(result.error).toContain('cancelled');
    await bus.shutdown();
  });

  test('cancels a tool that ignores the context signal', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-tool-cancel-ignore-'));
    dirs.push(dir);
    const bus = new AsyncEventBus();
    const tools = new ToolsModule({
      roots: [dir],
      builtin: false,
      plugins: false,
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
    });
    await tools.start(bus);
    await bus.start();
    await bus.request(
      new RegisterTool({
        name: 'ignores_signal',
        description: 'never observes ctx.signal',
        jsonSchema: { type: 'object', properties: {} },
        callable: async () => {
          await new Promise((resolve) => setTimeout(resolve, 100));
          return 'late';
        },
      }),
    );
    const controller = new AbortController();
    const pending = bus.request(
      new ExecuteTool({
        name: 'ignores_signal',
        args: {},
        sessionId: 's1',
        signal: controller.signal,
      }),
    );
    setTimeout(() => controller.abort(), 10);
    const started = Date.now();
    const result = await pending;
    expect(Date.now() - started).toBeLessThan(90);
    expect(result.error).toContain('cancelled');
    await bus.shutdown();
  });

  test('rejects an already-aborted tool request before invoking it', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-tool-cancel-pre-'));
    dirs.push(dir);
    const bus = new AsyncEventBus();
    const tools = new ToolsModule({
      roots: [dir],
      builtin: false,
      plugins: false,
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
    });
    await tools.start(bus);
    await bus.start();
    let invoked = false;
    await bus.request(
      new RegisterTool({
        name: 'never_run',
        description: 'must not run',
        jsonSchema: { type: 'object', properties: {} },
        callable: async () => {
          invoked = true;
          return 'ran';
        },
      }),
    );
    const controller = new AbortController();
    controller.abort();
    const result = await bus.request(
      new ExecuteTool({
        name: 'never_run',
        args: {},
        sessionId: 's1',
        signal: controller.signal,
      }),
    );
    expect(invoked).toBe(false);
    expect(result.error).toContain('cancelled');
    await bus.shutdown();
  });
});