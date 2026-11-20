/**
 * Session persistence — append-only JSONL event log (latest format only).
 *
 * File format:
 *   line 1: session header
 *   line 2..n: typed session events (seq, type, at, data)
 *
 * Older Python/JSON snapshot sessions are intentionally not supported.
 */

import {
  appendFile,
  mkdir,
  readFile,
  open,
  readdir,
  rename,
  stat,
  unlink,
  writeFile,
} from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

import type { Module } from '../../kernel/runtime';
import type { AsyncEventBus } from '../../kernel/bus';
import type { MessageDTO } from '../../kernel/dto/message';
import { TurnCompleted } from '../../kernel/contracts/chat';
import {
  CheckpointSession,
  ForkSession,
  ListSessions,
  LoadSession,
  RepairSession,
  type RepairResult,
  SessionEventAppended,
  type SessionBundle,
  type SessionEventRecord,
  type SessionMeta,
} from '../../kernel/contracts/session';
import { projectSessionEvents } from '../../session/projector';

const SESSION_SUFFIX = '.jsonl';
export const SESSION_FORMAT_VERSION = 3;

interface SessionHeader {
  kind: 'session';
  format_version: number;
  session_id: string;
  created_at: string;
  first_message: string;
  cron_history: Record<string, unknown>[];
  parent_session_id?: string;
  forked_at?: string;
  fork_boundary_seq?: number;
}

interface SessionFile {
  format_version: number;
  session_id: string;
  created_at: string;
  first_message: string;
  events: SessionEventRecord[];
  cron_history: Record<string, unknown>[];
  parent_session_id?: string;
  forked_at?: string;
  fork_boundary_seq?: number;
}

export interface StoreModuleOptions {
  sessionsDir?: string;
}

export function defaultSessionsDir(): string {
  return join(homedir(), '.alex', 'sessions');
}

export class StoreModule implements Module {
  readonly name = 'store';
  readonly dependencies: readonly string[] = [];
  private readonly sessionsDir: string;

  constructor(options: StoreModuleOptions = {}) {
    this.sessionsDir = options.sessionsDir ?? defaultSessionsDir();
  }

  async start(bus: AsyncEventBus): Promise<void> {
    await bus.subscribe(TurnCompleted, (event) => this.onTurnCompleted(event));
    await bus.subscribe(SessionEventAppended, (event) =>
      this.appendEventRecords(event.sessionId, event.events),
    );
    bus.provide(ListSessions, () => this.listSessions());
    bus.provide(LoadSession, (req) =>
      this.loadSession(req.targetSessionId || req.sessionId),
    );
    bus.provide(ForkSession, (req) => this.forkSession(req));
    bus.provide(RepairSession, (req) => this.repairSession(req));
    bus.provide(CheckpointSession, (req) => this.checkpointSession(req));
  }

  /** Flush the JSONL file to disk (DSH-style durability checkpoint). */
  async checkpointSession(req: CheckpointSession): Promise<{
    checkpointed: boolean;
    bytes: number;
  }> {
    const path = this.sessionPath(req.targetSessionId);
    try {
      const info = await stat(path);
      const handle = await open(path, 'a');
      try {
        await handle.sync();
      } finally {
        await handle.close();
      }
      return { checkpointed: true, bytes: info.size };
    } catch (error) {
      if (isNotFound(error)) return { checkpointed: false, bytes: 0 };
      throw error;
    }
  }

  /** Truncate a corrupted JSONL log at its first invalid event and rewrite atomically. */
  async repairSession(req: RepairSession): Promise<RepairResult | null> {
    const sessionId = req.targetSessionId;
    if (!sessionId) return null;
    let raw: string;
    try {
      raw = await readFile(this.sessionPath(sessionId), 'utf8');
    } catch (error) {
      if (isNotFound(error)) return null;
      throw error;
    }
    const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
    if (lines.length === 0) return null;
    const header = JSON.parse(lines[0]!) as SessionHeader;
    if (header.kind !== 'session' || header.format_version !== SESSION_FORMAT_VERSION) {
      throw new Error(`无法修复：不支持或缺少 session 头（${sessionId}）`);
    }
    const valid: SessionEventRecord[] = [];
    let dropped = 0;
    for (const line of lines.slice(1)) {
      try {
        valid.push(JSON.parse(line) as SessionEventRecord);
      } catch {
        dropped = lines.length - 1 - valid.length;
        break;
      }
    }
    if (dropped === 0) {
      return { repaired: false, validEvents: valid.length, droppedEvents: 0 };
    }
    const path = this.sessionPath(sessionId);
    const tmp = `${path}.${process.pid}.repair.tmp`;
    await writeFile(
      tmp,
      `${[JSON.stringify(header), ...valid.map((event) => JSON.stringify(event))].join('\n')}\n`,
      'utf8',
    );
    await rename(tmp, path).catch(async (error) => {
      await unlink(tmp).catch(() => undefined);
      throw error;
    });
    return { repaired: true, validEvents: valid.length, droppedEvents: dropped };
  }

