/** Cron contracts — TypeScript port of alex/kernel/contracts/cron.py. */

import { Command, Event, Request, type MessageContext } from '../message';

export class ScheduleCron extends Request<string> {
  cron: string;
  prompt: string;
  recurring: boolean;
  durable: boolean;

  constructor(
    init: {
      cron?: string;
      prompt?: string;
      recurring?: boolean;
      durable?: boolean;
    } & { sessionId?: string } = {},
  ) {
    super(init);
    this.cron = init.cron ?? '';
    this.prompt = init.prompt ?? '';
    this.recurring = init.recurring ?? true;
    this.durable = init.durable ?? false;
  }
}

export class CancelCron extends Request<boolean> {
  jobId: string;

  constructor(init: { jobId?: string; sessionId?: string } = {}) {
    super(init);
    this.jobId = init.jobId ?? '';
  }
}

export interface CronJobInfo {
  id: string;
  name: string;
  status: string;
  prompt: string;
  recurring: boolean;
  durable: boolean;
  cron?: string;
  runsDone?: number;
  nextRunAt?: number;
  lastStartedAt?: number;
  lastFinishedAt?: number;
  lastResult?: string;
  lastError?: string;
}

export class ListCronJobs extends Request<CronJobInfo[]> {}

/** Published by cron when a scheduled job fires — the agent subscribes. */
export class CronTurnRequested extends Command {
  trigger: Record<string, unknown>;

  constructor(init: MessageContext & { trigger?: Record<string, unknown> } = {}) {
    super(init);
    this.trigger = init.trigger ?? {};
  }
}

/** Published when a cron job changes status (UI + store notification). */
export class CronJobEvent extends Event {
  jobId: string;
  name: string;
  status: string;
  prompt: string;
  recurring: boolean;
  durable: boolean;
  runsDone: number;
  startedAt: number | null;
  finishedAt: number | null;
  result: string;
  error: string;
  toolCallId: string;

  constructor(
    init: MessageContext & {
      jobId?: string;
      name?: string;
      status?: string;
      prompt?: string;
      recurring?: boolean;
      durable?: boolean;
      runsDone?: number;
      startedAt?: number | null;
      finishedAt?: number | null;
      result?: string;
      error?: string;
      toolCallId?: string;
    } = {},
  ) {
    super(init);
    this.jobId = init.jobId ?? '';
    this.name = init.name ?? '';
    this.status = init.status ?? '';
    this.prompt = init.prompt ?? '';
    this.recurring = init.recurring ?? true;
    this.durable = init.durable ?? false;
    this.runsDone = init.runsDone ?? 0;
    this.startedAt = init.startedAt ?? null;
    this.finishedAt = init.finishedAt ?? null;
    this.result = init.result ?? '';
    this.error = init.error ?? '';
    this.toolCallId = init.toolCallId ?? '';
  }
}