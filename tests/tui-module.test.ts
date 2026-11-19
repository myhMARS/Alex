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
import { SkillLoaded, SkillsReflected } from '../src/kernel/contracts/skills';
import {
  ToolApprovalResolved,
  ToolFinished,
  ToolStarted,
} from '../src/kernel/contracts/tools';
import { CronJobEvent, ListCronJobs } from '../src/kernel/contracts/cron';
import { GetMCPStatus, GetToolCatalog, ListDirectory } from '../src/kernel/contracts/tools';
import { ListSkills } from '../src/kernel/contracts/skills';
import { ListSessions, LoadSession } from '../src/kernel/contracts/session';
import { ReplaceMemory, GetContext, GetMemoryStatus } from '../src/kernel/contracts/memory';
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
import { MemoryModule } from '../src/modules/memory/module';
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

  test('context page shows memory window usage and recent messages', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetContext, () => [
      userMessage('hello there'),
      assistantMessage('hi, how can I help?'),
      userMessage('run the tests'),
    ]);
    bus.provide(GetMemoryStatus, () => ({
      messageCount: 3,
      maxTokens: 128_000,
      estimatedTokens: 30,
      maxContextTokens: 128_000,
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
      thinkingEnabled: true,
      effort: 'high',
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

    const ok = await tui.updateSetting('max_context', '1M');
    expect(ok.ok).toBe(true);
    body = tui.getSnapshot().page?.body ?? '';
    expect(body).toContain('1M');
    expect(current.maxContextTokens).toBe(1_048_576);

    const threshold = await tui.updateSetting('compact_threshold', '0.7');
    expect(threshold.ok).toBe(true);
    expect(current.compactThresholdRatio).toBe(0.7);
    expect(tui.getSnapshot().page?.body).toContain('compact_ratio 0.7');

    const badThreshold = await tui.updateSetting('compact_threshold', '2');
    expect(badThreshold.ok).toBe(false);

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

  test('loads a persisted session, replaces memory and rebuilds the view', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-tui-resume-'));
    tempRoots.push(dir);
    const bus = new AsyncEventBus();
    const memory = new MemoryModule();
    const store = new StoreModule({ sessionsDir: join(dir, 'sessions') });
    const tui = new TuiModule();
    await memory.start(bus);
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

    const history = await bus.request(new GetContext({ sessionId: 'sess-a' }));
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