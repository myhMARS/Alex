import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { CronParseError, nextCronTime, parseCron, validateCron } from '../src/cron/expression';
import { CronStore, cronJobFromFile, deriveJobName } from '../src/cron/store';
import { CronManager, type CronClock } from '../src/cron/manager';
import { CronModule } from '../src/cron/module';
import { AsyncEventBus } from '../src/kernel/bus';
import { MemoryModule } from '../src/modules/memory/module';
import { AgentModule } from '../src/modules/agent/module';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { ScheduleCron, CancelCron, ListCronJobs, CronJobEvent } from '../src/kernel/contracts/cron';
import { ExecuteTool, GetToolCatalog } from '../src/kernel/contracts/tools';
import { GetContext } from '../src/kernel/contracts/memory';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(prefix = 'alex-ts-cron-'): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), prefix));
  tempDirs.push(dir);
  return dir;
}

class FakeClock implements CronClock {
  time: number;
  private seq = 0;
  private readonly timers = new Map<number, { at: number; callback: () => void }>();

  constructor(time: number) {
    this.time = time;
  }

  now(): number {
    return this.time;
  }

  setTimeout(callback: () => void, delayMs: number): unknown {
    this.seq += 1;
    this.timers.set(this.seq, { at: this.time + delayMs, callback });
    return this.seq;
  }

  clearTimeout(handle: unknown): void {
    this.timers.delete(Number(handle));
  }

