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
import { join, resolve } from 'node:path';

import type { Module } from '../../kernel/runtime';
import type { AsyncEventBus } from '../../kernel/bus';
import type { MessageDTO } from '../../kernel/dto/message';
import { TurnCompleted } from '../../kernel/contracts/chat';
import {
  CheckpointSession,
  ExportSession,
  ForkSession,
  ListSessions,
  LoadSession,
  RemoveSession,
  RepairSession,
  UpdateSessionMeta,
  type RepairResult,
  SessionEventAppended,
  type SessionBundle,
  type SessionEventRecord,
  type SessionExportResult,
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
  workspace?: string;
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
  workspace?: string;
  events: SessionEventRecord[];
  cron_history: Record<string, unknown>[];
  parent_session_id?: string;
  forked_at?: string;
  fork_boundary_seq?: number;
}

export interface StoreModuleOptions {
  sessionsDir?: string;
  /** Directory for Markdown exports (default ~/.alex/exports). */
  exportsDir?: string;
  /** Workspace bound to this store (default process.cwd()). */
  cwd?: string;
}

export function defaultSessionsDir(): string {
  return join(homedir(), '.alex', 'sessions');
}

export class StoreModule implements Module {
  readonly name = 'store';
  readonly dependencies: readonly string[] = [];
  private readonly sessionsDir: string;
  private readonly exportsDir: string;
  private readonly workspace: string;
  /** Last persisted seq per session; avoids re-reading the whole JSONL per event. */
  private readonly seqCache = new Map<string, number>();
  /** Projected message count per session; avoids re-reading on every TurnCompleted. */
  private readonly messageCountCache = new Map<string, number>();

  constructor(options: StoreModuleOptions = {}) {
    this.sessionsDir = options.sessionsDir ?? defaultSessionsDir();
    this.exportsDir = options.exportsDir ?? join(homedir(), '.alex', 'exports');
    this.workspace = resolve(options.cwd ?? process.cwd());
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
    bus.provide(UpdateSessionMeta, (req) => this.updateSessionMeta(req));
    bus.provide(RemoveSession, (req) => this.removeSession(req.targetSessionId));
    bus.provide(ExportSession, (req) => this.exportSession(req));
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
    this.seqCache.set(sessionId, valid[valid.length - 1]?.seq ?? 0);
    this.messageCountCache.set(sessionId, projectSessionEvents(valid).length);
    return { repaired: true, validEvents: valid.length, droppedEvents: dropped };
  }

  private async updateSessionMeta(req: UpdateSessionMeta): Promise<boolean> {
    const id = req.targetSessionId;
    if (!id) return false;
    const existing = await this.readSessionFile(id);
    if (!existing) return false;
    const events: SessionEventRecord[] = [];
    if (req.patch.title !== undefined) {
      const title = req.patch.title.trim();
      if (!title) return false;
      events.push({
        seq: 0,
        type: 'session_title',
        at: new Date().toISOString(),
        data: { title, auto: false },
      });
    }
    if (req.patch.pinned !== undefined) {
      events.push({
        seq: 0,
        type: 'session_pinned',
        at: new Date().toISOString(),
        data: { pinned: req.patch.pinned },
      });
    }
    if (req.patch.archived !== undefined) {
      events.push({
        seq: 0,
        type: 'session_archived',
        at: new Date().toISOString(),
        data: { archived: req.patch.archived },
      });
    }
    if (events.length === 0) return true;
    await this.appendEventRecords(id, events);
    return true;
  }

  private async removeSession(sessionId: string): Promise<boolean> {
    if (!sessionId) return false;
    try {
      await unlink(this.sessionPath(sessionId));
      this.seqCache.delete(sessionId);
      this.messageCountCache.delete(sessionId);
      return true;
    } catch (error) {
      if (isNotFound(error)) return false;
      throw error;
    }
  }

