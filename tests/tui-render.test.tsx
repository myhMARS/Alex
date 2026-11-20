import { describe, expect, test } from 'bun:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { testRender } from '@opentui/react/test-utils';

import { AsyncEventBus } from '../src/kernel/bus';
import { TuiModule } from '../src/modules/tui/module';
import { TuiApp } from '../src/tui/app';
import { MarkdownText } from '../src/tui/markdown-view';
import {
  ThinkingUpdated,
  TokenEmitted,
  TurnCompleted,
  TurnDequeued,
  TurnFailed,
  TurnQueued,
  TurnStarted,
  UserTurnRequested,
} from '../src/kernel/contracts/chat';
import {
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolFinished,
  ToolStarted,
} from '../src/kernel/contracts/tools';
import { ListSkills } from '../src/kernel/contracts/skills';
import { DisposeTeam, ListAgentTasks, ListAgents } from '../src/kernel/contracts/agents';
import { GetToolCatalog, GetMCPStatus, ListDirectory, ReloadMCP, SetMCPServerEnabled } from '../src/kernel/contracts/tools';
import {
  CompactContext,
  CompactionStarted,
  ContextCompacted,
} from '../src/kernel/contracts/compaction';
import { ListCronJobs } from '../src/kernel/contracts/cron';
import {
  ListSessions,
  LoadSession,
  ReplaceSessionEvents,
} from '../src/kernel/contracts/session';
import { messageEvent } from '../src/session/projector';
import { FeedbackSubmitted, TokenUsageReported } from '../src/kernel/contracts/chat';
import {
  GetSettings,
  UpdateSettings,
  type SettingsSnapshot,
} from '../src/kernel/contracts/settings';

async function mountedTui(): Promise<{
  bus: AsyncEventBus;
  tui: TuiModule;
  setup: Awaited<ReturnType<typeof testRender>>;
  quitCalls: { count: number };
}> {
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
      taskId: 'task-sidebar',
      agentId: 'worker-1',
      sessionId: 's2',
      prompt: 'sidebar work',
      status: 'running',
      result: '',
      error: '',
      parentSessionId: 's1',
      teamId: 'default',
      startedAt: 1,
      finishedAt: null,
    },
  ]);
  const tui = new TuiModule();
  await tui.start(bus);
  await bus.start();

  bus.publish(new TurnStarted({ sessionId: 's1', turnId: 't1', userInput: 'hello bus' }));
  bus.publish(new TokenEmitted({ sessionId: 's1', turnId: 't1', delta: 'Hi ' }));
  bus.publish(new TokenEmitted({ sessionId: 's1', turnId: 't1', delta: 'there' }));
  bus.publish(
    new TurnCompleted({
      sessionId: 's1',
      turnId: 't1',
      content: 'Hi there',
      messages: [],
    }),
  );
  await bus.idle();

  const quitCalls = { count: 0 };
  const setup = await testRender(
    <TuiApp
      tui={tui}
      sessionId="s1"
      toolCount={16}
      onQuit={() => {
        quitCalls.count += 1;
      }}
    />,
    { width: 100, height: 30 },
  );
  await setup.renderOnce();
  return { bus, tui, setup, quitCalls };
}

/** captureCharFrame renders blank cells with '─' in this OpenTUI version. */
function plain(frame: string): string {
  return frame.replace(/─/g, ' ');
}