  private async onTurnCompleted(event: TurnCompleted): Promise<void> {
    if (!event.sessionId) return;
    await this.saveSession(event.sessionId, event.messages);
  }

  /** Persist events immediately — the log is the source of truth. */
  private async appendEventRecords(
    sessionId: string,
    records: SessionEventRecord[],
  ): Promise<void> {
    if (!sessionId || records.length === 0) return;
    await mkdir(this.sessionsDir, { recursive: true });
    const path = this.sessionPath(sessionId);
    const existing = await this.readSessionFile(sessionId);
    if (!existing) {
      const events = records.map((record, index) => ({
        ...record,
        seq: record.seq > 0 ? record.seq : index + 1,
      }));
      const header: SessionHeader = {
        kind: 'session',
        format_version: SESSION_FORMAT_VERSION,
        session_id: sessionId,
        created_at: new Date().toISOString(),
        first_message: computeFirstMessage(projectSessionEvents(events)),
        cron_history: [],
      };
      const lines = [JSON.stringify(header), ...events.map((event) => JSON.stringify(event))];
      await writeFile(path, `${lines.join('\n')}\n`, 'utf8');
      await syncFile(path);
      return;
    }
    let lastSeq = existing.events.length > 0
      ? existing.events[existing.events.length - 1]!.seq
      : 0;
    const appended = records.map((record) => ({
      ...record,
      seq: record.seq > lastSeq ? record.seq : ++lastSeq,
    }));
    await appendFile(
      path,
      `${appended.map((event) => JSON.stringify(event)).join('\n')}\n`,
      'utf8',
    );
    await syncFile(path);
  }

  /** Fork a session by copying its complete event log. */
  async forkSession(req: ForkSession): Promise<SessionBundle | null> {
    const source = await this.readSessionFile(req.sourceSessionId);
    if (!source) return null;
    const events =
      req.boundarySeq === undefined
        ? source.events
        : source.events.filter((event) => event.seq <= req.boundarySeq!);
    const targetId =
      req.targetSessionId || `session-fork-${Date.now().toString(36)}`;
    const created = new Date().toISOString();
    const firstMessage = computeFirstMessage(projectSessionEvents(events));
    const header: SessionHeader = {
      kind: 'session',
      format_version: SESSION_FORMAT_VERSION,
      session_id: targetId,
      created_at: created,
      first_message: firstMessage || source.first_message,
      cron_history: source.cron_history,
      parent_session_id: source.session_id,
      forked_at: created,
      fork_boundary_seq: req.boundarySeq,
    };
    const path = this.sessionPath(targetId);
    await mkdir(this.sessionsDir, { recursive: true });
    await writeFile(
      path,
      `${[JSON.stringify(header), ...events.map((event) => JSON.stringify(event))].join('\n')}\n`,
      'utf8',
    );
    await syncFile(path);
    return {
      sessionId: targetId,
      createdAt: created,
      firstMessage: firstMessage || source.first_message,
      messages: projectSessionEvents(events),
      cronHistory: source.cron_history,
      events,
      formatVersion: SESSION_FORMAT_VERSION,
      parentSessionId: source.session_id,
      forkedAt: created,
      forkBoundarySeq: req.boundarySeq,
    };
  }

  /** Append newly observed events to the JSONL log. */
  async saveSession(
    sessionId: string,
    messages: MessageDTO[],
    cronHistory?: Record<string, unknown>[],
  ): Promise<boolean> {
    if (!sessionId) throw new Error('sessionId is required');
    await mkdir(this.sessionsDir, { recursive: true });
    const path = this.sessionPath(sessionId);
    const existing = await this.readSessionFile(sessionId);
    const events = [...(existing?.events ?? [])];
    const projectedBefore = projectSessionEvents(events);
    let changed = false;
    if (messages.length > projectedBefore.length) {
      let nextSeq = events.length > 0 ? events[events.length - 1]!.seq + 1 : 1;
      for (const message of messages.slice(projectedBefore.length)) {
        events.push({
          seq: nextSeq++,
          type:
            message.role === 'user'
              ? 'user_message'
              : message.role === 'tool'
                ? 'tool_message'
                : 'assistant_message',
          at: new Date().toISOString(),
          data: { message },
        });
        changed = true;
      }
    }
    if (!existing) {
      const projected = projectSessionEvents(events);
      const header: SessionHeader = {
        kind: 'session',
        format_version: SESSION_FORMAT_VERSION,
        session_id: sessionId,
        created_at: new Date().toISOString(),
        first_message: computeFirstMessage(projected),
        cron_history: cronHistory ?? [],
      };
      const lines = [JSON.stringify(header), ...events.map((event) => JSON.stringify(event))];
      await writeFile(path, `${lines.join('\n')}\n`, 'utf8');
      await syncFile(path);
      return true;
    }
    if (changed) {
      const appended = events.slice(existing.events.length);
      await appendFile(
        path,
        `${appended.map((event) => JSON.stringify(event)).join('\n')}\n`,
        'utf8',
      );
      await syncFile(path);
    }
    return true;
  }

