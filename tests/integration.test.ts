import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { ModuleHost } from '../src/kernel/runtime';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import { StoreModule } from '../src/modules/store/module';
import { TuiModule } from '../src/modules/tui/module';
import { ToolsModule } from '../src/tools/module';
import { MCPModule } from '../src/mcp/module';
import { SkillModule } from '../src/skill/module';
import { CronModule } from '../src/cron/module';
import { GetSessionProjection } from '../src/kernel/contracts/session';
import { ListSessions, LoadSession } from '../src/kernel/contracts/session';
import { ExecuteTool } from '../src/kernel/contracts/tools';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { writeFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function waitFor(check: () => boolean, timeoutMs = 2_000): Promise<void> {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) throw new Error('waitFor timed out');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

describe('TUI as a bus module (integration)', () => {
  test('TUI command → agent → memory/store → TUI view', async () => {
    const sessionsDir = await mkdtemp(join(tmpdir(), 'alex-ts-integration-'));
    tempDirs.push(sessionsDir);

    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, { debug() {}, info() {}, warn() {}, error() {} });
    const agent = new AgentModule();
    const tui = new TuiModule();

    host.register(new SessionModule());
    host.register(new StoreModule({ sessionsDir }));
    host.register(new ToolsModule({ roots: [process.cwd()] }));
    host.register(new MCPModule({ configs: [] }));
    host.register(new SkillModule({ skillsPath: join(sessionsDir, 'skills.json') }));
    host.register(new CronModule({ directory: join(sessionsDir, 'cron') }));
    host.register(agent);
    host.register(tui);
    await host.startAll();

    tui.submitUserTurn('hello integration', 's-int');
    await agent.whenIdle();
    await bus.idle();

    const turns = tui.viewState.turns;
    expect(turns).toHaveLength(1);
    expect(turns[0]?.status).toBe('done');
    expect(turns[0]?.content).toBe('Echo: hello integration');
    expect(turns[0]?.userInput).toBe('hello integration');

    const history = (
      await bus.request(new GetSessionProjection({ sessionId: 's-int' }))
    ).messages;
    expect(history.map((message) => message.content)).toEqual([
      'hello integration',
      'Echo: hello integration',
    ]);

    const sessions = await bus.request(new ListSessions());
    expect(sessions).toHaveLength(1);
    expect(sessions[0]?.sessionId).toBe('s-int');

    const bundle = await bus.request(new LoadSession({ targetSessionId: 's-int' }));
    expect(bundle?.messages).toHaveLength(2);

    await host.stopAll();
  });

  test('TUI approves a tool call through bus events', async () => {
    const workDir = await mkdtemp(join(tmpdir(), 'alex-ts-approval-'));
    tempDirs.push(workDir);
    const bus = new AsyncEventBus();
    const tools = new ToolsModule({
      roots: [workDir],
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(workDir, 'audit.jsonl')),
      approvalTimeoutMs: 5_000,
    });
    const tui = new TuiModule();
    await tools.start(bus);
    await tui.start(bus);
    await bus.start();

    const target = join(workDir, 'approved.txt');
    const pending = bus.request(
      new ExecuteTool({
        name: 'write',
        args: { path: target, content: 'approved content' },
        sessionId: 's-approval',
      }),
    );

    await waitFor(() => tui.viewState.pendingApprovals.length === 1);
    const approval = tui.viewState.pendingApprovals[0];
    expect(approval?.toolName).toBe('write');
    expect(approval?.preview).toContain('write');

    tui.resolveApproval(approval!.reqId, true);
    const result = await pending;
    expect(result.error).toBeUndefined();
    expect(result.output).toContain('Wrote');
    expect(await readFile(target, 'utf8')).toBe('approved content');
    await bus.idle();
    expect(tui.viewState.pendingApprovals).toHaveLength(0);

    await bus.shutdown();
  }, 10_000);
});