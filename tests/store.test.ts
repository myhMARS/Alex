import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { StoreModule } from '../src/modules/store/module';
import { TurnCompleted } from '../src/kernel/contracts/chat';
import { ListSessions, LoadSession } from '../src/kernel/contracts/session';
import { assistantMessage, userMessage, type MessageDTO } from '../src/kernel/dto/message';

const tempDirs: string[] = [];

async function makeStore(): Promise<{ bus: AsyncEventBus; store: StoreModule; dir: string }> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-store-'));
  tempDirs.push(dir);
  const bus = new AsyncEventBus();
  const store = new StoreModule({ sessionsDir: dir });
  await store.start(bus);
  await bus.start();
  return { bus, store, dir };
}

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

describe('StoreModule', () => {
  test('persists TurnCompleted and lists sessions', async () => {
    const { bus, dir } = await makeStore();
    const messages: MessageDTO[] = [userMessage('hello there'), assistantMessage('hi')];
    bus.publish(new TurnCompleted({ sessionId: 's1', messages }));
    await bus.idle();

    const sessions = await bus.request(new ListSessions());
    expect(sessions).toHaveLength(1);
    expect(sessions[0]?.sessionId).toBe('s1');
    expect(sessions[0]?.firstMessage).toBe('hello there');
    expect(sessions[0]?.messageCount).toBe(2);

    const raw = JSON.parse(await readFile(join(dir, 's1.json'), 'utf8')) as Record<string, unknown>;
    expect(raw.session_id).toBe('s1');
    expect(raw.first_message).toBe('hello there');
    expect(Array.isArray(raw.messages)).toBe(true);
    expect(Array.isArray(raw.cron_history)).toBe(true);

    await bus.shutdown();
  });

  test('loads a session bundle and keeps created_at across saves', async () => {
    const { bus, dir } = await makeStore();
    bus.publish(new TurnCompleted({ sessionId: 's2', messages: [userMessage('first')] }));
    await bus.idle();
    const first = JSON.parse(await readFile(join(dir, 's2.json'), 'utf8')) as {
      created_at: string;
    };

    bus.publish(
      new TurnCompleted({
        sessionId: 's2',
        messages: [userMessage('first'), assistantMessage('second')],
      }),
    );
    await bus.idle();
    const second = JSON.parse(await readFile(join(dir, 's2.json'), 'utf8')) as {
      created_at: string;
    };
    expect(second.created_at).toBe(first.created_at);

    const bundle = await bus.request(new LoadSession({ targetSessionId: 's2' }));
    expect(bundle?.sessionId).toBe('s2');
    expect(bundle?.messages).toHaveLength(2);
    expect(bundle?.messages[1]?.content).toBe('second');

    await bus.shutdown();
  });

  test('returns null / empty list for unknown sessions', async () => {
    const { bus } = await makeStore();
    expect(await bus.request(new ListSessions())).toEqual([]);
    expect(await bus.request(new LoadSession({ targetSessionId: 'missing' }))).toBeNull();
    await bus.shutdown();
  });
});