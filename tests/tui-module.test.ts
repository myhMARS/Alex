import { afterEach, describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { TuiModule } from '../src/modules/tui/module';
import {
  FeedbackSubmitted,
  ThinkingUpdated,
  TokenEmitted,
  TurnCompleted,
  TurnFailed,
  TurnStarted,
  UserTurnRequested,
} from '../src/kernel/contracts/chat';
import { ReflectSkills, SkillLoaded, SkillsReflected } from '../src/kernel/contracts/skills';
import {
  ToolApprovalResolved,
  ToolFinished,
  ToolStarted,
} from '../src/kernel/contracts/tools';
import { CronJobEvent, ListCronJobs } from '../src/kernel/contracts/cron';
import { GetMCPStatus, GetToolCatalog, ListDirectory } from '../src/kernel/contracts/tools';
import { ListSkills } from '../src/kernel/contracts/skills';
import { GetPromptInfo } from '../src/kernel/contracts/prompt';
import {
  AgentTaskFinished,
  DisposeAgent,
  ListAgentMessages,
  ListAgents,
  ListAgentTasks,
  type AgentInfo,
  type AgentTaskInfo,
} from '../src/kernel/contracts/agents';
import {
  GetSessionProjection,
  ListSessions,
  LoadSession,
  ReplaceSessionEvents,
} from '../src/kernel/contracts/session';
import {
  GetSystemPrompt,
  SetModelEffort,
  TokenUsageReported,
} from '../src/kernel/contracts/chat';
import {
  GetSettings,
  UpdateSettings,
  type SettingsSnapshot,
} from '../src/kernel/contracts/settings';
import { StoreModule } from '../src/modules/store/module';
import { SessionModule } from '../src/session/module';
import { assistantMessage, userMessage } from '../src/kernel/dto/message';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

async function startedTui(): Promise<{ bus: AsyncEventBus; tui: TuiModule }> {
  const bus = new AsyncEventBus();
  const tui = new TuiModule();
  await tui.start(bus);
  await bus.start();
  return { bus, tui };
}

const tempRoots: string[] = [];

afterEach(async () => {
  while (tempRoots.length > 0) {
    const dir = tempRoots.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

describe('TuiModule as a bus module', () => {
  test('folds streaming events into view state', async () => {
    const { bus, tui } = await startedTui();

    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 't1', kind: 'user', userInput: 'hello' }));
    bus.publish(new TokenEmitted({ sessionId: 's1', turnId: 't1', delta: 'Hi ' }));
    bus.publish(new TokenEmitted({ sessionId: 's1', turnId: 't1', delta: 'there' }));
    bus.publish(new ThinkingUpdated({ sessionId: 's1', turnId: 't1', delta: 'thinking…' }));
    bus.publish(new SkillLoaded({ sessionId: 's1', turnId: 't1', skillName: 'demo', skillPattern: 'when x' }));
    bus.publish(new ToolStarted({ sessionId: 's1', turnId: 't1', toolId: 'tool1', toolName: 'read', toolInput: { path: 'a' } }));
    bus.publish(new ToolFinished({ sessionId: 's1', turnId: 't1', toolId: 'tool1', output: 'file body' }));
    await bus.idle();

    const streaming = tui.viewState.streaming;
    expect(streaming).toHaveLength(1);
    expect(streaming[0]?.content).toBe('Hi there');
    expect(streaming[0]?.thinking).toBe('thinking…');
    expect(streaming[0]?.skills[0]?.name).toBe('demo');
    expect(streaming[0]?.tools[0]?.output).toBe('file body');

    bus.publish(
      new TurnCompleted({
        sessionId: 's1',
        turnId: 't1',
        kind: 'user',
        content: 'Hi there',
        thinking: 'thinking…',
        messages: [],
      }),
    );
    await bus.idle();

    expect(tui.viewState.streaming).toHaveLength(0);
    expect(tui.viewState.turns).toHaveLength(1);
    expect(tui.viewState.turns[0]?.status).toBe('done');
    await bus.shutdown();
  });

  test('marks failed turns', async () => {
    const { bus, tui } = await startedTui();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 't1', kind: 'user', userInput: 'x' }));
    bus.publish(new TurnFailed({ sessionId: 's1', turnId: 't1', error: 'boom' }));
    await bus.idle();

    expect(tui.viewState.turns[0]?.status).toBe('failed');
    expect(tui.viewState.turns[0]?.error).toBe('boom');
    await bus.shutdown();
  });

  test('records reflection and cron events', async () => {
    const { bus, tui } = await startedTui();
    bus.publish(new SkillsReflected({ sessionId: 's1', created: 2, updated: 1, deprecated: 0, names: ['a', 'b'] }));
    bus.publish(new CronJobEvent({ sessionId: 's1', jobId: 'j1', name: 'daily', status: 'RUNNING' }));
    await bus.idle();

    expect(tui.viewState.lastReflection?.created).toBe(2);
    expect(tui.viewState.cron[0]?.name).toBe('daily');
    await bus.shutdown();
  });

  test('pages load data over the bus and can be dismissed', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListSkills, () => [
      { id: 's1', name: 'demo-skill', status: 'ACTIVE', pattern: 'demo pattern' },
    ]);
    bus.provide(ListCronJobs, () => []);
    bus.provide(GetMCPStatus, () => ({ servers: [], statusMessage: 'No MCP servers' }));
    bus.provide(ListSessions, () => [
      {
        sessionId: 'sess-1',
        createdAt: '2026-01-01',
        firstMessage: 'hello',
        messageCount: 2,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.showPage('skills');
    expect(tui.getSnapshot().page?.title).toBe('技能列表');
    expect(tui.getSnapshot().page?.body).toContain('demo-skill');

    await tui.showPage('cron');
    expect(tui.getSnapshot().page?.body).toContain('暂无定时任务');

    await tui.showPage('mcp');
    expect(tui.getSnapshot().page?.body).toContain('No MCP servers');

    await tui.showPage('sessions');
    expect(tui.getSnapshot().page?.body).toContain('sess-1');

    await tui.showPage('help');
    expect(tui.getSnapshot().page?.body).toContain('/skills');

    tui.dismissPage();
    expect(tui.getSnapshot().page).toBeNull();
    await bus.shutdown();
  });

  test('sidebar tabs lazily load an expandable file tree with git status', async () => {
    const bus = new AsyncEventBus();
    const children: Record<string, Array<{ path: string; name: string; depth: number; type: 'file' | 'dir' }>> = {
      '': [{ path: 'src', name: 'src', depth: 0, type: 'dir' }],
      src: [
        { path: 'src/app.ts', name: 'app.ts', depth: 0, type: 'file' },
        { path: 'src/lib', name: 'lib', depth: 0, type: 'dir' },
      ],
      'src/lib': [{ path: 'src/lib/x.ts', name: 'x.ts', depth: 0, type: 'file' }],
    };
    bus.provide(ListDirectory, (req) => ({
      root: '/repo',
      path: req.path,
      entries: children[req.path] ?? [],
      isGitRepo: req.path === '',
      gitStatus: req.path === '' ? { 'src/app.ts': 'M' } : {},
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.setSidebarTab('files');
    expect(tui.getSnapshot().sidebarTab).toBe('files');
    expect(tui.getSnapshot().fileTree.root).toBe('/repo');
    expect(tui.getSnapshot().fileTree.isGitRepo).toBe(true);

    // Lazy: only immediate root children are loaded initially.
    let rows = tui.getSnapshot().fileRows;
    expect(rows.map((row) => row.name)).toEqual(['src']);
    expect(rows[0]?.gitStatus).toBe('M');

    await tui.expandSelectedDir();
    rows = tui.getSnapshot().fileRows;
    expect(rows.map((row) => row.name)).toEqual(['src', 'app.ts', 'lib']);
    expect(rows[1]?.gitStatus).toBe('M');

    tui.moveFileSelection(2);
    await tui.expandSelectedDir();
    rows = tui.getSnapshot().fileRows;
    expect(rows.map((row) => row.name)).toEqual(['src', 'app.ts', 'lib', 'x.ts']);
    expect(rows[2]?.expanded).toBe(true);

    tui.collapseSelectedDir();
    rows = tui.getSnapshot().fileRows;
    expect(rows.map((row) => row.name)).toEqual(['src', 'app.ts', 'lib']);

    // The 5s refresh must preserve expansion + selection.
    tui.moveFileSelection(2);
    await tui.expandSelectedDir();
    await tui.refreshFileTree();
    rows = tui.getSnapshot().fileRows;
    expect(rows.map((row) => row.name)).toEqual(['src', 'app.ts', 'lib', 'x.ts']);
    expect(tui.getSnapshot().fileSelected).toBe(2);
    expect(rows[2]?.expanded).toBe(true);

    await tui.setSidebarTab('overview');
    expect(tui.getSnapshot().sidebarTab).toBe('overview');
    await bus.shutdown();
  });

  test('agents page shows registered agents and tasks', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, () => [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 's1',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 's2',
        role: 'worker',
        status: 'running',
      },
    ]);
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-1',
        agentId: 'worker-1',
        sessionId: 's2',
        prompt: 'work',
        status: 'completed',
        result: 'done',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('agents');
    const page = tui.getSnapshot().page;
    expect(page?.title).toBe('Agent Team');
    expect(page?.body).toContain('main');
    expect(page?.body).toContain('worker-1');
    expect(page?.body).toContain('task-1');
    expect(page?.body).toContain('done');
    await bus.shutdown();
  });

  test('agents page refreshes when task events arrive', async () => {
    const bus = new AsyncEventBus();
    let tasks: Array<Record<string, unknown>> = [];
    bus.provide(ListAgents, () => [
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 's1',
        role: 'worker',
        status: 'idle',
      },
    ]);
    bus.provide(ListAgentTasks, () => tasks as never);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('agents');
    expect(tui.getSnapshot().page?.body).toContain('当前 session 无任务');

    tasks = [
      {
        taskId: 'task-live',
        agentId: 'worker-1',
        sessionId: 's1',
        prompt: 'work',
        status: 'completed',
        result: 'live result',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
    ];
    bus.publish(
      new AgentTaskFinished({
        task: {
          taskId: 'task-live',
          agentId: 'worker-1',
          sessionId: 's1',
          prompt: 'work',
          status: 'completed',
          result: 'live result',
          error: '',
          startedAt: 1,
          finishedAt: 2,
        },
      }),
    );
    await bus.idle();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(tui.getSnapshot().page?.body).toContain('task-live');
    expect(tui.getSnapshot().page?.body).toContain('live result');
    await bus.shutdown();
  });

  test('mailbox page shows unread and read messages', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, () => [
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 's1',
        role: 'worker',
        status: 'idle',
      },
    ]);
    bus.provide(ListAgentMessages, () => [
      {
        messageId: 'm1',
        fromAgentId: 'main',
        toAgentId: 'worker-1',
        teamId: 'default',
        content: 'unread task',
        at: '2026-01-01T00:00:00Z',
        read: false,
      },
      {
        messageId: 'm2',
        fromAgentId: 'main',
        toAgentId: 'worker-1',
        teamId: 'default',
        content: 'old task',
        at: '2026-01-01T00:00:01Z',
        read: true,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('mailbox');
    const page = tui.getSnapshot().page;
    expect(page?.title).toBe('Agent Mailbox');
    expect(page?.body).toContain('1 unread');
    expect(page?.body).toContain('unread task');
    expect(page?.body).toContain('✓ main: old task');
    await bus.shutdown();
  });

  test('lineage page renders agent and session trees', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, () => [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 'session-main',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 'session-worker',
        role: 'worker',
        status: 'idle',
      },
    ]);
    bus.provide(ListSessions, () => [
      {
        sessionId: 'session-main',
        createdAt: '2026-01-01T00:00:00Z',
        firstMessage: 'main',
        messageCount: 2,
        agentId: 'main',
      },
      {
        sessionId: 'session-fork',
        createdAt: '2026-01-01T00:00:01Z',
        firstMessage: 'fork',
        messageCount: 3,
        parentSessionId: 'session-main',
        forkBoundarySeq: 4,
        agentId: 'main',
      },
      {
        sessionId: 'session-worker',
        createdAt: '2026-01-01T00:00:02Z',
        firstMessage: 'worker',
        messageCount: 1,
        agentId: 'worker-1',
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('lineage');
    const body = tui.getSnapshot().page?.body ?? '';
    expect(tui.getSnapshot().page?.title).toBe('Team / Session Lineage');
    expect(body).toContain('- main [lead]');
    expect(body).toContain('  - worker-1 [worker]');
    expect(body).toContain('- session-fork @4 agent=main');
    expect(body).toContain('  - session-fork @4 agent=main');
    await bus.shutdown();
  });

  test('tasks page shows task details', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-detail',
        agentId: 'worker-1',
        sessionId: 'session-worker',
        parentSessionId: 'session-main',
        prompt: 'do detail work',
        status: 'completed',
        result: 'detail result',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('tasks');
    const body = tui.getSnapshot().page?.body ?? '';
    expect(tui.getSnapshot().page?.title).toBe('Agent Tasks');
    expect(body).toContain('task-detail [completed]');
    expect(body).toContain('parent=session-main worker=session-worker');
    expect(body).toContain('result: detail result');
    await bus.shutdown();
  });

  test('team filter scopes agents and tasks', async () => {
    const bus = new AsyncEventBus();
    let requestedTeam = '';
    bus.provide(ListAgents, (req) => {
      requestedTeam = req.teamId;
      return [
        {
          agentId: 'a1',
          parentAgentId: '',
          teamId: 'team-a',
          sessionId: 's1',
          role: 'lead',
          status: 'idle',
        },
      ];
    });
    bus.provide(ListSessions, () => [
      {
        sessionId: 'session-a',
        createdAt: '2026-01-01T00:00:00Z',
        firstMessage: 'a',
        messageCount: 1,
        agentId: 'a1',
      },
      {
        sessionId: 'session-b',
        createdAt: '2026-01-01T00:00:01Z',
        firstMessage: 'b',
        messageCount: 1,
        agentId: 'b1',
      },
    ]);
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-a',
        agentId: 'a1',
        sessionId: 's1',
        teamId: 'team-a',
        prompt: 'a',
        status: 'completed',
        result: 'a',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
      {
        taskId: 'task-b',
        agentId: 'b1',
        sessionId: 's2',
        teamId: 'team-b',
        prompt: 'b',
        status: 'completed',
        result: 'b',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setTeamFilter('team-a');
    await tui.showPage('tasks');
    let body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('task-a');
    expect(body).not.toContain('task-b');
    await tui.showPage('agents');
    expect(requestedTeam).toBe('team-a');
    tui.setTeamFilter('');
    await tui.showPage('agents');
    expect(requestedTeam).toBe('');
    await bus.shutdown();
  });

  test('teams page summarizes agents and tasks per team', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, () => [
      {
        agentId: 'a1',
        parentAgentId: '',
        teamId: 'team-a',
        sessionId: 's1',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'b1',
        parentAgentId: '',
        teamId: 'team-b',
        sessionId: 's2',
        role: 'lead',
        status: 'running',
      },
    ]);
    bus.provide(ListSessions, () => [
      {
        sessionId: 'session-a',
        createdAt: '2026-01-01T00:00:00Z',
        firstMessage: 'a',
        messageCount: 1,
        agentId: 'a1',
      },
      {
        sessionId: 'session-b',
        createdAt: '2026-01-01T00:00:01Z',
        firstMessage: 'b',
        messageCount: 1,
        agentId: 'b1',
      },
    ]);
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-a',
        agentId: 'a1',
        sessionId: 's1',
        teamId: 'team-a',
        prompt: 'a',
        status: 'completed',
        result: 'a',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
      {
        taskId: 'task-b',
        agentId: 'b1',
        sessionId: 's2',
        teamId: 'team-b',
        prompt: 'b',
        status: 'running',
        result: '',
        error: '',
        startedAt: 1,
        finishedAt: null,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('teams');
    const body = tui.getSnapshot().page?.body ?? '';
    expect(tui.getSnapshot().page?.title).toBe('Teams');
    expect(body).toContain('team-a: agents=1 running=0 tasks=1 task_running=0 completed=1');
    expect(body).toContain('team-b: agents=1 running=1 tasks=1 task_running=1 completed=0');
    await bus.shutdown();
  });

  test('task detail page links to parent session events', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-detail-2',
        agentId: 'worker-1',
        sessionId: 'session-worker',
        parentSessionId: 'session-main',
        prompt: 'work',
        status: 'failed',
        result: '',
        error: 'boom',
        failureReason: 'agent_error',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    bus.provide(GetSessionProjection, () => ({
      messages: [],
      rawMessages: [],
      events: [
        {
          seq: 3,
          type: 'agent_task_started',
          at: '2026-01-01T00:00:00Z',
          data: { taskId: 'task-detail-2' },
        },
        {
          seq: 4,
          type: 'agent_task_finished',
          at: '2026-01-01T00:00:01Z',
          data: { taskId: 'task-detail-2' },
        },
      ],
      turns: [],
      messageHeaderSeqs: [],
      customEvents: [],
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showTaskDetail('task-detail-2');
    const page = tui.getSnapshot().page;
    expect(page?.title).toBe('Task Detail');
    expect(page?.body).toContain('status: failed');
    expect(page?.body).toContain('failure_reason: agent_error');
    expect(page?.body).toContain('seq=3 agent_task_started');
    expect(page?.body).toContain('seq=4 agent_task_finished');
    await bus.shutdown();
  });

  test('filters conversation turns by selected agent', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, () => [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 'session-main',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 'session-worker',
        role: 'worker',
        status: 'running',
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await new Promise((resolve) => setTimeout(resolve, 0));

    bus.publish(
      new TurnStarted({
        agentId: 'main',
        sessionId: 'session-main',
        turnId: 'turn-main',
        kind: 'user',
        userInput: 'main question',
        attachments: [],
      }),
    );
    bus.publish(
      new TokenEmitted({
        agentId: 'main',
        sessionId: 'session-main',
        turnId: 'turn-main',
        delta: 'main answer',
        streamId: '',
      }),
    );
    bus.publish(
      new TurnStarted({
        agentId: 'worker-1',
        sessionId: 'session-worker',
        turnId: 'turn-worker',
        kind: 'user',
        userInput: 'worker question',
        attachments: [],
      }),
    );
    bus.publish(
      new TokenEmitted({
        agentId: 'worker-1',
        sessionId: 'session-worker',
        turnId: 'turn-worker',
        delta: 'worker answer',
        streamId: '',
      }),
    );
    await bus.idle();

    expect(tui.getSnapshot().turns.map((turn) => turn.turnId)).toEqual(['turn-main']);
    expect(tui.getSnapshot().sidebarAgents).toHaveLength(2);
    expect(tui.getSnapshot().viewAgentId).toBe('main');

    tui.setViewAgent('worker-1');
    const workerTurns = tui.getSnapshot().turns;
    expect(workerTurns.map((turn) => turn.turnId)).toEqual(['turn-worker']);
    expect(workerTurns[0]?.content).toBe('worker answer');
    expect(tui.getSnapshot().viewAgentId).toBe('worker-1');

    tui.setViewAgent('main');
    expect(tui.getSnapshot().turns.map((turn) => turn.turnId)).toEqual(['turn-main']);
    await tui.setSidebarTab('agents');
    expect(tui.getSnapshot().sidebarTab).toBe('agents');
    await bus.shutdown();
  });

  test('loads a worker session and targets the selected agent', async () => {
    const bus = new AsyncEventBus();
    const events = [
      {
        seq: 1,
        type: 'turn_started',
        at: '2026-01-01T00:00:00Z',
        data: {
          turnId: 'tw',
          userInput: 'worker old question',
          kind: 'user',
          agentId: 'worker-1',
        },
      },
      {
        seq: 2,
        type: 'user_message',
        at: '2026-01-01T00:00:00Z',
        data: { message: { role: 'user', content: 'worker old question' } },
      },
      {
        seq: 3,
        type: 'assistant_message',
        at: '2026-01-01T00:00:01Z',
        data: { message: { role: 'assistant', content: 'worker old answer' } },
      },
    ];
    bus.provide(LoadSession, () => ({
      sessionId: 'worker-session',
      agentId: 'worker-1',
      events,
      messages: [],
      cronHistory: [],
      formatVersion: 3,
    }));
    bus.provide(ReplaceSessionEvents, () => ({ replaced: events.length }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.loadAgentSession('worker-1', 'worker-session');
    expect(tui.getSnapshot().viewAgentId).toBe('worker-1');
    expect(tui.getSnapshot().turns.some((turn) => turn.content.includes('worker old answer'))).toBe(
      true,
    );

    const turns: UserTurnRequested[] = [];
    await bus.subscribe(UserTurnRequested, (event) => {
      turns.push(event);
    });
    tui.submitUserTurn('continue worker');
    await bus.idle();
    expect(turns[0]?.targetAgentId).toBe('worker-1');
    expect(turns[0]?.sessionId).toBe('worker-session');
    await bus.shutdown();
  });

  test('shows a toast when a session cannot be loaded', async () => {
    const bus = new AsyncEventBus();
    bus.provide(LoadSession, () => null);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    expect(await tui.loadSession('missing-session')).toBe(false);
    expect(tui.getSnapshot().toast).toContain('会话不存在');
    await bus.shutdown();
  });

  test('newSession creates a fresh main session and clears the view', async () => {
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    await session.start(bus);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('old-session');
    bus.publish(
      new TurnStarted({
        sessionId: 'old-session',
        turnId: 'old-turn',
        kind: 'user',
        userInput: 'old question',
      }),
    );
    bus.publish(
      new TokenEmitted({
        sessionId: 'old-session',
        turnId: 'old-turn',
        delta: 'old answer',
      }),
    );
    await bus.idle();
    expect(tui.getSnapshot().turns).toHaveLength(1);

    const next = await tui.newSession();
    expect(next).toMatch(/^session-/);
    expect(next).not.toBe('old-session');
    expect(tui.getSnapshot().sessionId).toBe(next);
    expect(tui.getSnapshot().turns).toEqual([]);
    expect(tui.getSnapshot().viewAgentId).toBe('main');
    const projection = await bus.request(
      new GetSessionProjection({ sessionId: next }),
    );
    expect(projection.events.some((event) => event.type === 'session_created')).toBe(
      true,
    );
    await bus.shutdown();
  });

  test('restores full main history after switching to a worker and back', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, () => [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 'main-session',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 'worker-session',
        role: 'worker',
        status: 'idle',
      },
    ]);
    const mainEvents = [
      {
        seq: 1,
        type: 'turn_started',
        at: '2026-01-01T00:00:00Z',
        data: {
          turnId: 'main-turn',
          userInput: 'main question',
          kind: 'user',
          agentId: 'main',
        },
      },
      {
        seq: 2,
        type: 'user_message',
        at: '2026-01-01T00:00:00Z',
        data: { message: { role: 'user', content: 'main question' } },
      },
      {
        seq: 3,
        type: 'assistant_message',
        at: '2026-01-01T00:00:01Z',
        data: { message: { role: 'assistant', content: 'main answer' } },
      },
    ];
    const workerEvents = [
      {
        seq: 1,
        type: 'turn_started',
        at: '2026-01-01T00:00:02Z',
        data: {
          turnId: 'worker-turn',
          userInput: 'worker question',
          kind: 'user',
          agentId: 'worker-1',
        },
      },
      {
        seq: 2,
        type: 'user_message',
        at: '2026-01-01T00:00:02Z',
        data: { message: { role: 'user', content: 'worker question' } },
      },
      {
        seq: 3,
        type: 'assistant_message',
        at: '2026-01-01T00:00:03Z',
        data: { message: { role: 'assistant', content: 'worker answer' } },
      },
    ];
    bus.provide(LoadSession, (req) => {
      const isWorker = req.targetSessionId === 'worker-session';
      return {
        sessionId: isWorker ? 'worker-session' : 'main-session',
        agentId: isWorker ? 'worker-1' : 'main',
        events: isWorker ? workerEvents : mainEvents,
        messages: [],
        cronHistory: [],
        formatVersion: 3,
      };
    });
    bus.provide(ReplaceSessionEvents, (req) => ({ replaced: req.events.length }));
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-worker-switch',
        agentId: 'worker-1',
        sessionId: 'worker-session',
        prompt: 'worker question',
        status: 'completed',
        result: 'worker answer',
        error: '',
        parentSessionId: 'main-session',
        teamId: 'default',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('main-session');
    await tui.selectAgent('main');
    expect(tui.getSnapshot().turns.some((turn) => turn.content.includes('main answer'))).toBe(
      true,
    );

    await tui.selectAgent('worker-1');
    expect(tui.getSnapshot().viewAgentId).toBe('worker-1');
    expect(tui.getSnapshot().turns.some((turn) => turn.content.includes('worker answer'))).toBe(
      true,
    );

    await tui.selectAgent('main');
    expect(tui.getSnapshot().viewAgentId).toBe('main');
    expect(tui.getSnapshot().sessionId).toBe('main-session');
    const contents = tui.getSnapshot().turns.map((turn) => turn.content).join('\n');
    expect(contents).toContain('main answer');
    expect(contents).not.toContain('worker answer');
    await bus.shutdown();
  });

  test('disposes a worker from the TUI and refreshes the sidebar', async () => {
    const bus = new AsyncEventBus();
    let agents: AgentInfo[] = [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 'main-session',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'worker-1',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 'worker-session',
        role: 'worker',
        status: 'running',
      },
    ];
    bus.provide(ListAgents, () => agents);
    bus.provide(DisposeAgent, (req) => {
      const before = agents.length;
      agents = agents.filter((agent) => agent.agentId !== req.agentId);
      return agents.length < before;
    });
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.refreshSidebar();
    expect(tui.getSnapshot().sidebarAgents.map((agent) => agent.agentId)).toContain('worker-1');

    const result = await tui.disposeAgent('worker-1');
    expect(result.ok).toBe(true);
    expect(result.message).toContain('worker-1');
    expect(tui.getSnapshot().sidebarAgents.map((agent) => agent.agentId)).not.toContain(
      'worker-1',
    );
    expect((await tui.disposeAgent('main')).ok).toBe(false);
    await bus.shutdown();
  });

  test('task pages default to the current session and support all', async () => {
    const bus = new AsyncEventBus();
    const tasks: AgentTaskInfo[] = [
      {
        taskId: 'task-current',
        agentId: 'worker-1',
        sessionId: 'worker-session-1',
        prompt: 'current work',
        status: 'completed',
        result: 'current result',
        error: '',
        parentSessionId: 'current-session',
        teamId: 'default',
        startedAt: 1,
        finishedAt: 2,
      },
      {
        taskId: 'task-other',
        agentId: 'worker-2',
        sessionId: 'worker-session-2',
        prompt: 'other work',
        status: 'failed',
        result: '',
        error: 'other error',
        parentSessionId: 'other-session',
        teamId: 'default',
        startedAt: 3,
        finishedAt: 4,
      },
    ];
    bus.provide(ListAgentTasks, () => tasks);
    bus.provide(ListAgents, () => [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 'current-session',
        role: 'lead',
        status: 'idle',
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('current-session');

    await tui.showPage('tasks');
    let body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('task-current');
    expect(body).not.toContain('task-other');

    tui.setTaskScope('all');
    await tui.showPage('tasks');
    body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('task-current');
    expect(body).toContain('task-other');

    tui.setTaskScope('session');
    await tui.showPage('agents');
    body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('Tasks (current session):');
    expect(body).not.toContain('task-other');
    await bus.shutdown();
  });

  test('sidebar only lists agents related to the current session', async () => {
    const bus = new AsyncEventBus();
    const agents: AgentInfo[] = [
      {
        agentId: 'main',
        parentAgentId: '',
        teamId: 'default',
        sessionId: 'current-session',
        role: 'lead',
        status: 'idle',
      },
      {
        agentId: 'worker-current',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 'worker-current-session',
        role: 'worker',
        status: 'idle',
      },
      {
        agentId: 'worker-other',
        parentAgentId: 'main',
        teamId: 'default',
        sessionId: 'worker-other-session',
        role: 'worker',
        status: 'idle',
      },
      {
        agentId: 'worker-grandchild',
        parentAgentId: 'worker-current',
        teamId: 'default',
        sessionId: 'worker-grandchild-session',
        role: 'worker',
        status: 'idle',
      },
    ];
    bus.provide(ListAgents, () => agents);
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-current',
        agentId: 'worker-current',
        sessionId: 'worker-current-session',
        prompt: 'current',
        status: 'completed',
        result: 'ok',
        error: '',
        parentSessionId: 'current-session',
        teamId: 'default',
        startedAt: 1,
        finishedAt: 2,
      },
      {
        taskId: 'task-other',
        agentId: 'worker-other',
        sessionId: 'worker-other-session',
        prompt: 'other',
        status: 'completed',
        result: 'ok',
        error: '',
        parentSessionId: 'other-session',
        teamId: 'default',
        startedAt: 3,
        finishedAt: 4,
      },
      {
        taskId: 'task-grandchild',
        agentId: 'worker-grandchild',
        sessionId: 'worker-grandchild-session',
        prompt: 'nested',
        status: 'completed',
        result: 'ok',
        error: '',
        parentSessionId: 'worker-current-session',
        teamId: 'default',
        startedAt: 5,
        finishedAt: 6,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('current-session');
    await tui.refreshSidebar();
    const rows = tui.getSnapshot().sidebarAgents;
    const ids = rows.map((agent) => agent.agentId);
    expect(ids).toEqual(['main', 'worker-current', 'worker-grandchild']);
    expect(ids).not.toContain('worker-other');
    expect(rows.map((agent) => agent.depth)).toEqual([0, 1, 2]);
    await bus.shutdown();
  });

  test('sidebar excludes disposed/resumable agents', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListAgents, (req) => {
      const active: AgentInfo[] = [
        {
          agentId: 'main',
          parentAgentId: '',
          teamId: 'default',
          sessionId: 'main-session',
          role: 'lead',
          status: 'idle',
        },
      ];
      if (!req.includeResumable) return active;
      return [
        ...active,
        {
          agentId: 'worker-disposed',
          parentAgentId: 'main',
          teamId: 'default',
          sessionId: 'worker-disposed-session',
          role: 'worker',
          status: 'resumable',
        },
      ];
    });
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 'task-disposed',
        agentId: 'worker-disposed',
        sessionId: 'worker-disposed-session',
        prompt: 'old',
        status: 'completed',
        result: 'ok',
        error: '',
        parentSessionId: 'main-session',
        teamId: 'default',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('main-session');
    await tui.refreshSidebar();
    const ids = tui.getSnapshot().sidebarAgents.map((agent) => agent.agentId);
    expect(ids).not.toContain('worker-disposed');
    await bus.shutdown();
  });

  test('/reflect publishes a forced reflection command', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('session-reflect');
    const seen: ReflectSkills[] = [];
    await bus.subscribe(ReflectSkills, (event) => { seen.push(event); });
    const result = tui.reflectSkills();
    await bus.idle();
    expect(result.ok).toBe(true);
    expect(seen[0]?.force).toBe(true);
    expect(seen[0]?.sessionId).toBe('session-reflect');
    await bus.shutdown();
  });

  test('prompt page shows the assembled prompt sections', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetPromptInfo, () => ({
      userPromptPath: '/tmp/system-prompt.md',
      userPrompt: 'USER',
      agentsPath: '/tmp/AGENTS.md',
      sections: [
        { id: 'base', content: 'USER', order: 0, source: '/tmp/system-prompt.md' },
        { id: 'tools', title: 'Tools', content: 'tool body', order: 20, source: 'tools' },
      ],
      prompt: 'USER\n\ntool body',
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('prompt');
    const page = tui.getSnapshot().page;
    expect(page?.title).toBe('System Prompt');
    expect(page?.body).toContain('/tmp/system-prompt.md');
    expect(page?.body).toContain('/tmp/AGENTS.md');
    expect(page?.body).toContain('tools');
    expect(page?.body).toContain('tool body');
    await bus.shutdown();
  });

  test('context page shows memory window usage and recent messages', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetSessionProjection, () => ({
      messages: [
        userMessage('hello there'),
        assistantMessage('hi, how can I help?'),
        userMessage('run the tests'),
      ],
      rawMessages: [],
      events: [],
      turns: [],
      messageHeaderSeqs: [],
      customEvents: [],
    }));
    bus.provide(GetSettings, () => ({
      provider: 'deepseek',
      baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-chat',
      apiKey: '',
      maxContextTokens: 128_000,
      maxOutputTokens: 4_096,
      compactThresholdRatio: 0.8,
      thinkingEnabled: true,
      effort: 'high',
      imageMaxDimension: 2_048,
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('s1');

    bus.publish(
      new TokenUsageReported({
        sessionId: 's1',
        turnId: 't1',
        promptTokens: 1_234,
        completionTokens: 56,
        totalTokens: 1_290,
        cacheHitTokens: 800,
        cacheMissTokens: 434,
      }),
    );
    await bus.idle();
    await tui.showPage('context');
    const page = tui.getSnapshot().page;
    expect(page?.kind).toBe('context');
    expect(page?.title).toBe('上下文窗口');
    expect(page?.body).toContain('上下文：1,234 / 128,000');
    expect(page?.body).toContain('本次会话：输入 1,234 · 输出 56 · 请求 1 次');
    expect(page?.body).toContain('缓存：命中 800 · 未命中 434 · 命中率 64.8%');
    expect(page?.body).toContain('最近请求：prompt 1,234 · completion 56 · total 1,290');
    expect(page?.body).toContain('历史消息：3 条（无数量上限）');
    expect(page?.body).toContain('最近消息');
    expect(page?.body).toContain('hello there');
    await bus.shutdown();
  });

  test('settings page masks keys and accepts K/M token settings', async () => {
    const bus = new AsyncEventBus();
    const current: SettingsSnapshot = {
      provider: 'deepseek',
      baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-chat',
      apiKey: 'sk-secret-1234',
      maxContextTokens: 131_072,
      maxOutputTokens: 4_096,
      compactThresholdRatio: 0.8,
      imageMaxDimension: 2_048,
      thinkingEnabled: true,
      effort: 'high',
      maxIterations: 10,
      autoReflect: true,
      reflectionMinTools: 3,
      reflectionMinWriteTools: 2,
    };
    bus.provide(GetSettings, () => ({ ...current }));
    bus.provide(UpdateSettings, (req) => {
      Object.assign(current, req.patch);
      return { ...current };
    });
    bus.provide(SetModelEffort, (req) => ({
      thinkingEnabled: req.thinkingEnabled,
      effort: req.effort,
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.showPage('settings');
    let body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('模型设置');
    expect(body).toContain('sk-s…1234');
    expect(body).toContain('128K');
    expect(body).toContain('4,096');
    expect(body).toContain('thinking      on');
    expect(body).toContain('effort        high');
    expect(body).toContain('max_iterations 10');
    expect(body).toContain('auto_reflect   on');
    expect(body).not.toContain('可用键');
    expect(body).not.toContain('max_context 会减去');
    expect(body).toContain('reflection_min_tools 3');
    const fields = tui.getSnapshot().page?.fields ?? [];
    expect(fields.find((field) => field.key === 'max_context')?.editValue).toBe('128K');
    expect(fields.find((field) => field.key === 'api_key')?.value).toContain('sk-s…1234');
    expect(fields.find((field) => field.key === 'api_key')?.secret).toBe(true);

    const ok = await tui.updateSetting('max_context', '1M');
    expect(ok.ok).toBe(true);
    body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('1M');
    expect(current.maxContextTokens).toBe(1_048_576);

    const threshold = await tui.updateSetting('compact_threshold', '0.7');
    expect(threshold.ok).toBe(true);
    expect(current.compactThresholdRatio).toBe(0.7);
    expect(tui.getSnapshot().page?.body).toContain('compact_ratio 0.7');
    const ratio = await tui.updateSetting('compact_ratio', '0.6');
    expect(ratio.ok).toBe(true);
    expect(current.compactThresholdRatio).toBe(0.6);

    const badThreshold = await tui.updateSetting('compact_threshold', '2');
    expect(badThreshold.ok).toBe(false);

    const iterations = await tui.updateSetting('max_iterations', '3');
    expect(iterations.ok).toBe(true);
    expect(current.maxIterations).toBe(3);
    expect(tui.getSnapshot().page?.body).toContain('max_iterations 3');
    const badIterations = await tui.updateSetting('max_iterations', '0');
    expect(badIterations.ok).toBe(false);

    const autoReflect = await tui.updateSetting('auto_reflect', 'off');
    expect(autoReflect.ok).toBe(true);
    expect(current.autoReflect).toBe(false);
    const minTools = await tui.updateSetting('reflection_min_tools', '5');
    expect(minTools.ok).toBe(true);
    expect(current.reflectionMinTools).toBe(5);

    const max = await tui.setEffort('max');
    expect(max.ok).toBe(true);
    expect(tui.getSnapshot().effort).toBe('max');
    expect(current.effort).toBe('max');
    expect(current.thinkingEnabled).toBe(true);

    const off = await tui.setEffort('nothinking');
    expect(off.ok).toBe(true);
    expect(tui.getSnapshot().effort).toBe('nothinking');
    expect(current.thinkingEnabled).toBe(false);

    const invalid = await tui.updateSetting('max_output', 'nope');
    expect(invalid.ok).toBe(false);
    expect(invalid.message).toContain('无效');
    await bus.shutdown();
  });

  test('reflections page lists recent reflection changes', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    bus.publish(
      new SkillsReflected({
        created: 1,
        updated: 2,
        deprecated: 1,
        names: ['debug-import'],
        updatedNames: ['greet', 'deploy'],
      }),
    );
    await bus.idle();
    await tui.showPage('reflections');

    const page = tui.getSnapshot().page;
    expect(page?.kind).toBe('reflections');
    expect(page?.title).toBe('反思记录');
    expect(page?.body).toContain('+1 ~2 -1');
    expect(page?.body).toContain('新建：debug-import');
    expect(page?.body).toContain('更新：greet, deploy');
    expect(page?.body).toContain('废弃：1 个技能');
    await bus.shutdown();
  });

  test('sessions page sorts by recency and renders multi-line entries', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListSessions, () => [
      {
        sessionId: 'older-session',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
        firstMessage: 'older message',
        messageCount: 1,
      },
      {
        sessionId: 'recent-session',
        createdAt: '2026-02-01T00:00:00Z',
        updatedAt: '2026-03-01T00:00:00Z',
        firstMessage: 'recent message',
        messageCount: 7,
      },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.showPage('sessions');
    const body = tui.getSnapshot().page?.body ?? '';
    expect(body.indexOf('recent-session')).toBeLessThan(body.indexOf('older-session'));
    expect(body).toContain('7 msgs');
    expect(body).toContain('recent message');
    expect(body).toMatch(/1\. recent-session/);
    expect(body).toMatch(/2\. older-session/);
    expect(body.split('\n').filter((line) => line.includes('recent-session'))).toHaveLength(1);
    expect(body.split('\n').filter((line) => line.includes('recent message'))).toHaveLength(1);
    await bus.shutdown();
  });

  test('session log page shows event types and payloads', async () => {
    const bus = new AsyncEventBus();
    bus.provide(LoadSession, () => ({
      sessionId: 's1',
      messageCount: 1,
      events: [
        {
          seq: 1,
          type: 'user_message',
          at: '2026-01-01T00:00:00Z',
          data: { message: { role: 'user', content: 'hello' } },
        },
        {
          seq: 2,
          type: 'replace_messages',
          at: '2026-01-01T00:00:01Z',
          data: { messages: [{ role: 'user', content: 'summary' }] },
        },
      ],
      cronHistory: [],
      formatVersion: 3,
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    tui.setSessionId('s1');
    await tui.showPage('log');
    const page = tui.getSnapshot().page;
    const body = page?.body ?? '';
    expect(page?.title).toBe('会话事件日志');
    expect(body).toContain('共 2 个事件');
    expect(body).toContain('user_message');
    expect(body).toContain('replace_messages');
    await bus.shutdown();
  });

  test('loads a persisted session, replaces memory and rebuilds the view', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-tui-resume-'));
    tempRoots.push(dir);
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const store = new StoreModule({ sessionsDir: join(dir, 'sessions') });
    const tui = new TuiModule();
    await session.start(bus);
    await store.start(bus);
    await tui.start(bus);
    await bus.start();
    await store.saveSession('sess-a', [
      userMessage('old question'),
      assistantMessage('old answer'),
    ]);

    await tui.showPage('sessions');
    expect(tui.getSnapshot().page?.body).toContain('1. sess-a');

    const loaded = await tui.chooseSession(1);
    expect(loaded).toBe(true);
    expect(tui.getSnapshot().sessionId).toBe('sess-a');
    expect(tui.getSnapshot().turns).toHaveLength(1);
    expect(tui.getSnapshot().turns[0]?.userInput).toBe('old question');
    expect(tui.getSnapshot().turns[0]?.content).toBe('old answer');
    expect(tui.getSnapshot().page).toBeNull();

    const history = (
      await bus.request(new GetSessionProjection({ sessionId: 'sess-a' }))
    ).messages;
    expect(history.map((message) => message.content)).toEqual(['old question', 'old answer']);
    await bus.shutdown();
  });

  test('sidebar refreshes counters from the bus', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => [
      { name: 'a', description: '', jsonSchema: {} },
      { name: 'b', description: '', jsonSchema: {} },
    ]);
    bus.provide(ListSkills, () => [{ id: 's1', name: 'x', status: 'ACTIVE' }]);
    bus.provide(ListCronJobs, () => [
      { id: 'j1', name: 'job', status: 'SCHEDULED', prompt: 'p', recurring: true, durable: false, cron: '* * * * *', runsDone: 0, nextRunAt: null, lastStartedAt: null, lastFinishedAt: null, lastResult: '', lastError: '' },
    ]);
    bus.provide(GetMCPStatus, () => ({ servers: [], statusMessage: 'MCP: 1 connected' }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.refreshSidebar();
    expect(tui.getSnapshot().sidebar).toEqual({
      tools: 2,
      skills: 1,
      cron: 1,
      mcpStatus: 'MCP: 1 connected',
    });
    await tui.stop();
    await bus.shutdown();
  });

  test('MCP page expands and collapses server tools by index', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetMCPStatus, () => ({
      statusMessage: 'MCP: 1 server(s) connected',
      servers: [
        {
          name: 'demo-server',
          status: 'CONNECTED',
          transport: 'stdio',
          tool_count: 1,
          tools: [{ name: 'echo', description: 'Echo text' }],
        },
      ],
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    await tui.showPage('mcp');
    expect(tui.getSnapshot().page?.body).toContain('1. ▶ demo-server');
    expect(tui.getSnapshot().page?.body).not.toContain('🔧 echo');

    expect(tui.toggleMcpServer(1)).toBe(true);
    expect(tui.getSnapshot().page?.body).toContain('▼ demo-server');
    expect(tui.getSnapshot().page?.body).toContain('🔧 echo');

    expect(tui.toggleMcpServer(1)).toBe(true);
    expect(tui.getSnapshot().page?.body).not.toContain('🔧 echo');
    expect(tui.toggleMcpServer(9)).toBe(false);
    await bus.shutdown();
  });

  test('feedback targets the last turn', async () => {
    const { bus, tui } = await startedTui();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 't1', userInput: 'x' }));
    bus.publish(new TurnCompleted({ sessionId: 's1', turnId: 't1', content: 'y', messages: [] }));
    await bus.idle();
    tui.setSessionId('s1');

    const feedback: import('../src/kernel/contracts/chat').FeedbackSubmitted[] = [];
    await bus.subscribe(FeedbackSubmitted, (event) => { feedback.push(event); });
    tui.submitFeedback(false);
    await bus.idle();

    expect(feedback[0]?.positive).toBe(false);
    expect(feedback[0]?.turnId).toBe('t1');
    expect(feedback[0]?.sessionId).toBe('s1');
    await bus.shutdown();
  });

  test('toggles thinking/skills/tool output and expires toasts', async () => {
    const { bus, tui } = await startedTui();
    expect(tui.getSnapshot().thinkingExpanded).toBe(false);

    expect(tui.toggleThinking()).toBe(true);
    expect(tui.getSnapshot().thinkingExpanded).toBe(true);
    expect(tui.toggleSkills()).toBe(true);
    expect(tui.getSnapshot().skillsExpanded).toBe(true);
    expect(tui.toggleToolOutput()).toBe(true);
    expect(tui.getSnapshot().toolOutputExpanded).toBe(true);
    expect(tui.getSnapshot().sidebarCollapsed).toBe(true);
    expect(tui.toggleSidebar()).toBe(false);
    expect(tui.getSnapshot().sidebarCollapsed).toBe(false);
    expect(tui.toggleSidebar()).toBe(true);
    expect(tui.getSnapshot().sidebarCollapsed).toBe(true);

    tui.setToast('hello toast', 20);
    expect(tui.getSnapshot().toast).toBe('hello toast');
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(tui.getSnapshot().toast).toBeNull();
    await bus.shutdown();
  });

  test('user intents are published as commands on the bus', async () => {
    const { bus, tui } = await startedTui();
    const turns: UserTurnRequested[] = [];
    const approvals: ToolApprovalResolved[] = [];
    await bus.subscribe(UserTurnRequested, (event) => {
      turns.push(event);
    });
    await bus.subscribe(ToolApprovalResolved, (event) => {
      approvals.push(event);
    });

    tui.submitUserTurn('hello', 's1');
    tui.resolveApproval('req1', true, true);
    await bus.idle();

    expect(turns[0]?.userText).toBe('hello');
    expect(turns[0]?.sessionId).toBe('s1');
    expect(approvals[0]?.granted).toBe(true);
    expect(approvals[0]?.remember).toBe(true);
    await bus.shutdown();
  });
});