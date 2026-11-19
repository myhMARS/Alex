/**
 * CronModule — scheduled prompt-driven turns over the bus.
 *
 * Provides ScheduleCron / CancelCron / ListCronJobs, publishes CronJobEvent for
 * status changes, and fires `CronTurnRequested` when a job is due (then waits
 * for the matching `TurnCompleted`). Also registers the `cron`, `cron_cancel`
 * and `cron_jobs` tools.
 */

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import type { CronJob } from './store';
import { CronManager, type CronClock } from './manager';
import type { CronJobInfo } from '../kernel/contracts/cron';
import {
  CancelCron,
  CronJobEvent,
  CronTurnRequested,
  ListCronJobs,
  ScheduleCron,
} from '../kernel/contracts/cron';
import { RegisterTool } from '../kernel/contracts/tools';
import { TurnCompleted, TurnFailed, TurnStarted } from '../kernel/contracts/chat';

export interface CronModuleOptions {
  directory?: string;
  clock?: CronClock;
  /** Session to bind restored durable jobs to. */
  sessionId?: string;
  registerTools?: boolean;
}

interface PendingFuture {
  resolve: (value: string) => void;
  timer: ReturnType<typeof setTimeout>;
}

const TURN_TIMEOUT_MS = 600_000;

export class CronModule implements Module {
  readonly name = 'cron';
  readonly dependencies: readonly string[] = ['tools'];

  readonly manager: CronManager;
  private readonly registerTools: boolean;
  private readonly bindSessionId: string;
  private readonly pending = new Map<string, PendingFuture>();
  private readonly sessionLocks = new Map<string, Promise<void>>();
  private sessionId = '';
  private bus: AsyncEventBus | null = null;

  constructor(options: CronModuleOptions = {}) {
    this.registerTools = options.registerTools !== false;
    this.bindSessionId = options.sessionId ?? '';
    this.manager = new CronManager({
      directory: options.directory,
      clock: options.clock,
      onEvent: (job, status, streamId) => this.onJobEvent(job, status, streamId),
    });
    this.manager.setRunner((sessionId, jobId, name, prompt, streamId) =>
      this.runCronTurn(sessionId, jobId, name, prompt, streamId),
    );
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    bus.provide(ScheduleCron, (req) => {
      try {
        return this.manager.schedule({
          sessionId: req.sessionId || this.sessionId,
          cron: req.cron,
          prompt: req.prompt,
          recurring: req.recurring,
          durable: req.durable,
        });
      } catch (error) {
        throw new Error(error instanceof Error ? error.message : String(error));
      }
    });
    bus.provide(CancelCron, (req) => this.manager.cancel(req.jobId));
    bus.provide(ListCronJobs, () => this.manager.listJobs());

    await bus.subscribe(TurnStarted, (event) => {
      if (event.sessionId) this.sessionId = event.sessionId;
    });
    await bus.subscribe(TurnCompleted, (event) => {
      this.resolvePending(event.streamId, event.content);
    });
    await bus.subscribe(TurnFailed, (event) => {
      this.resolvePending(event.streamId, `Error: ${event.error}`);
    });

    await this.manager.restore(this.bindSessionId);
    if (this.registerTools) await this.registerCronTools();
  }

