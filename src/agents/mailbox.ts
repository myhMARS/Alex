/** Durable per-agent mailbox log (JSONL). */

import { appendFile, mkdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';

import type { AgentMailboxEntry } from '../kernel/contracts/agents';

export function defaultTeamMailboxPath(): string {
  return join(homedir(), '.alex', 'team', 'mailbox.jsonl');
}

export class AgentMailbox {
  readonly path: string;
  readonly ackPath: string;
  readonly attemptPath: string;
  private entries: AgentMailboxEntry[] = [];
  private readonly acks = new Set<string>();
  private readonly ackAt = new Map<string, string>();
  private readonly attempts = new Map<string, number>();
  private readonly reasons = new Map<string, string>();

  constructor(path = defaultTeamMailboxPath()) {
    this.path = path;
    this.ackPath = `${path}.acks.jsonl`;
    this.attemptPath = `${path}.attempts.jsonl`;
  }

  async load(): Promise<void> {
    let raw = '';
    try {
      raw = await readFile(this.path, 'utf8');
    } catch {
      return;
    }
    const entries: AgentMailboxEntry[] = [];
    for (const line of raw.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const entry = JSON.parse(line) as AgentMailboxEntry;
        if (entry.messageId && entry.toAgentId) entries.push(entry);
      } catch {
        // Ignore corrupt lines; keep the readable mailbox.
      }
    }
    this.entries = entries;
    let ackRaw = '';
    try {
      ackRaw = await readFile(this.ackPath, 'utf8');
    } catch {
      ackRaw = '';
    }
    for (const line of ackRaw.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const ack = JSON.parse(line) as { messageId?: string; at?: string };
        if (ack.messageId) {
          this.acks.add(ack.messageId);
          if (ack.at) this.ackAt.set(ack.messageId, ack.at);
        }
      } catch {
        // Ignore corrupt ack lines.
      }
    }
    let attemptRaw = '';
    try {
      attemptRaw = await readFile(this.attemptPath, 'utf8');
    } catch {
      attemptRaw = '';
    }
    for (const line of attemptRaw.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const record = JSON.parse(line) as {
          messageId?: string;
          attempts?: number;
          reason?: string;
        };
        if (record.messageId && typeof record.attempts === 'number') {
          this.attempts.set(record.messageId, record.attempts);
          if (record.reason) this.reasons.set(record.messageId, record.reason);
        }
      } catch {
        // Ignore corrupt attempt lines.
      }
    }
  }

  record(entry: AgentMailboxEntry): void {
    this.entries.push(entry);
    void this.append(entry);
  }

  read(
    agentId: string,
    limit = 20,
    unreadOnly = false,
  ): AgentMailboxEntry[] {
    const matching = this.entries.filter((entry) => {
      if (entry.toAgentId !== agentId) return false;
      if (unreadOnly && this.acks.has(entry.messageId)) return false;
      return true;
    });
    return matching.slice(-Math.max(1, limit)).map((entry) => ({
      ...entry,
      read: this.acks.has(entry.messageId),
      attempts: this.attempts.get(entry.messageId) ?? 0,
      failureReason: this.reasons.get(entry.messageId),
    }));
  }

  async ack(agentId: string, messageIds?: string[]): Promise<number> {
    const targets = new Set(messageIds ?? []);
    let acked = 0;
    for (const entry of this.entries) {
      if (entry.toAgentId !== agentId) continue;
      if (targets.size > 0 && !targets.has(entry.messageId)) continue;
      if (this.acks.has(entry.messageId)) continue;
      this.acks.add(entry.messageId);
      const at = new Date().toISOString();
      this.ackAt.set(entry.messageId, at);
      acked += 1;
      try {
        await appendFile(
          this.ackPath,
          `${JSON.stringify({ messageId: entry.messageId, agentId, at })}\n`,
          'utf8',
        );
      } catch {
        // Ack persistence must not break delivery.
      }
    }
    return acked;
  }

  async recordAttempt(messageId: string, reason?: string): Promise<number> {
    const attempts = (this.attempts.get(messageId) ?? 0) + 1;
    this.attempts.set(messageId, attempts);
    if (reason) this.reasons.set(messageId, reason);
    try {
      await appendFile(
        this.attemptPath,
        `${JSON.stringify({ messageId, attempts, ...(reason ? { reason } : {}) })}\n`,
        'utf8',
      );
    } catch {
      // Attempt tracking must not break replay.
    }
    return attempts;
  }

  async revive(messageIds: string[]): Promise<number> {
    let revived = 0;
    for (const messageId of messageIds) {
      if ((this.attempts.get(messageId) ?? 0) === 0) continue;
      this.attempts.set(messageId, 0);
      revived += 1;
      try {
        await appendFile(
          this.attemptPath,
          `${JSON.stringify({ messageId, attempts: 0 })}\n`,
          'utf8',
        );
      } catch {
        // Revival must not break mailbox reads.
      }
    }
    return revived;
  }

  deadLetters(agentId: string, maxAttempts = 3): AgentMailboxEntry[] {
    return this.entries
      .filter(
        (entry) =>
          entry.toAgentId === agentId &&
          !this.acks.has(entry.messageId) &&
          (this.attempts.get(entry.messageId) ?? 0) >= maxAttempts,
      )
      .map((entry) => ({
        ...entry,
        read: false,
        attempts: this.attempts.get(entry.messageId) ?? 0,
        failureReason: this.reasons.get(entry.messageId),
      }));
  }

  status(agentId: string, maxAttempts = 3): {
    total: number;
    read: number;
    unread: number;
    lastReadMessageId?: string;
    lastReadAt?: string;
    dead: number;
  } {
    const entries = this.entries.filter((entry) => entry.toAgentId === agentId);
    const readEntries = entries.filter((entry) => this.acks.has(entry.messageId));
    const lastRead = readEntries[readEntries.length - 1];
    return {
      total: entries.length,
      read: readEntries.length,
      unread: entries.length - readEntries.length,
      lastReadMessageId: lastRead?.messageId,
      lastReadAt: lastRead ? this.ackAt.get(lastRead.messageId) : undefined,
      dead: entries.filter(
        (entry) =>
          !this.acks.has(entry.messageId) &&
          (this.attempts.get(entry.messageId) ?? 0) >= maxAttempts,
      ).length,
    };
  }

  private async append(entry: AgentMailboxEntry): Promise<void> {
    try {
      await mkdir(dirname(this.path), { recursive: true });
      await appendFile(this.path, `${JSON.stringify(entry)}\n`, 'utf8');
    } catch {
      // Mailbox persistence must never break message delivery.
    }
  }
}