import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AgentTaskStore } from '../src/agents/store';
import type { AgentTaskInfo } from '../src/kernel/contracts/agents';

const dirs: string[] = [];

afterEach(async () => {
  while (dirs.length > 0) {
    const dir = dirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-task-store-'));
  dirs.push(dir);
  return dir;
}

function task(overrides: Partial<AgentTaskInfo> = {}): AgentTaskInfo {
  return {
    taskId: 'task-1',
    agentId: 'worker',
    sessionId: 's1',
    prompt: 'work',
    status: 'running',
    result: '',
    error: '',
    startedAt: 1,
    finishedAt: null,
    ...overrides,
  };
}

describe('AgentTaskStore', () => {
  test('loads the latest record per task', async () => {
    const dir = await tempDir();
    const store = new AgentTaskStore(join(dir, 'tasks.jsonl'));
    await store.append('started', task());
    await store.append(
      'finished',
      task({ status: 'completed', result: 'done', finishedAt: 2 }),
    );
    const loaded = await store.load();
    expect(loaded).toHaveLength(1);
    expect(loaded[0]?.status).toBe('completed');
    expect(loaded[0]?.result).toBe('done');
  });

  test('marks interrupted running tasks as cancelled on restore', async () => {
    const dir = await tempDir();
    const store = new AgentTaskStore(join(dir, 'tasks.jsonl'));
    await store.append('started', task());
    const loaded = await store.load();
    expect(loaded[0]?.status).toBe('cancelled');
    expect(loaded[0]?.error).toContain('interrupted');
  });

  test('ignores corrupt lines and missing files', async () => {
    const dir = await tempDir();
    const path = join(dir, 'tasks.jsonl');
    const store = new AgentTaskStore(path);
    expect(await new AgentTaskStore(join(dir, 'missing.jsonl')).load()).toEqual([]);
    await store.append('started', task());
    const raw = await import('node:fs/promises').then((fs) => fs.readFile(path, 'utf8'));
    await import('node:fs/promises').then((fs) =>
      fs.appendFile(path, '{bad json\n', 'utf8'),
    );
    expect(raw).toContain('task-1');
    const loaded = await store.load();
    expect(loaded).toHaveLength(1);
  });
});