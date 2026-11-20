import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentRegistryModule } from '../src/agents/module';
import { SessionModule } from '../src/session/module';
import { StoreModule } from '../src/modules/store/module';
import {
  AgentDisposed,
  AgentSpawned,
  DisposeAgent,
  DisposeTeam,
  GetAgent,
  GetTeamLimits,
  ResumeAgent,
  ListAgentTasks,
  ListAgents,
  SendAgentMessage,
  SetTeamLimits,
  SpawnAgent,
  AgentTaskFinished,
  AgentTaskStarted,
} from '../src/kernel/contracts/agents';
import {
  AgentTeamMemberJoined,
  AgentTeamMemberLeft,
  AgentTeamMessage,
} from '../src/kernel/contracts/team';
import { TokenEmitted, UserTurnRequested } from '../src/kernel/contracts/chat';
import { AgentModule } from '../src/modules/agent/module';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { ExecuteTool, GetToolCatalog } from '../src/kernel/contracts/tools';
import { GetSessionProjection } from '../src/kernel/contracts/session';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

describe('AgentRegistryModule', () => {
  test('spawns, lists, messages and disposes agents', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule();
    await session.start(bus);
    await registry.start(bus);
    await bus.start();

    const spawned: AgentSpawned[] = [];
    const joined: AgentTeamMemberJoined[] = [];
    const left: AgentTeamMemberLeft[] = [];
    await bus.subscribe(AgentSpawned, (event) => { spawned.push(event); });
    await bus.subscribe(AgentTeamMemberJoined, (event) => { joined.push(event); });
    await bus.subscribe(AgentTeamMemberLeft, (event) => { left.push(event); });

    const lead = await bus.request(
      new SpawnAgent({
        spec: { agentId: 'lead', teamId: 'team-1', role: 'lead' },
      }),
    );
    const worker = await bus.request(
      new SpawnAgent({
        spec: {
          agentId: 'worker',
          teamId: 'team-1',
          parentAgentId: 'lead',
          role: 'worker',
        },
      }),
    );

    expect(lead.agentId).toBe('lead');
    expect(worker.parentAgentId).toBe('lead');
    expect((await bus.request(new ListAgents())).map((info) => info.agentId).sort()).toEqual([
      'lead',
      'worker',
    ]);
    expect((await bus.request(new GetAgent({ agentId: 'worker' })))?.role).toBe('worker');
    expect(spawned).toHaveLength(2);
    expect(joined).toHaveLength(2);

    const workerMessages: AgentTeamMessage[] = [];
    await bus.subscribe(
      AgentTeamMessage,
      (event) => { workerMessages.push(event); },
      { agentId: 'worker' },
    );
    const sent = await bus.request(
      new SendAgentMessage({
        fromAgentId: 'lead',
        toAgentId: 'worker',
        content: 'do the task',
      }),
    );
    await bus.idle();
    expect(sent.messageId).toBeTruthy();
    expect(workerMessages[0]?.content).toBe('do the task');

    const workerTokens: string[] = [];
    await bus.subscribe(
      TokenEmitted,
      (event) => { workerTokens.push(event.delta); },
      { agentId: 'worker' },
    );
    bus.publish(
      new UserTurnRequested({
        sessionId: 's1',
        userText: 'hi',
        agentId: 'lead',
        targetAgentId: 'worker',
      }),
    );
    await registry.whenIdle();
    await bus.idle();
    expect(workerTokens.join('')).toBe('Echo: hi');

    expect(await bus.request(new DisposeAgent({ agentId: 'worker' }))).toBe(true);
    expect(await bus.request(new GetAgent({ agentId: 'worker' }))).toBeNull();
    expect((await bus.request(new ListAgents())).map((info) => info.agentId)).toEqual(['lead']);
    expect(left).toHaveLength(1);
    expect(await bus.request(new DisposeAgent({ agentId: 'worker' }))).toBe(false);

    // A disposed agent no longer consumes commands.
    workerTokens.length = 0;
    bus.publish(
      new UserTurnRequested({
        sessionId: 's2',
        userText: 'again',
        targetAgentId: 'worker',
      }),
    );
    await registry.whenIdle();
    await bus.idle();
    expect(workerTokens).toEqual([]);

    await registry.stop();
    await bus.shutdown();
  });

  test('registers team tools and runs a worker task end-to-end', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-tools-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        defaultTeamId: 'team-tools',
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();

      const catalog = await bus.request(new GetToolCatalog());
      expect(catalog.map((spec) => spec.name)).toEqual(
        expect.arrayContaining([
          'spawn_agent',
          'send_agent_message',
          'list_agents',
          'dispose_agent',
        ]),
      );

      const spawned = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'do work', dispose: false },
          sessionId: 'main',
        }),
      );
      expect(spawned.output).toBe('Echo: do work');
      expect((await bus.request(new ListAgents())).length).toBe(1);
      await bus.idle();
      const projection = await bus.request(
        new GetSessionProjection({ sessionId: 'main' }),
      );
      const taskEvents = projection.customEvents.filter(
        (event) =>
          event.type === 'agent_task_started' ||
          event.type === 'agent_task_finished',
      );
      expect(taskEvents.length).toBeGreaterThanOrEqual(2);
      expect(taskEvents[0]?.data.taskId).toBeTruthy();
      expect(taskEvents[0]?.data.parentSessionId).toBe('main');

      const sent = await bus.request(
        new ExecuteTool({
          name: 'send_agent_message',
          args: {
            agent_id: (await bus.request(new ListAgents()))[0]!.agentId,
            content: 'follow up',
            from_agent_id: 'main',
          },
          sessionId: 'main',
        }),
      );
      expect(sent.output).toContain('Delivered:');
      await registry.whenIdle();
      await bus.idle();

      const listed = await bus.request(
        new ExecuteTool({ name: 'list_agents', args: {}, sessionId: 'main' }),
      );
      expect(listed.output).toContain('role=worker');
      const workerId = (await bus.request(new ListAgents()))[0]!.agentId;
      const disposed = await bus.request(
        new ExecuteTool({
          name: 'dispose_agent',
          args: { agent_id: workerId },
          sessionId: 'main',
        }),
      );
      expect(disposed.output).toContain('Disposed:');
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('adopts an externally constructed main agent', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const main = new AgentModule({ agentId: 'main', teamId: 'default' });
    const registry = new AgentRegistryModule({ registerTools: false, defaultAgentId: 'main' });
    await session.start(bus);
    await main.start(bus);
    registry.attachAgent(main, { agentId: 'main', role: 'lead', teamId: 'default' });
    await registry.start(bus);
    await bus.start();

    const agents = await bus.request(new ListAgents());
    expect(agents.map((info) => info.agentId)).toEqual(['main']);
    expect(agents[0]?.role).toBe('lead');

    // Broadcast UI turns still reach the main agent, but not workers.
    const tokens: string[] = [];
    await bus.subscribe(TokenEmitted, (event) => { tokens.push(event.delta); }, { agentId: 'main' });
    bus.publish(new UserTurnRequested({ sessionId: 's-main', userText: 'hello main' }));
    await main.whenIdle();
    await bus.idle();
    expect(tokens.join('')).toBe('Echo: hello main');

    await registry.stop();
    await bus.shutdown();
  });

  test('runs async tasks in parallel and waits for all results', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-async-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      let active = 0;
      let maxActive = 0;
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        defaultTeamId: 'team-async',
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond() {
                active += 1;
                maxActive = Math.max(maxActive, active);
                await new Promise((resolve) => setTimeout(resolve, 25));
                active -= 1;
                yield 'done';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();

      const startedTasks: string[] = [];
      const finishedTasks: string[] = [];
      await bus.subscribe(AgentTaskStarted, (event) => {
        startedTasks.push(event.task.taskId);
      });
      await bus.subscribe(AgentTaskFinished, (event) => {
        finishedTasks.push(event.task.status);
      });

      const first = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'one', wait: false },
          sessionId: 'main',
        }),
      );
      const second = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'two', wait: false },
          sessionId: 'main',
        }),
      );
      const taskId = (output: string): string => output.replace('Task: ', '');
      const firstId = taskId(String(first.output));
      const secondId = taskId(String(second.output));
      expect(firstId).toStartWith('task-');
      expect(secondId).toStartWith('task-');

      const waited = await bus.request(
        new ExecuteTool({
          name: 'wait_agents',
          args: { task_ids: [firstId, secondId] },
          sessionId: 'main',
        }),
      );
      expect(waited.output).toContain('[completed]');
      expect(waited.output).toContain('done');
      expect(maxActive).toBe(2);
      expect(startedTasks).toHaveLength(2);
      expect(finishedTasks).toEqual(['completed', 'completed']);
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('cancels a running task and reports cancelled status', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-cancel-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond() {
                await new Promise((resolve) => setTimeout(resolve, 40));
                yield 'too late';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();

      const started = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'slow', wait: false },
          sessionId: 'main',
        }),
      );
      const id = String(started.output).replace('Task: ', '');
      const cancelled = await bus.request(
        new ExecuteTool({
          name: 'cancel_agent_task',
          args: { task_id: id },
          sessionId: 'main',
        }),
      );
      expect(cancelled.output).toContain('Cancelled:');
      const waited = await bus.request(
        new ExecuteTool({
          name: 'wait_agents',
          args: { task_ids: [id] },
          sessionId: 'main',
        }),
      );
      expect(waited.output).toContain('[cancelled]');
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('rejects cross-team messages and filters agents by team', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({ registerTools: false });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'a1', teamId: 'team-a' } }),
    );
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'b1', teamId: 'team-b' } }),
    );
    expect(
      (await bus.request(new ListAgents({ teamId: 'team-a' }))).map(
        (info) => info.agentId,
      ),
    ).toEqual(['a1']);
    await expect(
      bus.request(
        new SendAgentMessage({
          fromAgentId: 'a1',
          toAgentId: 'b1',
          content: 'cross-team',
        }),
      ),
    ).rejects.toThrow('cross-team message denied');
    await registry.stop();
    await bus.shutdown();
  });

  test('enforces a per-team agent quota', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({
      registerTools: false,
      maxAgentsPerTeam: 1,
    });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(new SpawnAgent({ spec: { agentId: 'a1', teamId: 'team-a' } }));
    await expect(
      bus.request(new SpawnAgent({ spec: { agentId: 'a2', teamId: 'team-a' } })),
    ).rejects.toThrow('team agent limit reached');
    await bus.request(new SpawnAgent({ spec: { agentId: 'b1', teamId: 'team-b' } }));
    expect((await bus.request(new ListAgents())).map((info) => info.agentId).sort()).toEqual([
      'a1',
      'b1',
    ]);
    await registry.stop();
    await bus.shutdown();
  });

  test('limits concurrently running tasks per team', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-team-limit-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      let active = 0;
      let maxActive = 0;
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        defaultTeamId: 'team-limit',
        maxConcurrentTasksPerTeam: 1,
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond() {
                active += 1;
                maxActive = Math.max(maxActive, active);
                await new Promise((resolve) => setTimeout(resolve, 20));
                active -= 1;
                yield 'done';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();

      const started = await Promise.all([
        bus.request(
          new ExecuteTool({
            name: 'spawn_agent',
            args: { prompt: 'one', wait: false },
            sessionId: 'main',
          }),
        ),
        bus.request(
          new ExecuteTool({
            name: 'spawn_agent',
            args: { prompt: 'two', wait: false },
            sessionId: 'main',
          }),
        ),
      ]);
      const ids = started.map((result) => String(result.output).replace('Task: ', ''));
      const waited = await bus.request(
        new ExecuteTool({
          name: 'wait_agents',
          args: { task_ids: ids },
          sessionId: 'main',
        }),
      );
      expect((waited.output ?? '').match(/\[completed\]/g)).toHaveLength(2);
      expect(maxActive).toBe(1);
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('runs higher-priority queued tasks first within a team', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-priority-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const order: string[] = [];
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        defaultTeamId: 'team-priority',
        maxConcurrentTasksPerTeam: 1,
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond(params: { text: string }) {
                order.push(params.text);
                await new Promise((resolve) => setTimeout(resolve, 80));
                yield 'done';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();

      const spawn = async (
        prompt: string,
        priority: number,
      ): Promise<string> => {
        const result = await bus.request(
          new ExecuteTool({
            name: 'spawn_agent',
            args: { prompt, wait: false, priority },
            sessionId: 'main',
          }),
        );
        return String(result.output).replace('Task: ', '');
      };
      const blocker = await spawn('blocker', 0);
      await new Promise((resolve) => setTimeout(resolve, 10));
      const low = await spawn('low', 1);
      const high = await spawn('high', 5);
      await bus.request(
        new ExecuteTool({
          name: 'wait_agents',
          args: { task_ids: [blocker, low, high] },
          sessionId: 'main',
        }),
      );
      expect(order).toEqual(['blocker', 'high', 'low']);
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('classifies a timed-out task', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-timeout-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        taskTimeoutMs: 10,
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond() {
                await new Promise((resolve) => setTimeout(resolve, 100));
                yield 'late';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();
      const result = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'slow' },
          sessionId: 'main',
        }),
      );
      expect(result.output ?? result.error).toContain('timed out');
      const tasks = await bus.request(new ListAgentTasks());
      expect(tasks).toHaveLength(1);
      expect(tasks[0]?.status).toBe('failed');
      expect(tasks[0]?.failureReason).toBe('timeout');
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('applies dynamic per-team limits at runtime', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({ registerTools: false });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();

    await bus.request(
      new SetTeamLimits({
        limits: { teamId: 'team-live', maxAgents: 1, maxConcurrentTasks: 1 },
      }),
    );
    const limits = await bus.request(new GetTeamLimits());
    expect(limits).toEqual([
      { teamId: 'team-live', maxAgents: 1, maxConcurrentTasks: 1 },
    ]);
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'live-1', teamId: 'team-live' } }),
    );
    await expect(
      bus.request(
        new SpawnAgent({ spec: { agentId: 'live-2', teamId: 'team-live' } }),
      ),
    ).rejects.toThrow('team agent limit reached');
    await registry.stop();
    await bus.shutdown();
  });

  test('retries a failed task and records attempts', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-retry-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      let attempts = 0;
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond() {
                attempts += 1;
                if (attempts === 1) throw new Error('transient failure');
                yield 'recovered';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();
      const started = Date.now();
      const result = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'work', retries: 1, retry_delay_ms: 20 },
          sessionId: 'main',
        }),
      );
      expect(Date.now() - started).toBeGreaterThanOrEqual(15);
      expect(result.output).toBe('recovered');
      const tasks = await bus.request(new ListAgentTasks());
      expect(tasks[0]?.status).toBe('completed');
      expect(tasks[0]?.attempts).toBe(2);
      expect(attempts).toBe(2);
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('delegates a task to another team without cross-team messaging', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-delegate-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        defaultTeamId: 'team-a',
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();
      const result = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'remote work', team_id: 'team-b', dispose: false },
          sessionId: 'main',
        }),
      );
      expect(result.output).toBe('Echo: remote work');
      const remote = await bus.request(new ListAgents({ teamId: 'team-b' }));
      expect(remote).toHaveLength(1);
      expect(remote[0]?.parentAgentId).toBe('main');
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('does not resume child agent conversations', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-resume-'));
    try {
      const agentsPath = join(dir, 'agents.jsonl');
      const sessionsDir = join(dir, 'sessions');
      const firstBus = new AsyncEventBus();
      const firstSession = new SessionModule();
      const firstStore = new StoreModule({ sessionsDir });
      const first = new AgentRegistryModule({
        registerTools: false,
        agentsPath,
        defaultAgentId: 'main',
      });
      await firstStore.start(firstBus);
      await firstSession.start(firstBus);
      await first.start(firstBus);
      await firstBus.start();
      await firstBus.request(
        new SpawnAgent({
          spec: {
            agentId: 'worker-1',
            parentAgentId: 'main',
            teamId: 'team-resume',
            sessionId: 'worker-session',
            role: 'worker',
          },
        }),
      );
      firstBus.publish(
        new UserTurnRequested({
          sessionId: 'worker-session',
          userText: 'child only',
          targetAgentId: 'worker-1',
        }),
      );
      await first.whenIdle();
      await firstBus.idle();
      await first.stop();
      await firstBus.shutdown();

      const secondBus = new AsyncEventBus();
      const secondSession = new SessionModule();
      const secondStore = new StoreModule({ sessionsDir });
      const second = new AgentRegistryModule({
        registerTools: false,
        agentsPath,
        defaultAgentId: 'main',
      });
      await secondStore.start(secondBus);
      await secondSession.start(secondBus);
      await second.start(secondBus);
      await secondBus.start();
      const resumable = await secondBus.request(
        new ListAgents({ includeResumable: true }),
      );
      expect(resumable.map((info) => info.agentId)).toContain('worker-1');
      // Child agents stay diagnostic-only; they cannot be resumed as conversations.
      await expect(
        secondBus.request(new ResumeAgent({ agentId: 'worker-1' })),
      ).rejects.toThrow('only the main agent conversation can be resumed');
      expect((await secondBus.request(new ListAgents())).map((info) => info.agentId)).not.toContain(
        'worker-1',
      );
      await second.stop();
      await secondBus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('wait_agents honours its timeout', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-wait-timeout-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
        createAgent: (spec) =>
          new AgentModule({
            agentId: spec.agentId,
            parentAgentId: spec.parentAgentId,
            teamId: spec.teamId,
            acceptBroadcast: false,
            responder: {
              async *respond() {
                await new Promise((resolve) => setTimeout(resolve, 100));
                yield 'slow';
              },
            },
          }),
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();
      const started = await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'slow', wait: false },
          sessionId: 'main',
        }),
      );
      const taskId = String(started.output).replace('Task: ', '');
      const waited = await bus.request(
        new ExecuteTool({
          name: 'wait_agents',
          args: { task_ids: [taskId], timeout_ms: 10 },
          sessionId: 'main',
        }),
      );
      expect(waited.output).toContain('wait_agents timed out');
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('disposes a whole team and clears its limits', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({ registerTools: false });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(new SpawnAgent({ spec: { agentId: 'a1', teamId: 'team-a' } }));
    await bus.request(new SpawnAgent({ spec: { agentId: 'a2', teamId: 'team-a' } }));
    await bus.request(new SpawnAgent({ spec: { agentId: 'b1', teamId: 'team-b' } }));
    await bus.request(
      new SetTeamLimits({ limits: { teamId: 'team-a', maxAgents: 5 } }),
    );

    const result = await bus.request(new DisposeTeam({ teamId: 'team-a' }));
    expect(result.ok).toBe(true);
    expect(result.disposedAgents).toBe(2);
    expect((await bus.request(new ListAgents())).map((info) => info.agentId)).toEqual(['b1']);
    expect((await bus.request(new GetTeamLimits())).map((limits) => limits.teamId)).not.toContain(
      'team-a',
    );

    // The main/lead team is protected from a bulk delete.
    await bus.request(new SpawnAgent({ spec: { agentId: 'lead', teamId: 'core', role: 'lead' } }));
    const protectedResult = await bus.request(new DisposeTeam({ teamId: 'core' }));
    expect(protectedResult.ok).toBe(false);
    expect((await bus.request(new ListAgents())).map((info) => info.agentId)).toContain('lead');
    await registry.stop();
    await bus.shutdown();
  });

  test('serializes concurrent dispose calls', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({ registerTools: false });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(new SpawnAgent({ spec: { agentId: 'worker-x' } }));
    const results = await Promise.all([
      bus.request(new DisposeAgent({ agentId: 'worker-x' })),
      bus.request(new DisposeAgent({ agentId: 'worker-x' })),
    ]);
    expect(results).toEqual([true, true]);
    expect(await bus.request(new ListAgents())).toEqual([]);
    await registry.stop();
    await bus.shutdown();
  });

  test('keeps a worker when dispose is the string "false"', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-registry-dispose-string-'));
    try {
      const bus = new AsyncEventBus();
      const session = new SessionModule();
      const tools = new ToolsModule({
        roots: [dir],
        builtin: false,
        plugins: false,
        policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
        audit: new AuditLogger(join(dir, 'audit.jsonl')),
      });
      const registry = new AgentRegistryModule({
        registerTools: true,
        defaultAgentId: 'main',
      });
      await session.start(bus);
      await tools.start(bus);
      await registry.start(bus);
      await bus.start();
      await bus.request(
        new ExecuteTool({
          name: 'spawn_agent',
          args: { prompt: 'stay alive', agent_id: 'worker-stay', dispose: 'false' },
          sessionId: 'main',
        }),
      );
      const agents = await bus.request(new ListAgents());
      expect(agents.map((agent) => agent.agentId)).toContain('worker-stay');
      await registry.stop();
      await bus.shutdown();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('rejects duplicate agent ids and unknown message targets', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule();
    await session.start(bus);
    await registry.start(bus);
    await bus.start();

    await bus.request(new SpawnAgent({ spec: { agentId: 'a1' } }));
    await expect(
      bus.request(new SpawnAgent({ spec: { agentId: 'a1' } })),
    ).rejects.toThrow('already exists');
    await expect(
      bus.request(
        new SendAgentMessage({ fromAgentId: 'a1', toAgentId: 'missing', content: 'x' }),
      ),
    ).rejects.toThrow('unknown agent');
    await registry.stop();
    await bus.shutdown();
  });

  test('disposing a parent cascades to child agents', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({ registerTools: false });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();

    await bus.request(
      new SpawnAgent({ spec: { agentId: 'parent', parentAgentId: 'main' } }),
    );
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'child', parentAgentId: 'parent' } }),
    );
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'grandchild', parentAgentId: 'child' } }),
    );
    expect((await bus.request(new ListAgents())).map((info) => info.agentId).sort()).toEqual([
      'child',
      'grandchild',
      'parent',
    ]);

    await bus.request(new DisposeAgent({ agentId: 'parent' }));
    expect(await bus.request(new ListAgents())).toEqual([]);
    await registry.stop();
    await bus.shutdown();
  });

  test('publishes AgentDisposed for every disposed member', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule();
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(new SpawnAgent({ spec: { agentId: 'a1' } }));
    const disposed: string[] = [];
    await bus.subscribe(AgentDisposed, (event) => { disposed.push(event.agentId); });
    await bus.request(new DisposeAgent({ agentId: 'a1' }));
    await bus.idle();
    expect(disposed).toEqual(['a1']);
    await registry.stop();
    await bus.shutdown();
  });
});