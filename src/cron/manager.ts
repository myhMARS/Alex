/** Cron scheduler — timers, job lifecycle and durable persistence. */

import { CronStore, defaultCronDir, deriveJobName, type CronJob } from './store';
import { nextCronTime } from './expression';

export interface CronClock {
  now(): number;
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const systemCronClock: CronClock = {
  now: () => Date.now(),
  setTimeout: (callback, delayMs) => setTimeout(callback, delayMs),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export type CronRunner = (
  sessionId: string,
  jobId: string,
  name: string,
  prompt: string,
  streamId: string,
) => Promise<string>;

export interface CronManagerOptions {
  directory?: string;
  clock?: CronClock;
  runner?: CronRunner;
  onEvent?: (job: CronJob, status: string, streamId: string) => void;
}

export interface CronJobInfo {
  id: string;
  name: string;
  status: string;
  prompt: string;
  recurring: boolean;
  durable: boolean;
  cron: string;
  runsDone: number;
  nextRunAt: number | null;
  lastStartedAt: number | null;
  lastFinishedAt: number | null;
  lastResult: string;
  lastError: string;
}

let idCounter = 0;
function newJobId(): string {
  idCounter += 1;
  return `${Date.now().toString(36)}${idCounter.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

export class CronManager {
  readonly store: CronStore;
  private readonly clock: CronClock;
  private readonly onEvent?: (job: CronJob, status: string, streamId: string) => void;
  private readonly jobs = new Map<string, CronJob>();
  private readonly timers = new Map<string, unknown>();
  private readonly running = new Set<Promise<void>>();
  private runner: CronRunner | null;
  private shuttingDown = false;

  constructor(options: CronManagerOptions = {}) {
    this.store = new CronStore({ directory: options.directory ?? defaultCronDir() });
    this.clock = options.clock ?? systemCronClock;
    this.onEvent = options.onEvent;
    this.runner = options.runner ?? null;
  }

  setRunner(runner: CronRunner): void {
    this.runner = runner;
  }

  /** Restore durable jobs and schedule them (one-shots already run are dropped). */
  async restore(sessionId = ''): Promise<void> {
    if (this.shuttingDown) return;
    const restored = await this.store.restoreAll();
    for (const job of restored) {
      if (sessionId) job.sessionId = sessionId;
      if (job.status === 'CANCELLED') continue;
      job.status = 'SCHEDULED';
      job.nextRunAt = nextCronTime(this.clock.now(), job.cron);
      this.jobs.set(job.id, job);
      await this.store.persist(job);
      this.arm(job);
      this.emit(job, 'SCHEDULED');
    }
  }

  async schedule(input: {
    sessionId?: string;
    cron: string;
    prompt: string;
    recurring?: boolean;
    durable?: boolean;
    name?: string;
  }): Promise<string> {
    const cron = (input.cron ?? '').trim();
    const prompt = (input.prompt ?? '').trim();
    if (!cron) throw new Error('Provide cron');
    if (!prompt) throw new Error('Provide prompt');
    const nextRunAt = nextCronTime(this.clock.now(), cron);

    const job: CronJob = {
      id: newJobId(),
      sessionId: input.sessionId ?? '',
      name: (input.name ?? deriveJobName(prompt)).slice(0, 60),
      cron,
      prompt,
      recurring: input.recurring !== false,
      durable: input.durable === true,
      status: 'SCHEDULED',
      runsDone: 0,
      lastStartedAt: null,
      lastFinishedAt: null,
      lastError: '',
      lastResult: '',
      nextRunAt,
    };
    this.jobs.set(job.id, job);
    await this.store.persist(job);
    this.arm(job);
    this.emit(job, 'SCHEDULED');
    return job.id;
  }

  async cancel(jobId: string): Promise<boolean> {
    const job = this.jobs.get(jobId);
    this.clearTimer(jobId);
    if (!job) {
      await this.store.delete(jobId);
      return false;
    }
    job.status = 'CANCELLED';
    job.nextRunAt = null;
    this.jobs.delete(jobId);
    await this.store.delete(jobId);
    this.emit(job, 'CANCELLED');
    return true;
  }

  getJob(jobId: string): CronJob | undefined {
    return this.jobs.get(jobId);
  }

  listJobs(): CronJobInfo[] {
    const jobs = [...this.jobs.values()].map((job) => ({
      id: job.id,
      name: job.name,
      status: job.status,
      prompt: job.prompt,
      recurring: job.recurring,
      durable: job.durable,
      cron: job.cron,
      runsDone: job.runsDone,
      nextRunAt: job.nextRunAt,
      lastStartedAt: job.lastStartedAt,
      lastFinishedAt: job.lastFinishedAt,
      lastResult: job.lastResult,
      lastError: job.lastError,
    }));
    jobs.sort((a, b) => {
      const rank = (status: string) => (status === 'RUNNING' ? 0 : status === 'SCHEDULED' ? 1 : 2);
      return rank(a.status) - rank(b.status) || (a.nextRunAt ?? 0) - (b.nextRunAt ?? 0);
    });
    return jobs;
  }

  async whenIdle(): Promise<void> {
    while (this.running.size > 0) {
      await Promise.all([...this.running]);
    }
  }

  async shutdown(): Promise<void> {
    this.shuttingDown = true;
    for (const jobId of [...this.timers.keys()]) this.clearTimer(jobId);
    await this.whenIdle();
  }

  // ── internals ──────────────────────────────────────────────────────────

  private arm(job: CronJob): void {
    if (this.shuttingDown) return;
    this.clearTimer(job.id);
    const nextRunAt = job.nextRunAt ?? nextCronTime(this.clock.now(), job.cron);
    job.nextRunAt = nextRunAt;
    const delay = Math.max(0, nextRunAt - this.clock.now());
    const handle = this.clock.setTimeout(() => {
      this.timers.delete(job.id);
      void this.fire(job.id);
    }, delay);
    this.timers.set(job.id, handle);
  }

  private clearTimer(jobId: string): void {
    const handle = this.timers.get(jobId);
    if (handle !== undefined) {
      this.clock.clearTimeout(handle);
      this.timers.delete(jobId);
    }
  }

  private async fire(jobId: string): Promise<void> {
    const job = this.jobs.get(jobId);
    if (!job || job.status === 'RUNNING' || job.status === 'CANCELLED' || this.shuttingDown) {
      return;
    }
    const task = this.executeJob(job);
    this.running.add(task);
    try {
      await task;
    } finally {
      this.running.delete(task);
    }
  }

  private async executeJob(job: CronJob): Promise<void> {
    const runner = this.runner;
    const streamId = `cron:${job.id}:${job.runsDone + 1}`;
    job.status = 'RUNNING';
    job.lastStartedAt = this.clock.now();
    await this.store.persist(job);
    this.emit(job, 'RUNNING');

    let runStatus = 'SUCCESS';
    let result = '';
    try {
      if (!runner) throw new Error('cron runner is not configured');
      result = await runner(job.sessionId, job.id, job.name, job.prompt, streamId);
      job.lastResult = result;
      job.lastError = '';
      if (result.trimStart().startsWith('Error:')) {
        runStatus = 'FAILED';
        job.lastError = result;
      }
    } catch (error) {
      runStatus = 'FAILED';
      job.lastResult = '';
      job.lastError = error instanceof Error ? error.message : String(error);
    }

    if (job.status === 'CANCELLED') return;

    job.runsDone += 1;
    job.lastFinishedAt = this.clock.now();
    const done = !job.recurring;
    if (done) {
      job.status = runStatus;
      job.nextRunAt = null;
      this.jobs.delete(job.id);
      await this.store.delete(job.id);
    } else {
      job.status = 'SCHEDULED';
      job.nextRunAt = nextCronTime(this.clock.now(), job.cron);
      await this.store.persist(job);
      this.arm(job);
    }
    this.emit(job, runStatus, streamId);
  }

  private emit(job: CronJob, status: string, streamId = ''): void {
    this.onEvent?.(job, status, streamId);
  }
}