  async listSessions(): Promise<SessionMeta[]> {
    let entries: string[];
    try {
      entries = await readdir(this.sessionsDir);
    } catch (error) {
      if (isNotFound(error)) return [];
      throw error;
    }
    const sessions: { meta: SessionMeta; mtimeMs: number }[] = [];
    for (const entry of entries) {
      if (!entry.endsWith(SESSION_SUFFIX)) continue;
      const data = await this.readSessionFile(
        entry.slice(0, -SESSION_SUFFIX.length),
      ).catch(() => null);
      if (!data) continue;
      const info = await stat(join(this.sessionsDir, entry)).catch(() => null);
      sessions.push({
        mtimeMs: info?.mtimeMs ?? 0,
        meta: {
          sessionId: data.session_id,
          createdAt: data.created_at,
          updatedAt: info ? new Date(info.mtimeMs).toISOString() : data.created_at,
          firstMessage: (data.first_message || '').slice(0, 80),
          messageCount: projectSessionEvents(data.events).length,
          title: sessionTitle(data.events),
          parentSessionId: data.parent_session_id,
          forkedAt: data.forked_at,
          forkBoundarySeq: data.fork_boundary_seq,
          agentId: sessionAgentId(data.events),
        },
      });
    }
    sessions.sort((a, b) => {
      if (b.mtimeMs !== a.mtimeMs) return b.mtimeMs - a.mtimeMs;
      return b.meta.createdAt.localeCompare(a.meta.createdAt);
    });
    return sessions.map((item) => item.meta);
  }

  async loadSession(sessionId: string | undefined): Promise<SessionBundle | null> {
    if (!sessionId) return null;
    const data = await this.readSessionFile(sessionId);
    if (!data) return null;
    const messages = projectSessionEvents(data.events);
    return {
      sessionId: data.session_id || sessionId,
      createdAt: data.created_at,
      firstMessage: data.first_message,
      title: sessionTitle(data.events),
      messages,
      cronHistory: data.cron_history,
      events: data.events,
      formatVersion: data.format_version,
      parentSessionId: data.parent_session_id,
      forkedAt: data.forked_at,
      forkBoundarySeq: data.fork_boundary_seq,
      agentId: sessionAgentId(data.events),
    };
  }

  private sessionPath(sessionId: string): string {
    return join(this.sessionsDir, `${sessionId}${SESSION_SUFFIX}`);
  }

  private async readSessionFile(sessionId: string): Promise<SessionFile | null> {
    try {
      const raw = await readFile(this.sessionPath(sessionId), 'utf8');
      const lines = raw.split(/\r?\n/).filter((line) => line.trim().length > 0);
      if (lines.length === 0) return null;
      const header = JSON.parse(lines[0]!) as Partial<SessionHeader>;
      if (header.kind !== 'session') {
        throw new Error(`会话文件缺少 session 头（${sessionId}）`);
      }
      if (header.format_version !== SESSION_FORMAT_VERSION) {
        throw new Error(
          `不支持的 session 版本：${String(header.format_version)}（需要 ${SESSION_FORMAT_VERSION}）`,
        );
      }
      const events: SessionEventRecord[] = [];
      for (const line of lines.slice(1)) {
        try {
          events.push(JSON.parse(line) as SessionEventRecord);
        } catch {
          throw new Error(`会话事件损坏（${sessionId}）`);
        }
      }
      return {
        format_version: SESSION_FORMAT_VERSION,
        session_id: header.session_id || sessionId,
        created_at: header.created_at || '',
        first_message: header.first_message || '',
        events,
        cron_history: Array.isArray(header.cron_history) ? header.cron_history : [],
        parent_session_id: header.parent_session_id,
        forked_at: header.forked_at,
        fork_boundary_seq: header.fork_boundary_seq,
      };
    } catch (error) {
      if (isNotFound(error)) return null;
      if (error instanceof SyntaxError) {
        throw new Error(`会话文件损坏（${sessionId}）：${error.message}`);
      }
      throw error;
    }
  }
}

async function syncFile(path: string): Promise<void> {
  const handle = await open(path, 'a');
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

function sessionTitle(events: SessionEventRecord[]): string | undefined {
  let title: string | undefined;
  for (const event of events) {
    if (event.type !== 'session_title') continue;
    const value = event.data?.title;
    if (typeof value === 'string' && value.trim()) title = value.trim();
  }
  return title;
}

function sessionAgentId(events: SessionEventRecord[]): string | undefined {
  for (const event of events) {
    if (event.type !== 'turn_started') continue;
    const agentId = event.data?.agentId;
    if (typeof agentId === 'string' && agentId) return agentId;
  }
  return undefined;
}

function computeFirstMessage(messages: MessageDTO[]): string {
  const user = messages.find((message) => message.role === 'user');
  const content = user?.content ?? messages[0]?.content ?? '';
  return typeof content === 'string' ? content.slice(0, 80) : '';
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'code' in error &&
    (error as { code?: string }).code === 'ENOENT'
  );
}