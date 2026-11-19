import { afterEach, describe, expect, test } from 'bun:test';
import { appendFile, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { StoreModule } from '../src/modules/store/module';
import { TurnCompleted } from '../src/kernel/contracts/chat';
import {
  CheckpointSession,
  ListSessions,
  LoadSession,
  RepairSession,
} from '../src/kernel/contracts/session';
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

    const lines = (await readFile(join(dir, 's1.jsonl'), 'utf8'))
      .trim()
      .split('\n');
    const raw = JSON.parse(lines[0]!) as Record<string, unknown>;
    const events = lines.slice(1).map((line) => JSON.parse(line));
    expect(raw.kind).toBe('session');
    expect(raw.session_id).toBe('s1');
    expect(raw.first_message).toBe('hello there');
    expect(raw.format_version).toBe(3);
    expect(raw.messages).toBeUndefined();
    expect(Array.isArray(raw.cron_history)).toBe(true);
    expect(events).toHaveLength(2);
    expect(events[0].type).toBe('user_message');

    await bus.shutdown();
  });

  test('loads a session bundle and keeps created_at across saves', async () => {
    const { bus, dir } = await makeStore();
    bus.publish(new TurnCompleted({ sessionId: 's2', messages: [userMessage('first')] }));
    await bus.idle();
    const first = JSON.parse(
      (await readFile(join(dir, 's2.jsonl'), 'utf8')).split('\n')[0]!,
    ) as { created_at: string };

    bus.publish(
      new TurnCompleted({
        sessionId: 's2',
        messages: [userMessage('first'), assistantMessage('second')],
      }),
    );
    await bus.idle();
    const second = JSON.parse(
      (await readFile(join(dir, 's2.jsonl'), 'utf8')).split('\n')[0]!,
    ) as { created_at: string };
    expect(second.created_at).toBe(first.created_at);

    const checkpoint = await bus.request(
      new CheckpointSession({ targetSessionId: 's2' }),
    );
    expect(checkpoint.checkpointed).toBe(true);
    expect(checkpoint.bytes).toBeGreaterThan(0);

    const bundle = await bus.request(new LoadSession({ targetSessionId: 's2' }));
    expect(bundle?.sessionId).toBe('s2');
    expect(bundle?.messages).toHaveLength(2);
    expect(bundle?.messages[1]?.content).toBe('second');

    await bus.shutdown();
  });

  test('repairs a corrupted JSONL tail by truncating bad events', async () => {
    const { bus, dir } = await makeStore();
    bus.publish(
      new TurnCompleted({
        sessionId: 's3',
        messages: [userMessage('hello'), assistantMessage('hi')],
      }),
    );
    await bus.idle();
    await appendFile(join(dir, 's3.jsonl'), '{"seq":3,"type":"broken"\n', 'utf8');
    const result = await bus.request(new RepairSession({ targetSessionId: 's3' }));
    expect(result?.repaired).toBe(true);
    expect(result?.validEvents).toBe(2);
    expect(result?.droppedEvents).toBe(1);
    const bundle = await bus.request(new LoadSession({ targetSessionId: 's3' }));
    expect(bundle?.messages).toHaveLength(2);
    await bus.shutdown();
  });

  test('returns null / empty list for unknown sessions', async () => {
    const { bus } = await makeStore();
    expect(await bus.request(new ListSessions())).toEqual([]);
    expect(await bus.request(new LoadSession({ targetSessionId: 'missing' }))).toBeNull();
    await bus.shutdown();
  });
});