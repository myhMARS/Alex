import { afterEach, describe, expect, test } from 'bun:test';
import { access, mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { StoreModule } from '../src/modules/store/module';
import {
  ExportSession,
  ListSessions,
  LoadSession,
  RemoveSession,
  UpdateSessionMeta,
} from '../src/kernel/contracts/session';
import { TurnCompleted, TurnStarted } from '../src/kernel/contracts/chat';
import { assistantMessage, userMessage } from '../src/kernel/dto/message';
import { TuiModule } from '../src/modules/tui/module';
import {
  RegenerateSessionTitle,
  type SessionMeta,
} from '../src/kernel/contracts/session';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function setup(): Promise<{
  bus: AsyncEventBus;
  sessionsDir: string;
  exportsDir: string;
}> {
  const root = await mkdtemp(join(tmpdir(), 'alex-ts-session-mgmt-'));
  tempDirs.push(root);
  const sessionsDir = join(root, 'sessions');
  const exportsDir = join(root, 'exports');
  const bus = new AsyncEventBus();
  const store = new StoreModule({ sessionsDir, exportsDir });
  await store.start(bus);
  await bus.start();
  return { bus, sessionsDir, exportsDir };
}

describe('session management', () => {
  test('binds sessions to a workspace and rejects cross-workspace resume', async () => {
    const root = await mkdtemp(join(tmpdir(), 'alex-ts-workspace-'));
    tempDirs.push(root);
    const sessionsDir = join(root, 'sessions');
    const workspaceA = join(root, 'project-a');
    const workspaceB = join(root, 'project-b');
    await mkdir(workspaceA, { recursive: true });
    await mkdir(workspaceB, { recursive: true });

    const busA = new AsyncEventBus();
    const storeA = new StoreModule({ sessionsDir, cwd: workspaceA });
    await storeA.start(busA);
    await busA.start();
    busA.publish(
      new TurnCompleted({ sessionId: 'bound', messages: [userMessage('hello')] }),
    );
    await busA.idle();
    const [meta] = await busA.request(new ListSessions());
    expect(meta?.workspace).toBe(workspaceA);
    await busA.shutdown();

    const busB = new AsyncEventBus();
    const storeB = new StoreModule({ sessionsDir, cwd: workspaceB });
    await storeB.start(busB);
    await busB.start();
    await expect(
      busB.request(new LoadSession({ targetSessionId: 'bound' })),
    ).rejects.toThrow('其他工作区');
    await busB.shutdown();

    const busA2 = new AsyncEventBus();
    const storeA2 = new StoreModule({ sessionsDir, cwd: workspaceA });
    await storeA2.start(busA2);
    await busA2.start();
    expect((await busA2.request(new LoadSession({ targetSessionId: 'bound' })))?.sessionId).toBe(
      'bound',
    );
    await busA2.shutdown();
  });

  test('renames, pins, archives and unarchives sessions', async () => {
    const { bus } = await setup();
    bus.publish(
      new TurnCompleted({
        sessionId: 's1',
        messages: [userMessage('hello'), assistantMessage('hi')],
      }),
    );
    bus.publish(
      new TurnCompleted({ sessionId: 's2', messages: [userMessage('other')] }),
    );
    await bus.idle();

    expect(
      await bus.request(
        new UpdateSessionMeta({
          targetSessionId: 's1',
          patch: { title: 'Alpha', pinned: true },
        }),
      ),
    ).toBe(true);
    expect(
      await bus.request(
        new UpdateSessionMeta({ targetSessionId: 's2', patch: { archived: true } }),
      ),
    ).toBe(true);

    const sessions = await bus.request(new ListSessions());
    const alpha = sessions.find((session) => session.sessionId === 's1');
    const other = sessions.find((session) => session.sessionId === 's2');
    expect(alpha?.title).toBe('Alpha');
    expect(alpha?.pinned).toBe(true);
    expect(other?.archived).toBe(true);

    const unarchived = await bus.request(
      new UpdateSessionMeta({ targetSessionId: 's2', patch: { archived: false } }),
    );
    expect(unarchived).toBe(true);
    const after = await bus.request(new ListSessions());
    expect(after.find((session) => session.sessionId === 's2')?.archived).toBe(false);
    await bus.shutdown();
  });

  test('exports Markdown and removes a session', async () => {
    const { bus, sessionsDir, exportsDir } = await setup();
    bus.publish(
      new TurnCompleted({
        sessionId: 's-export',
        messages: [userMessage('export me'), assistantMessage('done')],
      }),
    );
    await bus.idle();
    await bus.request(
      new UpdateSessionMeta({
        targetSessionId: 's-export',
        patch: { title: 'Exported Chat' },
      }),
    );

    const exported = await bus.request(
      new ExportSession({ targetSessionId: 's-export' }),
    );
    expect(exported?.path.startsWith(exportsDir)).toBe(true);
    expect(exported?.markdown).toContain('# Exported Chat');
    expect(exported?.markdown).toContain('## User');
    expect(exported?.markdown).toContain('export me');
    await access(exported!.path);
    expect(await readFile(exported!.path, 'utf8')).toContain('## Assistant');

    expect(await bus.request(new RemoveSession({ targetSessionId: 's-export' }))).toBe(true);
    expect(await bus.request(new LoadSession({ targetSessionId: 's-export' }))).toBeNull();
    expect((await bus.request(new ListSessions())).map((meta) => meta.sessionId)).not.toContain(
      's-export',
    );
    await expect(access(join(sessionsDir, 's-export.jsonl'))).rejects.toThrow();
    await bus.shutdown();
  });
});


describe('session management in the TUI', () => {
  async function tuiSetup(): Promise<{
    bus: AsyncEventBus;
    tui: TuiModule;
    sessions: SessionMeta[];
    actions: string[];
  }> {
    const bus = new AsyncEventBus();
    const now = new Date().toISOString();
    const sessions: SessionMeta[] = [
      {
        sessionId: 's-pinned',
        createdAt: now,
        updatedAt: now,
        firstMessage: 'alpha topic',
        messageCount: 2,
        title: 'Pinned Alpha',
        pinned: true,
      },
      {
        sessionId: 's-archived',
        createdAt: '2025-01-01T00:00:00Z',
        updatedAt: '2025-01-01T00:00:00Z',
        firstMessage: 'old topic',
        messageCount: 1,
        archived: true,
      },
      {
        sessionId: 's-normal',
        createdAt: now,
        updatedAt: now,
        firstMessage: 'normal topic',
        messageCount: 3,
      },
    ];
    const actions: string[] = [];
    bus.provide(ListSessions, () => sessions);
    bus.provide(UpdateSessionMeta, (req) => {
      actions.push(`meta:${req.patch.title ?? req.patch.pinned ?? req.patch.archived}`);
      return true;
    });
    bus.provide(RegenerateSessionTitle, (req) => {
      actions.push(`title:${req.targetSessionId}`);
      return { title: 'Regenerated' };
    });
    bus.provide(ExportSession, (req) => {
      actions.push(`export:${req.targetSessionId}`);
      return { path: '/tmp/export.md', markdown: '# x', messageCount: 1 };
    });
    bus.provide(RemoveSession, (req) => {
      actions.push(`remove:${req.targetSessionId}`);
      return true;
    });
    const tui = new TuiModule();
    await tui.start(bus);
    await bus.start();
    return { bus, tui, sessions, actions };
  }

  test('filters by archived, text and recency', async () => {
    const { bus, tui } = await tuiSetup();
    await tui.showPage('sessions');
    expect(tui.getSnapshot().page?.body).toContain('Pinned Alpha');
    expect(tui.getSnapshot().page?.body).toContain('normal topic');
    expect(tui.getSnapshot().page?.body).not.toContain('s-archived');

    tui.setSessionFilter('archived');
    await tui.showPage('sessions');
    expect(tui.getSnapshot().page?.body).toContain('s-archived');
    expect(tui.getSnapshot().page?.body).not.toContain('Pinned Alpha');

    tui.setSessionFilter('alpha');
    await tui.showPage('sessions');
    expect(tui.getSnapshot().page?.body).toContain('Pinned Alpha');
    expect(tui.getSnapshot().page?.body).not.toContain('s-normal');

    tui.setSessionFilter('today');
    await tui.showPage('sessions');
    expect(tui.getSnapshot().page?.body).toContain('Pinned Alpha');
    expect(tui.getSnapshot().page?.body).not.toContain('s-archived');
    await bus.shutdown();
  });

  test('renames and performs session row actions', async () => {
    const { bus, tui, actions } = await tuiSetup();
    await tui.showPage('sessions');
    expect((await tui.renameSession(1, 'Manual Title')).ok).toBe(true);
    expect(actions).toContain('meta:Manual Title');
    expect((await tui.sessionAction(1, 'pin')).ok).toBe(true);
    expect((await tui.sessionAction(1, 'title')).ok).toBe(true);
    expect((await tui.sessionAction(1, 'export')).ok).toBe(true);
    expect((await tui.sessionAction(1, 'delete')).ok).toBe(true);
    expect(actions).toContain('title:s-pinned');
    expect(actions).toContain('export:s-pinned');
    expect(actions).toContain('remove:s-pinned');
    await bus.shutdown();
  });

});
