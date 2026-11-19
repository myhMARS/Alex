/** Durable agent-task log (JSONL), one record per start/finish. */

import { appendFile, mkdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';

import type { AgentTaskInfo } from '../kernel/contracts/agents';

interface TaskLogRecord {
  at: string;
  phase: 'started' | 'finished';
  task: AgentTaskInfo;
}

export function defaultTeamTasksPath(): string {
  return join(homedir(), '.alex', 'team', 'tasks.jsonl');
}

export class AgentTaskStore {
  readonly path: string;

  constructor(path = defaultTeamTasksPath()) {
    this.path = path;
  }

  async append(phase: 'started' | 'finished', task: AgentTaskInfo): Promise<void> {
    try {
      await mkdir(dirname(this.path), { recursive: true });
      const record: TaskLogRecord = {
        at: new Date().toISOString(),
        phase,
        task: { ...task },
      };
      await appendFile(this.path, `${JSON.stringify(record)}\n`, 'utf8');
    } catch {
      // Task history must never break execution.
    }
  }

  async load(): Promise<AgentTaskInfo[]> {
    let raw = '';
    try {
      raw = await readFile(this.path, 'utf8');
    } catch {
      return [];
    }
    const latest = new Map<string, AgentTaskInfo>();
    for (const line of raw.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const record = JSON.parse(line) as TaskLogRecord;
        const task = record.task;
        if (!task?.taskId) continue;
        const previous = latest.get(task.taskId);
        latest.set(task.taskId, {
          ...(previous ?? {}),
          ...task,
          // A running task from a previous process never completed.
          status:
            task.status === 'running'
              ? 'cancelled'
              : task.status,
          error:
            task.status === 'running' && !task.error
              ? 'interrupted by restart'
              : task.error,
          finishedAt:
            task.status === 'running'
              ? (task.finishedAt ?? Date.now())
              : task.finishedAt,
        } as AgentTaskInfo);
      } catch {
        // Ignore a corrupt line; the rest of the history remains readable.
      }
    }
    return [...latest.values()];
  }
}