  async stop(): Promise<void> {
    for (const [streamId, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.resolve('Error: cron turn cancelled');
      this.pending.delete(streamId);
    }
    await this.manager.shutdown();
    this.bus = null;
  }

  async whenIdle(): Promise<void> {
    await this.manager.whenIdle();
  }

  // ── cron runner: publish command and wait for the turn result ─────────

  private runCronTurn(
    sessionId: string,
    jobId: string,
    name: string,
    prompt: string,
    streamId: string,
  ): Promise<string> {
    const bus = this.bus;
    if (!bus) return Promise.resolve('Error: cron module is not started');
    const effectiveSession = sessionId || this.sessionId;
    const runTurn = () => new Promise<string>((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(streamId);
        resolve('Error: cron turn timed out after 600s');
      }, TURN_TIMEOUT_MS);
      this.pending.set(streamId, {
        resolve: (value) => {
          clearTimeout(timer);
          this.pending.delete(streamId);
          resolve(value);
        },
        timer,
      });
      bus.publish(
        new CronTurnRequested({
          sessionId: effectiveSession,
          trigger: {
            job_id: jobId,
            name,
            prompt,
            stream_id: streamId,
            session_id: effectiveSession,
          },
        }),
      );
    });
    return this.withSessionLock(effectiveSession, runTurn).then(async (value) => {
      const job = this.manager.getJob(jobId);
      if (job && !job.sessionId && effectiveSession) {
        await this.manager.bindSession(jobId, effectiveSession).catch(() => undefined);
      }
      return value;
    });
  }

  /** Serialize cron turns per session (DSH-style per-session lock). */
  private withSessionLock<T>(sessionId: string, task: () => Promise<T>): Promise<T> {
    const key = sessionId || '__unbound__';
    const previous = this.sessionLocks.get(key) ?? Promise.resolve();
    const run = previous.catch(() => undefined).then(task);
    const tail = run.then(
      () => undefined,
      () => undefined,
    );
    this.sessionLocks.set(key, tail);
    return run.finally(() => {
      if (this.sessionLocks.get(key) === tail) this.sessionLocks.delete(key);
    });
  }

  private resolvePending(streamId: string, content: string): void {
    if (!streamId || !streamId.startsWith('cron:')) return;
    this.pending.get(streamId)?.resolve(content ?? '');
  }

  private onJobEvent(job: CronJob, status: string, streamId: string): void {
    if (!this.bus) return;
    this.bus.publish(
      new CronJobEvent({
        sessionId: job.sessionId,
        jobId: job.id,
        name: job.name,
        status,
        prompt: job.prompt,
        recurring: job.recurring,
        durable: job.durable,
        runsDone: job.runsDone,
        startedAt: job.lastStartedAt,
        finishedAt: job.lastFinishedAt,
        result: job.lastResult,
        error: job.lastError,
        toolCallId: streamId,
      }),
    );
  }

  // ── tools ──────────────────────────────────────────────────────────────

  private async registerCronTools(): Promise<void> {
    const bus = this.bus;
    if (!bus) return;
    try {
      await bus.request(
        new RegisterTool({
          name: 'cron',
          description:
            'Schedule a prompt-driven background cron job with a standard 5-field cron ' +
            'expression in local time. The prompt must contain only the actual task content.',
          jsonSchema: {
            type: 'object',
            properties: {
              cron: { type: 'string', description: "5-field crontab, e.g. '*/5 * * * *'" },
              prompt: { type: 'string', description: 'Task prompt executed on every fire' },
              recurring: { type: 'boolean', description: 'Keep running (default true)' },
              durable: { type: 'boolean', description: 'Persist across restarts (default false)' },
            },
            required: ['cron', 'prompt'],
          },
          callable: async (args: Record<string, unknown>) => {
            const cron = String(args.cron ?? '').trim();
            const prompt = String(args.prompt ?? '').trim();
            if (!cron) return 'Error: cron is required';
            if (!prompt) return 'Error: prompt is required';
            try {
              const jobId = await bus.request(
                new ScheduleCron({
                  sessionId: this.sessionId,
                  cron,
                  prompt,
                  recurring: args.recurring !== false,
                  durable: args.durable === true,
                }),
              );
              return `Scheduled: ${jobId}`;
            } catch (error) {
              return `Error: ${error instanceof Error ? error.message : String(error)}`;
            }
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'cron_cancel',
          description: 'Cancel and delete an existing scheduled cron job by job id.',
          jsonSchema: {
            type: 'object',
            properties: { job_id: { type: 'string', description: 'Cron job id' } },
            required: ['job_id'],
          },
          callable: async (args: Record<string, unknown>) => {
            const jobId = String(args.job_id ?? '').trim();
            if (!jobId) return 'Error: job_id is required';
            const cancelled = await bus.request(new CancelCron({ jobId }));
            return cancelled ? `Cancelled: ${jobId}` : `Error: cron job not found: ${jobId}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'cron_jobs',
          description: 'List currently scheduled cron jobs with their ids and status.',
          jsonSchema: { type: 'object', properties: {} },
          callable: async () => formatJobs(await bus.request(new ListCronJobs())),
        }),
      );
    } catch {
      // ToolsModule may not be started (unit tests) — skip silently.
    }
  }
}

function formatJobs(jobs: CronJobInfo[]): string {
  if (jobs.length === 0) return 'No cron jobs.';
  const lines = [`Total: ${jobs.length} cron job(s)`];
  for (const job of jobs) {
    const next = job.nextRunAt ? new Date(job.nextRunAt).toISOString() : '-';
    lines.push(
      `- ${job.id} [${job.status}] ${job.name} (${job.cron}) runs=${job.runsDone} next=${next}`,
    );
  }
  return lines.join('\n');
}