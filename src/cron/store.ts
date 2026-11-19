/** Durable cron job store — compatible with alex/scheduler/cron_store.py. */

import { mkdir, readFile, readdir, rename, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

export interface CronJob {
  id: string;
  sessionId: string;
  name: string;
  cron: string;
  prompt: string;
  recurring: boolean;
  durable: boolean;
  status: string;
  runsDone: number;
  lastStartedAt: number | null;
  lastFinishedAt: number | null;
  lastError: string;
  lastResult: string;
  nextRunAt: number | null;
}

export interface CronStoreOptions {
  directory?: string;
}

export function defaultCronDir(): string {
  return join(homedir(), '.alex', 'cron');
}

interface CronJobFile {
  id?: string;
  session_id?: string;
  name?: string;
  cron?: string;
  prompt?: string;
  recurring?: boolean;
  durable?: boolean;
  status?: string;
  runs_done?: number;
  last_started_at?: number | null;
  last_finished_at?: number | null;
  last_error?: string;
  last_result?: string;
  next_run_at?: number | null;
}

export function deriveJobName(prompt: string): string {
  const first = (prompt ?? '').trim().split('\n')[0] ?? '';
  return first.slice(0, 24) || 'cron job';
}

export function cronJobToFile(job: CronJob): Record<string, unknown> {
  return {
    id: job.id,
    session_id: job.sessionId,
    name: job.name,
    cron: job.cron,
    prompt: job.prompt,
    recurring: job.recurring,
    durable: job.durable,
    status: job.status,
    runs_done: job.runsDone,
    last_started_at: job.lastStartedAt,
    last_finished_at: job.lastFinishedAt,
    last_error: job.lastError,
    last_result: job.lastResult,
    next_run_at: job.nextRunAt,
  };
}

export function cronJobFromFile(data: CronJobFile): CronJob {
  return {
    id: String(data.id ?? ''),
    sessionId: String(data.session_id ?? ''),
    name: String(data.name ?? '') || deriveJobName(String(data.prompt ?? '')),
    cron: String(data.cron ?? '').trim(),
    prompt: String(data.prompt ?? ''),
    recurring: data.recurring !== false,
    durable: data.durable === true,
    status: String(data.status ?? 'SCHEDULED') || 'SCHEDULED',
    runsDone: Number(data.runs_done ?? 0) || 0,
    lastStartedAt: typeof data.last_started_at === 'number' ? data.last_started_at : null,
    lastFinishedAt: typeof data.last_finished_at === 'number' ? data.last_finished_at : null,
    lastError: String(data.last_error ?? ''),
    lastResult: String(data.last_result ?? ''),
    nextRunAt: typeof data.next_run_at === 'number' ? data.next_run_at : null,
  };
}

export class CronStore {
  readonly directory: string;

  constructor(options: CronStoreOptions = {}) {
    this.directory = options.directory ?? defaultCronDir();
  }

  private jobPath(jobId: string): string {
    return join(this.directory, `${jobId}.json`);
  }

  async persist(job: CronJob): Promise<void> {
    if (!job.durable) return;
    await mkdir(this.directory, { recursive: true });
    const path = this.jobPath(job.id);
    const tmp = `${path}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
    try {
      await writeFile(tmp, JSON.stringify(cronJobToFile(job), null, 2), 'utf8');
      await rename(tmp, path);
    } catch (error) {
      await unlink(tmp).catch(() => undefined);
      throw error;
    }
  }

  async delete(jobId: string): Promise<void> {
    await unlink(this.jobPath(jobId)).catch(() => undefined);
  }

  /** Restore durable jobs; finished one-shots are deleted, RUNNING is reset. */
  async restoreAll(): Promise<CronJob[]> {
    let entries: string[];
    try {
      entries = await readdir(this.directory);
    } catch {
      return [];
    }
    const jobs: CronJob[] = [];
    for (const entry of entries.sort()) {
      if (!entry.endsWith('.json')) continue;
      try {
        const raw = await readFile(join(this.directory, entry), 'utf8');
        const job = cronJobFromFile(JSON.parse(raw) as CronJobFile);
        if (!job.id || !job.cron || !job.prompt) continue;
        if (!job.recurring && job.runsDone > 0) {
          await this.delete(job.id);
          continue;
        }
        if (job.status === 'RUNNING') job.status = 'SCHEDULED';
        jobs.push(job);
      } catch {
        // ignore corrupt file
      }
    }
    return jobs;
  }
}