describe('TUI renderer', () => {
  test('renders header, turns and submits user input over the bus', async () => {
    const { bus, setup } = await mountedTui();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('Alex');
    expect(frame).toContain('16 tools');
    expect(frame).toContain('hello bus');
    expect(frame).toContain('Hi there');

    const turns: UserTurnRequested[] = [];
    await bus.subscribe(UserTurnRequested, (event) => {
      turns.push(event);
    });

    await setup.mockInput.typeText('next message');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 50));
    await bus.idle();

    expect(turns[0]?.userText).toBe('next message');
    expect(turns[0]?.sessionId).toBe('s1');
    await bus.shutdown();
  });

  test('agents sidebar tab lists main and workers', async () => {
    const { bus, tui, setup } = await mountedTui();
    await new Promise((resolve) => setTimeout(resolve, 20));
    if (tui.getSnapshot().sidebarCollapsed) tui.toggleSidebar();
    await tui.setSidebarTab('agents');
    await bus.idle();
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('AGENTS');
    expect(frame).toContain('CONVERSATION AGENT (2)');
    expect(frame).toContain('main');
    expect(frame).toContain('worker-1');
    expect(frame).toContain('×');
    await bus.shutdown();
  });

  test('renders turn and tool errors instead of hiding them', async () => {
    const { bus, setup } = await mountedTui();
    bus.publish(
      new TurnFailed({
        agentId: 'main',
        sessionId: 's1',
        turnId: 'turn-error',
        error: 'provider request failed: invalid api key',
      }),
    );
    bus.publish(
      new TurnStarted({
        agentId: 'main',
        sessionId: 's1',
        turnId: 'turn-tool-error',
        kind: 'user',
        userInput: 'run failing tool',
        attachments: [],
      }),
    );
    bus.publish(
      new ToolStarted({
        agentId: 'main',
        sessionId: 's1',
        turnId: 'turn-tool-error',
        toolId: 'tool-error-1',
        toolName: 'spawn_agent',
        toolInput: { prompt: 'x' },
      }),
    );
    bus.publish(
      new ToolFinished({
        agentId: 'main',
        sessionId: 's1',
        turnId: 'turn-tool-error',
        toolId: 'tool-error-1',
        output: 'Error: permission denied by policy',
      }),
    );
    await bus.idle();
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('provider request failed: invalid api key');
    expect(frame).toContain('spawn_agent');
    expect(frame).toContain('permission denied by policy');
    expect(frame).toContain('✗');
    await bus.shutdown();
  });

  test('/new starts a new session instead of clearing the view', async () => {
    const { bus, tui, setup } = await mountedTui();
    expect(tui.getSnapshot().sessionId).toBe('s1');
    await setup.mockInput.typeText('/new');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 50));
    await bus.idle();
    expect(tui.getSnapshot().sessionId).not.toBe('s1');
    expect(tui.getSnapshot().sessionId).toMatch(/^session-/);
    expect(tui.getSnapshot().turns).toEqual([]);
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain(tui.getSnapshot().sessionId.slice(0, 12));
    await bus.shutdown();
  });

  test('shows queued user messages and removes them when dequeued', async () => {
    const { bus, setup } = await mountedTui();
    bus.publish(
      new TurnQueued({
        sessionId: 's1',
        agentId: 'main',
        queueId: 'q-1',
        userInput: 'second question while busy',
      }),
    );
    await bus.idle();
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('Queued (1)');
    expect(frame).toContain('second question while busy');

    bus.publish(new TurnDequeued({ sessionId: 's1', agentId: 'main', queueId: 'q-1' }));
    await bus.idle();
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).not.toContain('Queued (1)');
    await bus.shutdown();
  });

  test('removes a queued message from the dock', async () => {
    const { bus, tui, setup } = await mountedTui();
    bus.publish(
      new TurnQueued({
        sessionId: 's1',
        agentId: 'main',
        queueId: 'q-remove',
        userInput: 'remove me',
      }),
    );
    await bus.idle();
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).toContain('remove me');
    const result = tui.cancelQueuedMessage('q-remove');
    expect(result.ok).toBe(true);
    await bus.idle();
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).not.toContain('Queued (1)');
    await bus.shutdown();
  });

  test('/mcp reload reconnects and reports the refreshed catalog', async () => {
    const { bus, setup } = await mountedTui();
    bus.provide(ReloadMCP, () => ({
      ok: true,
      servers: 1,
      connected: 1,
      tools: 2,
      message: 'MCP 已重新加载：1/1 connected · 2 tools',
    }));
    await setup.mockInput.typeText('/mcp reload');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('MCP 已重新加载');
    await bus.shutdown();
  });

  test('Shift+Tab cycles permission modes in the status bar', async () => {
    const { bus, setup } = await mountedTui();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('perm 手动');
    setup.mockInput.pressTab({ shift: true });
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('perm 模型');
    await bus.shutdown();
  });

  test('shows approval modal and resolves it with keyboard shortcuts', async () => {
    const { bus, setup } = await mountedTui();
    const approvals: ToolApprovalResolved[] = [];
    await bus.subscribe(ToolApprovalResolved, (event) => {
      approvals.push(event);
    });
    await setup.mockInput.typeText('draft text');
    await setup.renderOnce();

    bus.publish(
      new ToolApprovalRequested({
        sessionId: 's1',
        reqId: 'req-1',
        toolName: 'write',
        permission: 'write',
        preview: 'edit notes.txt\n--- a/notes.txt\n+++ b/notes.txt\n@@ -1 +1 @@\n-old line\n+new line',
      }),
    );
    await bus.idle();
    await setup.renderOnce();

    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('Tool approval required');
    expect(frame).toContain('edit notes.txt');
    expect(frame).toContain('-old line');
    expect(frame).toContain('+new line');
    expect(frame).toContain('permission: write');

    await setup.mockInput.pressKey('y');
    await new Promise((resolve) => setTimeout(resolve, 30));
    await bus.idle();

    expect(approvals[0]?.reqId).toBe('req-1');
    expect(approvals[0]?.granted).toBe(true);
    expect(approvals[0]?.remember).toBe(false);
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('draft text');
    expect(frame).toContain('hello bus');
    expect(frame).toContain('Hi there');
    await bus.shutdown();
  });

  test('slash command pages render and close with :q', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListSkills, () => [
      { id: 's1', name: 'demo-skill', status: 'ACTIVE', pattern: 'demo pattern' },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();

    await setup.mockInput.typeText('/skills');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    await setup.renderOnce();

    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('技能列表');
    expect(frame).toContain('demo-skill');

    await setup.mockInput.typeText(':q');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).not.toContain('技能列表');
    await bus.shutdown();
  });

  test('session page selects a session and restores its history', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListSessions, () => [
      {
        sessionId: 'sess-a',
        createdAt: '2026-01-01',
        firstMessage: 'old question',
        messageCount: 2,
      },
    ]);
    bus.provide(ReplaceSessionEvents, () => ({ replaced: 2 }));
    bus.provide(LoadSession, () => ({
      sessionId: 'sess-a',
      messages: [
        { role: 'user', content: 'old question' },
        { role: 'assistant', content: 'old answer' },
      ],
      events: [
        { ...messageEvent('user_message', { role: 'user', content: 'old question' }), seq: 1 },
        { ...messageEvent('assistant_message', { role: 'assistant', content: 'old answer' }), seq: 2 },
      ],
      cronHistory: [],
      formatVersion: 3,
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="initial" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();

    await setup.mockInput.typeText('/sessions');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).toContain('sess-a');

    await setup.mockInput.typeText('1');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('old answer');
    expect(frame).toContain('#sess-a');
    expect(tui.getSnapshot().sessionId).toBe('sess-a');
    await bus.shutdown();
  });

  test('animates the process row while thinking', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 'spin-1', userInput: 'think' }));
    bus.publish(new ThinkingUpdated({ sessionId: 's1', turnId: 'spin-1', delta: 'still thinking' }));
    await bus.idle();
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('思考中');
    expect(frame).toMatch(/[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]/);

    bus.publish(new TurnCompleted({ sessionId: 's1', turnId: 'spin-1', content: 'done', messages: [] }));
    await bus.idle();
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('✻ 思考');
    await bus.shutdown();
  });

  test('Ctrl+T and Ctrl+O expand thinking and tool output', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 'exp-1', userInput: 'expand me' }));
    bus.publish(new ThinkingUpdated({ sessionId: 's1', turnId: 'exp-1', delta: 'deep-thought-xyz' }));
    bus.publish(new ToolStarted({ sessionId: 's1', turnId: 'exp-1', toolId: 'tool-1', toolName: 'read', toolInput: {} }));
    bus.publish(new ToolFinished({
      sessionId: 's1',
      turnId: 'exp-1',
      toolId: 'tool-1',
      output: 'file-body-xyz\nline2\nline3\nline4\nline5',
    }));
    bus.publish(new TurnCompleted({ sessionId: 's1', turnId: 'exp-1', content: 'done', messages: [] }));
    await bus.idle();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('思考 · 1 工具');
    expect(frame).toContain('1 工具');
    expect(frame).not.toContain('Thinking (');
    expect(frame).not.toContain('deep-thought-xyz');
    expect(frame).not.toContain('file-body-xyz');

    tui.toggleToolTurn('exp-1');
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    console.log(frame.split('\n').slice(1, 12).map((line, i) => `${String(i + 1).padStart(2)}|${line}`).join('\n'));
    expect(frame).toContain('Thinking (16 chars) · Ctrl+T');
    expect(frame).toContain('└ ⚙ read');
    expect(frame).not.toContain('└ │ ⚙');
    // Large output is collapsed to a line count for readability.
    expect(frame).toContain('5 line(s) · Ctrl+O');

    await setup.mockInput.pressKey('t', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('deep-thought-xyz');
    expect(frame).toContain('Thinking 已展开');

    await setup.mockInput.pressKey('o', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('file-body-xyz');
    await bus.shutdown();
  });

  test('full-width colon :q closes the page without sending a message', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListSkills, () => [
      { id: 's1', name: 'demo-skill', status: 'ACTIVE', pattern: 'demo' },
    ]);
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const turns: UserTurnRequested[] = [];
    await bus.subscribe(UserTurnRequested, (event) => { turns.push(event); });

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    await setup.mockInput.typeText('/skills');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    expect(tui.getSnapshot().page?.kind).toBe('skills');

    // Chinese IME often produces the full-width colon.
    await setup.mockInput.typeText('：q');
    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 60));
    await bus.idle();

    expect(tui.getSnapshot().page).toBeNull();
    expect(turns).toHaveLength(0);
    await bus.shutdown();
  });

  test('renders tool calls at their chronological position', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 'order-1', userInput: 'check' }));
    bus.publish(new TokenEmitted({ sessionId: 's1', turnId: 'order-1', delta: 'first-part ' }));
    bus.publish(new ToolStarted({ sessionId: 's1', turnId: 'order-1', toolId: 't-1', toolName: 'read', toolInput: {} }));
    bus.publish(new ToolFinished({ sessionId: 's1', turnId: 'order-1', toolId: 't-1', output: 'data' }));
    bus.publish(new TokenEmitted({ sessionId: 's1', turnId: 'order-1', delta: 'second-part' }));
    bus.publish(new TurnCompleted({ sessionId: 's1', turnId: 'order-1', content: 'first-part second-part', messages: [] }));
    await bus.idle();
    tui.toggleToolTurn('order-1');

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('first-part');
    expect(frame).toContain('⚙ read');
    expect(frame).toContain('second-part');
    expect(frame.indexOf('first-part')).toBeLessThan(frame.indexOf('⚙ read'));
    expect(frame.indexOf('⚙ read')).toBeLessThan(frame.indexOf('second-part'));
    await bus.shutdown();
  });

  test('wraps a long single-line command preview so the whole command is visible', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const command =
      'bash: for i in $(seq 1 10); do echo "step $i"; sleep 1; done; echo "done" (cwd: (default), timeout: 15s)';
    bus.publish(
      new ToolApprovalRequested({
        reqId: 'wrap-approval',
        toolName: 'bash',
        permission: 'shell',
        preview: command,
      }),
    );
    await bus.idle();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('tool approval required'.replace('tool', 'Tool'));
    expect(frame).toContain('bash: for i in $(seq 1 10)');
    expect(frame).toContain('timeout: 15s');
    expect(frame).not.toContain('…');
    await bus.shutdown();
  });

  test('long approval previews are paged instead of overflowing', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const preview = Array.from({ length: 60 }, (_, index) => `- line ${index + 1}`).join('\n');
    bus.publish(
      new ToolApprovalRequested({
        reqId: 'long-approval',
        toolName: 'write',
        permission: 'write',
        preview,
      }),
    );
    await bus.idle();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('Tool approval required');
    expect(frame).toContain('60 line(s)');
    expect(frame).toContain('行 1-18/60');
    expect(frame).toContain('line 1');
    expect(frame).not.toContain('line 60');
    expect(frame).toContain('y approve');

    setup.mockInput.pressArrow('down');
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('行 2-19/60');

    setup.mockInput.pressKey('END');
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('行 43-60/60');
    expect(frame).toContain('line 60');
    await bus.shutdown();
  });

  test('page content supports scrolling (wheel event + scroll range)', async () => {
    const bus = new AsyncEventBus();
    bus.provide(
      ListSessions,
      () =>
        Array.from({ length: 40 }, (_, index) => ({
          sessionId: `sess-${String(index + 1).padStart(2, '0')}`,
          createdAt: '2026-01-01T00:00:00Z',
          updatedAt: '2026-01-01T00:00:00Z',
          firstMessage: `message ${index + 1}`,
          messageCount: 1,
        })),
    );
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('sessions');

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30, useMouse: true },
    );
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('sess-01');
    expect(frame).not.toContain('sess-40');

    const collectScrollBoxes = (node: any, out: any[] = []): any[] => {
      if (node?.constructor?.name?.includes('ScrollBox')) out.push(node);
      for (const child of node?.getChildren?.() ?? []) collectScrollBoxes(child, out);
      return out;
    };
    const boxes = collectScrollBoxes(setup.renderer.root);
    expect(boxes).toHaveLength(1);
    const box = boxes[0]!;
    expect(box.scrollHeight).toBeGreaterThan(box.viewport.height);

    // The wheel event must be delivered to the page scrollbox.
    const before = box.scrollTop as number;
    await setup.mockMouse.scroll(40, 15, 'down');
    await new Promise((resolve) => setTimeout(resolve, 20));
    await setup.renderOnce();
    expect(box.scrollTop).toBeGreaterThan(before);

    // The scroll range must reach the end of the page.
    box.scrollTo(100_000);
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('sess-40');
    await bus.shutdown();
  });

  test('renders user/assistant/tool bubbles', async () => {
    const { bus, tui, setup } = await mountedTui();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 'bub-1', userInput: 'check file' }));
    bus.publish(new ToolStarted({ sessionId: 's1', turnId: 'bub-1', toolId: 't-1', toolName: 'read', toolInput: {} }));
    bus.publish(new ToolFinished({ sessionId: 's1', turnId: 'bub-1', toolId: 't-1', output: 'body' }));
    bus.publish(new TurnCompleted({ sessionId: 's1', turnId: 'bub-1', content: 'done', messages: [] }));
    await bus.idle();
    tui.toggleToolTurn('bub-1');
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('◆ Alex');
    expect(frame).toContain('❯ check file');
    expect(frame).toContain('⚙ read');
    await bus.shutdown();
  });

  test('settings page switches toggles by click', async () => {
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
      sessionTitleGeneration: false,
      autoReflect: true,
      reflectionMinTools: 3,
      reflectionMinWriteTools: 2,
    };
    bus.provide(GetSettings, () => ({ ...current }));
    bus.provide(UpdateSettings, (req) => {
      Object.assign(current, req.patch);
      return { ...current };
    });
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('settings');
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 32 },
    );
    await setup.renderOnce();
    let lines = plain(setup.captureCharFrame()).split('\n');
    let row = lines.findIndex((line) => line.includes('thinking') && line.includes('● ON'));
    expect(row).toBeGreaterThanOrEqual(0);
    await setup.mockMouse.click(lines[row]!.indexOf('● ON') + 1, row);
    await new Promise((resolve) => setTimeout(resolve, 60));
    await bus.idle();
    await setup.renderOnce();
    expect(current.thinkingEnabled).toBe(false);
    lines = plain(setup.captureCharFrame()).split('\n');
    row = lines.findIndex((line) => line.includes('thinking') && line.includes('○ OFF'));
    expect(row).toBeGreaterThanOrEqual(0);

    row = lines.findIndex((line) => line.includes('session_title') && line.includes('○ OFF'));
    expect(row).toBeGreaterThanOrEqual(0);
    await setup.mockMouse.click(lines[row]!.indexOf('○ OFF') + 1, row);
    await new Promise((resolve) => setTimeout(resolve, 60));
    await bus.idle();
    expect(current.sessionTitleGeneration).toBe(true);

    row = lines.findIndex((line) => line.includes('effort') && line.includes('‹ high ›'));
    expect(row).toBeGreaterThanOrEqual(0);
    await setup.mockMouse.click(lines[row]!.indexOf('high'), row);
    await new Promise((resolve) => setTimeout(resolve, 60));
    await bus.idle();
    expect(current.effort).toBe('max');
    await bus.shutdown();
  });

  test('settings page edits a field directly', async () => {
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
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('settings');

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('❯ provider');
    expect(frame).toContain('deepseek');
    expect(frame).toContain('↑/↓ select · Enter 切换/编辑');

    await setup.mockInput.pressArrow('down');
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('❯ model');

    await setup.mockInput.pressEnter();
    await setup.renderOnce();
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.mockInput.typeText('new-model');
    await setup.renderOnce();
    await setup.mockInput.pressEnter();
    await setup.renderOnce();
    await bus.idle();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(current.model).toBe('new-model');
    expect(frame).toContain('new-model');
    await bus.shutdown();
  });

  test('separates Markdown paragraphs with a blank line', async () => {
    const setup = await testRender(<MarkdownText text={'first\n\nsecond'} width={40} />, {
      width: 40,
      height: 10,
    });
    await setup.renderOnce();
    const lines = setup.captureCharFrame().split('\n').map((line) => line.trimEnd());
    expect(lines[0]?.trim()).toBe('first');
    expect(lines[1]?.trim()).toBe('');
    expect(lines[2]?.trim()).toBe('second');
  });

  test('keeps Markdown headings adjacent to their content', async () => {
    const setup = await testRender(<MarkdownText text={'# Heading\n\nbody'} width={40} />, {
      width: 40,
      height: 10,
    });
    await setup.renderOnce();
    const lines = setup.captureCharFrame().split('\n').map((line) => line.trimEnd());
    expect(lines[0]?.trim()).toBe('Heading');
    expect(lines[1]?.trim()).toBe('body');
  });

  test('renders assistant Markdown as styled blocks', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    bus.publish(new TurnStarted({ sessionId: 's1', turnId: 'md-1', userInput: 'format this' }));
    bus.publish(
      new TurnCompleted({
        sessionId: 's1',
        turnId: 'md-1',
        content: '# Title\n\nSome **bold** text\n\n- item one\n\n```ts\nconst x = 1;\n```',
        messages: [],
      }),
    );
    await bus.idle();
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('Title');
    expect(frame).toContain('bold');
    expect(frame).toContain('item one');
    expect(frame).toContain('const x = 1;');
    await bus.shutdown();
  });

  test('typing / opens a scrollable command palette and Enter runs the selection', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    await setup.mockInput.typeText('/');
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('Commands (');
    expect(frame).toContain('/help');
    expect(frame).toContain('/skills');
    expect(frame).toContain('/reflect');
    expect(frame).toContain('❯ /help');

    setup.mockInput.pressArrow('down');
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('❯ /skills');

    // Selecting past the visible window scrolls the list.
    for (let index = 0; index < 50; index += 1) {
      setup.mockInput.pressArrow('down');
    }
    await new Promise((resolve) => setTimeout(resolve, 30));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('❯ /quit');
    expect(frame).not.toContain('显示命令帮助');

    await setup.mockInput.typeText('s');
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('/skills');
    expect(frame).toContain('已学习技能列表');
    expect(frame).not.toContain('MCP 服务器状态');

    await setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    expect(tui.getSnapshot().page?.kind).toBe('skills');
    await bus.shutdown();
  });

  test('command palette can find non-default commands such as /dispose', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    await setup.mockInput.typeText('/dis');
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('/dispose');
    await bus.shutdown();
  });

  test('Escape dismisses the command palette', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    await setup.mockInput.typeText('/');
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).toContain('Commands');

    setup.mockInput.pressEscape();
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).not.toContain('Commands');
    expect(tui.getSnapshot().page).toBeNull();
    await bus.shutdown();
  });

  test('sidebar starts collapsed and Ctrl+S toggles it', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('◆ ALEX');
    expect(frame).toContain('● ready');
    expect(frame).not.toContain('event-driven agent');
    expect(frame).toContain('Type a message');
    expect(frame).toContain('effort high');
    expect(frame).toContain('ctx -');
    expect(frame).not.toContain('STATUS');
    expect(frame).toContain('collapsed');

    setup.mockInput.pressKey('s', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('STATUS');
    expect(frame).toContain('侧栏已展开');
    expect(frame).toContain('╭');

    setup.mockInput.pressKey('s', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).not.toContain('STATUS');
    expect(frame).toContain('侧栏已收起');
    expect(frame).toContain('╭');
    await bus.shutdown();
  });

  test('sidebar tabs lazily expand the file tree and show git status', async () => {
    const bus = new AsyncEventBus();
    const children: Record<string, Array<{ path: string; name: string; depth: number; type: 'file' | 'dir' }>> = {
      '': [{ path: 'src', name: 'src', depth: 0, type: 'dir' }],
      src: [
        { path: 'src/app.ts', name: 'app.ts', depth: 0, type: 'file' },
        { path: 'src/lib', name: 'lib', depth: 0, type: 'dir' },
      ],
      'src/lib': [{ path: 'src/lib/x.ts', name: 'x.ts', depth: 0, type: 'file' }],
    };
    let calls = 0;
    bus.provide(ListDirectory, (req) => {
      calls += 1;
      return {
        root: '/repo',
        path: req.path,
        entries: children[req.path] ?? [],
        isGitRepo: req.path === '',
        gitStatus: req.path === '' ? { 'src/app.ts': 'M' } : {},
      };
    });
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    setup.mockInput.pressKey('s', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('STATUS');
    expect(frame).toContain('SESSION');
    expect(frame).toContain('RUNTIME');
    expect(frame).toContain('tools');
    expect(frame).toContain('skills');
    expect(frame).toContain('KEYS');

    setup.mockInput.pressKey('F2');
    await new Promise((resolve) => setTimeout(resolve, 60));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('FILES');
    expect(frame).toContain('/repo');
    expect(frame).toContain('git ✓ 1 changed');
    expect(frame).toContain('▸ src');
    expect(frame).not.toContain('app.ts');

    // Expand the selected src directory.
    setup.mockInput.pressArrow('right');
    await new Promise((resolve) => setTimeout(resolve, 60));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('▾ src');
    expect(frame).toContain('app.ts');
    expect(frame).toContain('▸ lib');

    // Select lib and expand it.
    setup.mockInput.pressArrow('down');
    setup.mockInput.pressArrow('down');
    await new Promise((resolve) => setTimeout(resolve, 30));
    setup.mockInput.pressArrow('right');
    await new Promise((resolve) => setTimeout(resolve, 60));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('▾ lib');
    expect(frame).toContain('x.ts');

    // F5 manually refreshes the file tree (no 5s polling).
    const callsBeforeRefresh = calls;
    setup.mockInput.pressKey('F5');
    await new Promise((resolve) => setTimeout(resolve, 80));
    await setup.renderOnce();
    expect(calls).toBeGreaterThan(callsBeforeRefresh);
    expect(plain(setup.captureCharFrame())).toContain('文件树已刷新');

    setup.mockInput.pressKey('F1');
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).toContain('STATUS');
    await bus.shutdown();
  });

  test('sidebar tabs can be switched with the mouse', async () => {
    const bus = new AsyncEventBus();
    bus.provide(ListDirectory, (req) => ({
      root: '/repo',
      path: req.path,
      entries: req.path === '' ? [{ path: 'src', name: 'src', depth: 0, type: 'dir' as const }] : [],
      isGitRepo: req.path === '',
      gitStatus: {},
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30, useMouse: true },
    );
    await setup.renderOnce();
    setup.mockInput.pressKey('s', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).toContain('SESSION');

    // FILES tab sits at x≈82, y=1 (sidebar starts at column 70).
    await setup.mockMouse.click(82, 1);
    await new Promise((resolve) => setTimeout(resolve, 80));
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('ROOT');
    expect(frame).toContain('/repo');
    expect(frame).not.toContain('SESSION');

    await setup.mockMouse.click(74, 1);
    await new Promise((resolve) => setTimeout(resolve, 60));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('SESSION');
    expect(frame).toContain('RUNTIME');
    await bus.shutdown();
  });

  test('user turns render image and file attachment labels', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    bus.publish(
      new TurnStarted({
        sessionId: 's1',
        turnId: 't1',
        userInput: 'look at this',
        attachments: [
          { kind: 'image', name: 'photo.png', size: 2048 },
          { kind: 'file', name: 'notes.md', size: 120, text: 'hello' },
        ],
      }),
    );
    bus.publish(
      new TurnCompleted({
        sessionId: 's1',
        turnId: 't1',
        content: 'ok',
      }),
    );
    await bus.idle();
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('[image] photo.png');
    expect(frame).toContain('[file] notes.md');
    await bus.shutdown();
  });

  test('pasting a file path attaches it without a command', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-paste-'));
    const path = join(dir, 'note.md');
    await writeFile(path, '# hello');
    const previousAttachmentDir = process.env.ALEX_ATTACHMENT_DIR;
    process.env.ALEX_ATTACHMENT_DIR = join(dir, 'attachments');
    try {
      const bus = new AsyncEventBus();
      const tui = new TuiModule();
      await tui.start(bus);
      await bus.start();
      const setup = await testRender(
        <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
        { width: 100, height: 30 },
      );
      await setup.renderOnce();
      await setup.mockInput.pasteBracketedText(path);
      await new Promise((resolve) => setTimeout(resolve, 80));
      await setup.renderOnce();
      const frame = plain(setup.captureCharFrame());
      expect(frame).toContain('📎');
      expect(frame).toContain('note.md');
      await bus.shutdown();
    } finally {
      if (previousAttachmentDir === undefined) delete process.env.ALEX_ATTACHMENT_DIR;
      else process.env.ALEX_ATTACHMENT_DIR = previousAttachmentDir;
      await rm(dir, { recursive: true, force: true });
    }
  });

  test('status bar shows the model and provider context percentage', async () => {
    const bus = new AsyncEventBus();
    const tui = new TuiModule({ model: 'deepseek-chat', contextWindowTokens: 128_000 });
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 110, height: 30 },
    );
    await setup.renderOnce();
    expect(plain(setup.captureCharFrame())).toContain('◆ deepseek-chat');

    bus.publish(
      new TokenUsageReported({
        sessionId: 's1',
        turnId: 't1',
        promptTokens: 12_800,
        completionTokens: 10,
        totalTokens: 12_810,
        cacheHitTokens: 8_000,
        cacheMissTokens: 4_800,
      }),
    );
    await bus.idle();
    await setup.renderOnce();
    const frame = plain(setup.captureCharFrame());
    expect(frame).toContain('ctx 10%');
    expect(frame).toContain('cache 62.5%');
    await bus.shutdown();
  });

  test('compaction notices render in chat and /compact reports the result', async () => {
    const bus = new AsyncEventBus();
    bus.provide(CompactContext, () => ({
      id: 'c1',
      reason: 'manual' as const,
      compactedMessages: 8,
      keptMessages: 3,
      freedTokens: 1_200,
      summary: 'SUMMARY',
      thresholdTokens: 800,
      skipped: false,
    }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();

    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s1" toolCount={17} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();

    bus.publish(new CompactionStarted({ sessionId: 's1', id: 'c1', reason: 'manual', thresholdTokens: 800 }));
    bus.publish(
      new ContextCompacted({
        sessionId: 's1',
        result: {
          id: 'c1',
          reason: 'manual',
          compactedMessages: 8,
          keptMessages: 3,
          freedTokens: 1_200,
          summary: 'SUMMARY',
          thresholdTokens: 800,
          skipped: false,
        },
      }),
    );
    await bus.idle();
    await setup.renderOnce();
    let frame = plain(setup.captureCharFrame());
    expect(frame).toContain('上下文已压缩');
    expect(frame).toContain('8 条消息');
    expect(frame).toContain('SUMMARY');

    setup.mockInput.typeText('/compact');
    setup.mockInput.pressEnter();
    await new Promise((resolve) => setTimeout(resolve, 60));
    await setup.renderOnce();
    frame = plain(setup.captureCharFrame());
    expect(frame).toContain('已压缩 8 条');
    await bus.shutdown();
  });

  test('clicking the MCP switch toggles a server', async () => {
    const bus = new AsyncEventBus();
    let enabled = true;
    bus.provide(GetMCPStatus, () => ({
      statusMessage: enabled ? 'MCP: 1 server(s) connected' : 'MCP: all servers disabled',
      servers: [
        {
          name: 'demo-server',
          status: enabled ? 'CONNECTED' : 'DISABLED',
          enabled,
          transport: 'stdio',
          tool_count: enabled ? 1 : 0,
          tools: [],
        },
      ],
    }));
    bus.provide(SetMCPServerEnabled, (req) => {
      enabled = req.enabled;
      return { ok: true, enabled, message: `${req.name} 已${enabled ? '启用' : '停用'}` };
    });
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('mcp');
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s-mcp" toolCount={0} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    const lines = plain(setup.captureCharFrame()).split('\n');
    const row = lines.findIndex((line) => line.includes('● ON'));
    expect(row).toBeGreaterThanOrEqual(0);
    const column = lines[row]!.indexOf('● ON');
    await setup.mockMouse.click(column + 2, row);
    await new Promise((resolve) => setTimeout(resolve, 60));
    await bus.idle();
    await setup.renderOnce();
    expect(enabled).toBe(false);
    expect(plain(setup.captureCharFrame())).toContain('○ OFF');
    await bus.shutdown();
  });

  test('clicking [解散] twice disbands a team', async () => {
    const bus = new AsyncEventBus();
    let disposed = false;
    bus.provide(ListAgents, () => [
      {
        agentId: 'w1',
        parentAgentId: 'main',
        teamId: 'team-a',
        sessionId: 's1',
        role: 'worker',
        status: 'idle',
      },
    ]);
    bus.provide(ListAgentTasks, () => [
      {
        taskId: 't1',
        agentId: 'w1',
        sessionId: 's1',
        teamId: 'team-a',
        prompt: 'work',
        status: 'completed',
        result: 'ok',
        error: '',
        startedAt: 1,
        finishedAt: 2,
      },
    ]);
    bus.provide(DisposeTeam, (req) => {
      disposed = true;
      return {
        ok: true,
        teamId: req.teamId,
        disposedAgents: 1,
        cancelledTasks: 0,
        removedTasks: 1,
        message: `已解散 ${req.teamId}`,
      };
    });
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.showPage('teams');
    const setup = await testRender(
      <TuiApp tui={tui} sessionId="s-team" toolCount={0} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await setup.renderOnce();
    let lines = plain(setup.captureCharFrame()).split('\n');
    let row = lines.findIndex((line) => line.includes('[ 解散 ]'));
    expect(row).toBeGreaterThanOrEqual(0);
    await setup.mockMouse.click(lines[row]!.indexOf('[ 解散 ]') + 2, row);
    await new Promise((resolve) => setTimeout(resolve, 50));
    await setup.renderOnce();
    expect(disposed).toBe(false);
    lines = plain(setup.captureCharFrame()).split('\n');
    row = lines.findIndex((line) => line.includes('[ 确认解散 ]'));
    expect(row).toBeGreaterThanOrEqual(0);
    await setup.mockMouse.click(lines[row]!.indexOf('[ 确认解散 ]') + 2, row);
    await new Promise((resolve) => setTimeout(resolve, 80));
    await bus.idle();
    await setup.renderOnce();
    expect(disposed).toBe(true);
    await bus.shutdown();
  });

  test('sidebar shows live status on wide terminals and hides on narrow ones', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => [
      { name: 'a', description: '', jsonSchema: {} },
      { name: 'b', description: '', jsonSchema: {} },
    ]);
    bus.provide(ListSkills, () => []);
    bus.provide(ListCronJobs, () => []);
    bus.provide(GetMCPStatus, () => ({ servers: [], statusMessage: 'no mcp' }));
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    await tui.refreshSidebar();

    const wide = await testRender(
      <TuiApp tui={tui} sessionId="s-sidebar" toolCount={2} onQuit={() => {}} />,
      { width: 100, height: 30 },
    );
    await wide.renderOnce();
    wide.mockInput.pressKey('s', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 40));
    await wide.renderOnce();
    const wideFrame = plain(wide.captureCharFrame());
    expect(wideFrame).toContain('STATUS');
    expect(wideFrame).toMatch(/tools\s+2/);
    expect(wideFrame).toContain('feedback');

    const narrow = await testRender(
      <TuiApp tui={tui} sessionId="s-sidebar" toolCount={2} onQuit={() => {}} />,
      { width: 72, height: 24 },
    );
    await narrow.renderOnce();
    expect(plain(narrow.captureCharFrame())).not.toContain('STATUS');
    await bus.shutdown();
  });

  test('Ctrl+G / Ctrl+B submit feedback', async () => {
    const { bus, setup } = await mountedTui();
    const feedback: FeedbackSubmitted[] = [];
    await bus.subscribe(FeedbackSubmitted, (event) => { feedback.push(event); });

    await setup.mockInput.pressKey('g', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 20));
    await setup.mockInput.pressKey('b', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 20));
    await bus.idle();

    expect(feedback).toHaveLength(2);
    expect(feedback[0]?.positive).toBe(true);
    expect(feedback[1]?.positive).toBe(false);
    await bus.shutdown();
  });

  test('quits on Ctrl+C', async () => {
    const { bus, setup, quitCalls } = await mountedTui();
    await setup.mockInput.pressKey('c', { ctrl: true });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(quitCalls.count).toBe(1);
    await bus.shutdown();
  });
});