  private async exportSession(req: ExportSession): Promise<SessionExportResult | null> {
    const id = req.targetSessionId;
    if (!id) return null;
    const data = await this.readSessionFile(id);
    if (!data) return null;
    const messages = projectSessionEvents(data.events);
    const title = sessionTitle(data.events) || data.session_id;
    const markdown = renderSessionMarkdown(
      title,
      data.session_id,
      data.created_at,
      messages,
    );
    await mkdir(this.exportsDir, { recursive: true });
    const safe = data.session_id.replace(/[^a-zA-Z0-9._-]/g, '_');
    const path = join(this.exportsDir, `${safe}.md`);
    await writeFile(path, markdown, 'utf8');
    return { path, markdown, messageCount: messages.length };
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
    let lastSeq = this.seqCache.get(sessionId);
    if (lastSeq === undefined) {
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
          workspace: this.workspace,
          cron_history: [],
        };
        const lines = [JSON.stringify(header), ...events.map((event) => JSON.stringify(event))];
        await writeFile(path, `${lines.join('\n')}\n`, 'utf8');
        this.seqCache.set(sessionId, events[events.length - 1]?.seq ?? 0);
        this.messageCountCache.set(sessionId, projectSessionEvents(events).length);
        return;
      }
      lastSeq = existing.events[existing.events.length - 1]?.seq ?? 0;
      this.seqCache.set(sessionId, lastSeq);
      this.messageCountCache.set(sessionId, projectSessionEvents(existing.events).length);
    }
    let seq = lastSeq ?? 0;
    const appended = records.map((record) => ({
      ...record,
      seq: record.seq > seq ? record.seq : ++seq,
    }));
    await appendFile(
      path,
      `${appended.map((event) => JSON.stringify(event)).join('\n')}\n`,
      'utf8',
    );
    this.seqCache.set(sessionId, seq);
    let messageCount = this.messageCountCache.get(sessionId) ?? 0;
    for (const event of appended) messageCount = nextMessageCount(messageCount, event);
    this.messageCountCache.set(sessionId, messageCount);
    // Do not fsync every event batch: this caused visible pauses on large
    // sessions (each event used to re-read the whole JSONL and fsync it).
    // Explicit CheckpointSession still flushes to disk.
  }

  /** Fork a session by copying its complete event log. */
  async forkSession(req: ForkSession): Promise<SessionBundle | null> {
    const source = await this.readSessionFile(req.sourceSessionId);
    if (!source) return null;
    this.assertWorkspace(source);
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
      workspace: source.workspace ?? this.workspace,
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
    this.seqCache.set(targetId, events[events.length - 1]?.seq ?? 0);
    this.messageCountCache.set(targetId, projectSessionEvents(events).length);
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
    const cachedMessages = this.messageCountCache.get(sessionId);
    if (cachedMessages !== undefined && messages.length <= cachedMessages) return true;
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
        workspace: this.workspace,
        cron_history: cronHistory ?? [],
      };
      const lines = [JSON.stringify(header), ...events.map((event) => JSON.stringify(event))];
      await writeFile(path, `${lines.join('\n')}\n`, 'utf8');
      await syncFile(path);
      this.seqCache.set(sessionId, events[events.length - 1]?.seq ?? 0);
      this.messageCountCache.set(sessionId, projectSessionEvents(events).length);
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
      this.seqCache.set(sessionId, events[events.length - 1]?.seq ?? 0);
      this.messageCountCache.set(sessionId, projectSessionEvents(events).length);
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
          pinned: sessionFlag(data.events, 'session_pinned', 'pinned'),
          archived: sessionFlag(data.events, 'session_archived', 'archived'),
          parentSessionId: data.parent_session_id,
          forkedAt: data.forked_at,
          forkBoundarySeq: data.fork_boundary_seq,
          agentId: sessionAgentId(data.events),
          workspace: data.workspace,
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
    this.assertWorkspace(data);
    const messages = projectSessionEvents(data.events);
    this.seqCache.set(data.session_id || sessionId, data.events[data.events.length - 1]?.seq ?? 0);
    this.messageCountCache.set(data.session_id || sessionId, messages.length);
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
      workspace: data.workspace,
    };
  }

  private assertWorkspace(data: SessionFile): void {
    if (!data.workspace) return; // legacy sessions without a workspace stay resumable
    const expected = resolve(data.workspace);
    if (
      process.platform === 'win32'
        ? expected.toLowerCase() !== this.workspace.toLowerCase()
        : expected !== this.workspace
    ) {
      throw new Error(
        `会话属于其他工作区：${expected}\n当前工作区：${this.workspace}\n请在对应目录重新启动 Alex 后 resume`,
      );
    }
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
        workspace: header.workspace,
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

function sessionFlag(
  events: SessionEventRecord[],
  type: string,
  field: string,
): boolean | undefined {
  let value: boolean | undefined;
  for (const event of events) {
    if (event.type !== type) continue;
    const next = event.data?.[field];
    if (typeof next === 'boolean') value = next;
  }
  return value;
}

function renderSessionMarkdown(
  title: string,
  sessionId: string,
  createdAt: string,
  messages: MessageDTO[],
): string {
  const lines = [
    `# ${title}`,
    '',
    `- session: ${sessionId}`,
    `- created: ${createdAt}`,
    `- messages: ${messages.length}`,
    '',
    '---',
    '',
  ];
  for (const message of messages) {
    const role = message.role === 'assistant' ? 'Assistant' : message.role === 'tool' ? 'Tool' : 'User';
    lines.push(`## ${role}`, '');
    const content =
      typeof message.content === 'string'
        ? message.content
        : JSON.stringify(message.content, null, 2);
    lines.push(content || '(empty)', '');
  }
  return `${lines.join('\n').trimEnd()}\n`;
}

function nextMessageCount(count: number, event: SessionEventRecord): number {
  if (
    event.type === 'user_message' ||
    event.type === 'assistant_message' ||
    event.type === 'tool_message'
  ) {
    return count + 1;
  }
  if (event.type === 'replace_messages') {
    return Array.isArray(event.data?.messages) ? event.data.messages.length : 0;
  }
  if (event.type === 'clear_messages') return 0;
  return count;
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