  async advance(ms: number): Promise<void> {
    this.time += ms;
    for (;;) {
      const due = [...this.timers.entries()]
        .filter(([, timer]) => timer.at <= this.time)
        .sort((a, b) => a[1].at - b[1].at);
      if (due.length === 0) break;
      const [id, timer] = due[0]!;
      this.timers.delete(id);
      timer.callback();
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  }
}

function localTime(year: number, month: number, day: number, hour = 0, minute = 0): number {
  return new Date(year, month - 1, day, hour, minute, 0, 0).getTime();
}

describe('cron expression', () => {
  test('parses fields, ranges, steps and lists', () => {
    const spec = parseCron('*/15 9-17 1,15 * 1-5');
    expect([...spec.minutes]).toEqual([0, 15, 30, 45]);
    expect([...spec.hours]).toEqual([9, 10, 11, 12, 13, 14, 15, 16, 17]);
    expect([...spec.days]).toEqual([1, 15]);
    expect(spec.months.size).toBe(12);
    expect([...spec.weekdays]).toEqual([1, 2, 3, 4, 5]);
  });

  test('rejects malformed expressions', () => {
    expect(() => parseCron('* * * *')).toThrow(CronParseError);
    expect(() => parseCron('60 * * * *')).toThrow(CronParseError);
    expect(() => parseCron('*/0 * * * *')).toThrow(CronParseError);
    expect(() => validateCron('bad')).toThrow(CronParseError);
  });

  test('computes the next local fire time', () => {
    const base = localTime(2026, 1, 1, 12, 3);
    expect(nextCronTime(base, '*/5 * * * *')).toBe(localTime(2026, 1, 1, 12, 5));

    const morning = localTime(2026, 1, 1, 10, 0);
    expect(nextCronTime(morning, '0 9 * * *')).toBe(localTime(2026, 1, 2, 9, 0));

    expect(nextCronTime(localTime(2026, 1, 15, 0, 0), '0 0 1 * *')).toBe(
      localTime(2026, 2, 1, 0, 0),
    );

    // 2026-01-04 is a Sunday → next weekday 09:00 is Monday.
    expect(nextCronTime(localTime(2026, 1, 4, 12, 0), '0 9 * * 1-5')).toBe(
      localTime(2026, 1, 5, 9, 0),
    );
  });
});

describe('CronStore', () => {
  test('persists durable jobs with Python-compatible snake_case fields', async () => {
    const dir = await tempDir();
    const store = new CronStore({ directory: dir });
    const job = {
      id: 'job1',
      sessionId: 's1',
      name: 'daily',
      cron: '0 9 * * *',
      prompt: 'summarize news',
      recurring: true,
      durable: true,
      status: 'SCHEDULED',
      runsDone: 0,
      lastStartedAt: null,
      lastFinishedAt: null,
      lastError: '',
      lastResult: '',
      nextRunAt: 123,
    };
    await store.persist(job);
    const raw = JSON.parse(await readFile(join(dir, 'job1.json'), 'utf8')) as Record<
      string,
      unknown
    >;
    expect(raw.session_id).toBe('s1');
    expect(raw.runs_done).toBe(0);
    expect(raw).not.toHaveProperty('sessionId');

    const restored = await store.restoreAll();
    expect(restored).toHaveLength(1);
    expect(restored[0]?.name).toBe('daily');

    await store.delete('job1');
    expect(await readdir(dir)).toEqual([]);
  });

  test('restores only valid jobs and drops finished one-shots', async () => {
    const dir = await tempDir();
    await writeFile(
      join(dir, 'oneshot.json'),
      JSON.stringify({ id: 'oneshot', prompt: 'x', cron: '* * * * *', recurring: false, runs_done: 1 }),
      'utf8',
    );
    await writeFile(
      join(dir, 'running.json'),
      JSON.stringify({ id: 'running', prompt: 'y', cron: '* * * * *', status: 'RUNNING' }),
      'utf8',
    );
    await writeFile(join(dir, 'broken.json'), '{not json', 'utf8');

    const store = new CronStore({ directory: dir });
    const jobs = await store.restoreAll();
    expect(jobs.map((job) => job.id)).toEqual(['running']);
    expect(jobs[0]?.status).toBe('SCHEDULED');
    expect(await readdir(dir)).not.toContain('oneshot.json');
  });

  test('deriveJobName and from-file defaults', () => {
    expect(deriveJobName('check weather\nsecond line')).toBe('check weather');
    const job = cronJobFromFile({ id: 'x', prompt: 'hello', cron: '* * * * *' });
    expect(job.name).toBe('hello');
    expect(job.recurring).toBe(true);
    expect(job.durable).toBe(false);
    expect(job.status).toBe('SCHEDULED');
  });
});

describe('CronManager', () => {
  test('fires recurring jobs on the clock and reschedules', async () => {
    const dir = await tempDir();
    const clock = new FakeClock(localTime(2026, 1, 1, 8, 0));
    const calls: string[] = [];
    const events: string[] = [];
    const manager = new CronManager({
      directory: dir,
      clock,
      runner: async (_sid, jobId, _name, _prompt, streamId) => {
        calls.push(streamId);
        return `result:${jobId}:${calls.length}`;
      },
      onEvent: (job, status) => events.push(`${status}:${job.runsDone}`),
    });

    const jobId = await manager.schedule({ sessionId: 's1', cron: '* * * * *', prompt: 'tick' });
    expect(manager.listJobs()[0]?.status).toBe('SCHEDULED');

    await clock.advance(60_000);
    await manager.whenIdle();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toBe(`cron:${jobId}:1`);
    const job = manager.getJob(jobId);
    expect(job?.runsDone).toBe(1);
    expect(job?.lastResult).toBe(`result:${jobId}:1`);
    expect(events).toEqual(['SCHEDULED:0', 'RUNNING:0', 'SUCCESS:1']);

    await clock.advance(60_000);
    await manager.whenIdle();
    expect(calls).toHaveLength(2);
    expect(manager.getJob(jobId)?.runsDone).toBe(2);

    await manager.shutdown();
  });

  test('one-shot jobs disappear after running', async () => {
    const dir = await tempDir();
    const clock = new FakeClock(localTime(2026, 1, 1, 8, 0));
    const manager = new CronManager({
      directory: dir,
      clock,
      runner: async () => 'done',
    });
    await manager.schedule({ cron: '* * * * *', prompt: 'once', recurring: false, durable: true });
    await clock.advance(60_000);
    await manager.whenIdle();
    expect(manager.listJobs()).toHaveLength(0);
    expect(await readdir(dir)).toEqual([]);
    await manager.shutdown();
  });

  test('cancel clears timer and durable file', async () => {
    const dir = await tempDir();
    const clock = new FakeClock(localTime(2026, 1, 1, 8, 0));
    const manager = new CronManager({
      directory: dir,
      clock,
      runner: async () => 'done',
    });
    const jobId = await manager.schedule({ cron: '* * * * *', prompt: 'x', durable: true });
    expect(await readdir(dir)).toHaveLength(1);
    expect(await manager.cancel(jobId)).toBe(true);
    expect(manager.listJobs()).toHaveLength(0);
    expect(await readdir(dir)).toHaveLength(0);
    await clock.advance(120_000);
    await manager.whenIdle();
    await manager.shutdown();
  });

  test('durable jobs are restored after restart', async () => {
    const dir = await tempDir();
    const clock = new FakeClock(localTime(2026, 1, 1, 8, 0));
    const first = new CronManager({ directory: dir, clock, runner: async () => 'ok' });
    await first.schedule({ cron: '0 9 * * *', prompt: 'restored', durable: true });
    await first.shutdown();

    const second = new CronManager({ directory: dir, clock, runner: async () => 'ok' });
    await second.restore('session-rebound');
    const jobs = second.listJobs();
    expect(jobs).toHaveLength(1);
    expect(jobs[0]?.prompt).toBe('restored');
    expect(jobs[0]?.status).toBe('SCHEDULED');
    expect(second.getJob(jobs[0]!.id)?.sessionId).toBe('session-rebound');
    await second.shutdown();
  });

  test('marks failures and keeps recurring jobs scheduled', async () => {
    const dir = await tempDir();
    const clock = new FakeClock(localTime(2026, 1, 1, 8, 0));
    const events: string[] = [];
    const manager = new CronManager({
      directory: dir,
      clock,
      runner: async () => {
        throw new Error('runner exploded');
      },
      onEvent: (_job, status) => events.push(status),
    });
    const jobId = await manager.schedule({ cron: '* * * * *', prompt: 'x' });
    await clock.advance(60_000);
    await manager.whenIdle();
    const job = manager.getJob(jobId);
    expect(job?.status).toBe('SCHEDULED');
    expect(job?.lastError).toContain('runner exploded');
    expect(events).toContain('FAILED');
    await manager.shutdown();
  });
});

describe('CronModule end-to-end', () => {
  async function setup() {
    const dir = await tempDir();
    const clock = new FakeClock(localTime(2026, 1, 1, 8, 0));
    const bus = new AsyncEventBus();
    const memory = new MemoryModule();
    const tools = new ToolsModule({
      roots: [dir],
      policy: new PermissionPolicy({ allowed: ['read', 'network'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
      approvalTimeoutMs: 500,
      plugins: false,
    });
    const agent = new AgentModule();
    const cron = new CronModule({ directory: join(dir, 'cron'), clock });
    await memory.start(bus);
    await tools.start(bus);
    await agent.start(bus);
    await cron.start(bus);
    await bus.start();
    return { bus, memory, tools, agent, cron, clock, dir };
  }

  test('schedules, fires a turn and records the result', async () => {
    const { bus, agent, cron, clock } = await setup();
    const events: CronJobEvent[] = [];
    await bus.subscribe(CronJobEvent, (event) => { events.push(event); });

    const jobId = await bus.request(
      new ScheduleCron({ sessionId: 's1', cron: '* * * * *', prompt: 'ping cron' }),
    );
    expect(jobId).toBeTruthy();
    expect((await bus.request(new ListCronJobs()))[0]?.status).toBe('SCHEDULED');

    await clock.advance(60_000);
    await agent.whenIdle();
    await cron.whenIdle();
    await bus.idle();

    const statuses = events.map((event) => event.status);
    expect(statuses).toContain('RUNNING');
    expect(statuses).toContain('SUCCESS');
    const jobs = await bus.request(new ListCronJobs());
    expect(jobs[0]?.runsDone).toBe(1);
    expect(jobs[0]?.lastResult).toBe('Echo: ping cron');

    const history = await bus.request(new GetContext({ sessionId: 's1' }));
    expect(history.map((message) => message.role)).toEqual(['user', 'assistant']);
    expect(history[1]?.content).toBe('Echo: ping cron');
    expect(history[0]?.alex_turn_kind).toBe('cron');

    await bus.shutdown();
  }, 20_000);

  test('registers cron tools and lists jobs through them', async () => {
    const { bus } = await setup();
    const catalog = await bus.request(new GetToolCatalog());
    expect(catalog.map((spec) => spec.name)).toEqual(
      expect.arrayContaining(['cron', 'cron_cancel', 'cron_jobs']),
    );

    const scheduled = await bus.request(
      new ExecuteTool({
        name: 'cron',
        args: { cron: '*/5 * * * *', prompt: 'check status', recurring: true },
        sessionId: 's1',
      }),
    );
    expect(scheduled.output).toContain('Scheduled:');

    const listed = await bus.request(
      new ExecuteTool({ name: 'cron_jobs', args: {}, sessionId: 's1' }),
    );
    expect(listed.output).toContain('check status');

    const jobId = (await bus.request(new ListCronJobs()))[0]!.id;
    const cancelled = await bus.request(
      new ExecuteTool({ name: 'cron_cancel', args: { job_id: jobId }, sessionId: 's1' }),
    );
    expect(cancelled.output).toContain('Cancelled:');
    expect(await bus.request(new ListCronJobs())).toHaveLength(0);

    await bus.shutdown();
  }, 20_000);

  test('invalid cron requests are rejected', async () => {
    const { bus } = await setup();
    await expect(
      bus.request(new ScheduleCron({ sessionId: 's1', cron: 'not-a-cron', prompt: 'x' })),
    ).rejects.toThrow();
    expect(await bus.request(new CancelCron({ jobId: 'missing' }))).toBe(false);
    await bus.shutdown();
  }, 20_000);
});