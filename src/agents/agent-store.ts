/** Durable agent metadata so workers can be resumed after restart. */

import { appendFile, mkdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';

import type { AgentInfo } from '../kernel/contracts/agents';

interface PersistedAgentRecord {
  at: string;
  info: AgentInfo;
}

export function defaultTeamAgentsPath(): string {
  return join(homedir(), '.alex', 'team', 'agents.jsonl');
}

export class AgentStore {
  readonly path: string;

  constructor(path = defaultTeamAgentsPath()) {
    this.path = path;
  }

  async save(info: AgentInfo): Promise<void> {
    try {
      await mkdir(dirname(this.path), { recursive: true });
      const record: PersistedAgentRecord = {
        at: new Date().toISOString(),
        info: { ...info, status: 'idle' },
      };
      await appendFile(this.path, `${JSON.stringify(record)}\n`, 'utf8');
    } catch {
      // Agent history must not break spawning.
    }
  }

  async load(): Promise<AgentInfo[]> {
    let raw = '';
    try {
      raw = await readFile(this.path, 'utf8');
    } catch {
      return [];
    }
    const latest = new Map<string, AgentInfo>();
    for (const line of raw.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const record = JSON.parse(line) as PersistedAgentRecord;
        const info = record.info;
        if (!info?.agentId) continue;
        latest.set(info.agentId, {
          ...info,
          status: 'resumable' as const,
        });
      } catch {
        // Ignore a corrupt record.
      }
    }
    return [...latest.values()];
  }
}