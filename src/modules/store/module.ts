/**
 * Session persistence — TypeScript port of alex/store.
 *
 * File format is intentionally identical to the Python implementation
 * (`~/.alex/sessions/<id>.json`) so an existing workspace keeps working.
 */

import { mkdir, readFile, readdir, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

import type { Module } from '../../kernel/runtime';
import type { AsyncEventBus } from '../../kernel/bus';
import type { MessageDTO } from '../../kernel/dto/message';
import { TurnCompleted } from '../../kernel/contracts/chat';
import { ListSessions, LoadSession, type SessionBundle, type SessionMeta } from '../../kernel/contracts/session';

const SESSION_SUFFIX = '.json';

interface SessionFile {
  session_id: string;
  created_at: string;
  first_message: string;
  messages: MessageDTO[];
  cron_history: Record<string, unknown>[];
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
    bus.provide(ListSessions, () => this.listSessions());
    bus.provide(LoadSession, (req) =>
      this.loadSession(req.targetSessionId || req.sessionId),
    );
  }

  private async onTurnCompleted(event: TurnCompleted): Promise<void> {
    if (!event.sessionId) return;
    await this.saveSession(event.sessionId, event.messages);
  }

  /** Persist a message sequence; returns the written path. */
  async saveSession(
    sessionId: string,
    messages: MessageDTO[],
    cronHistory?: Record<string, unknown>[],
  ): Promise<string> {
    if (!sessionId) throw new Error('sessionId is required');
    await mkdir(this.sessionsDir, { recursive: true });
    const path = this.sessionPath(sessionId);
    const existing = await this.readSessionFile(sessionId);
    const createdAt = existing?.created_at || new Date().toISOString();
    const firstMessage = computeFirstMessage(messages);
    const data: SessionFile = {
      session_id: sessionId,
      created_at: createdAt,
      first_message: firstMessage || existing?.first_message || '',
      messages,
      cron_history: cronHistory ?? existing?.cron_history ?? [],
    };
    const tmp = `${path}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
    try {
      await writeFile(tmp, JSON.stringify(data, null, 2), 'utf8');
      await rename(tmp, path);
    } catch (error) {
      await unlink(tmp).catch(() => undefined);
      throw error;
    }
    return path;
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
      const data = await this.readSessionFile(entry.slice(0, -SESSION_SUFFIX.length));
      if (!data) continue;
      const info = await stat(join(this.sessionsDir, entry)).catch(() => null);
      sessions.push({
        mtimeMs: info?.mtimeMs ?? 0,
        meta: {
          sessionId: data.session_id,
          createdAt: data.created_at,
          updatedAt: info ? new Date(info.mtimeMs).toISOString() : data.created_at,
          firstMessage: (data.first_message || '').slice(0, 80),
          messageCount: data.messages.length,
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
    return {
      sessionId: data.session_id || sessionId,
      createdAt: data.created_at,
      firstMessage: data.first_message,
      messages: data.messages,
      cronHistory: data.cron_history,
    };
  }

  private sessionPath(sessionId: string): string {
    return join(this.sessionsDir, `${sessionId}${SESSION_SUFFIX}`);
  }

  private async readSessionFile(sessionId: string): Promise<SessionFile | null> {
    try {
      const raw = await readFile(this.sessionPath(sessionId), 'utf8');
      const parsed = JSON.parse(raw) as Partial<SessionFile>;
      return {
        session_id: parsed.session_id || sessionId,
        created_at: parsed.created_at || '',
        first_message: parsed.first_message || '',
        messages: Array.isArray(parsed.messages) ? parsed.messages : [],
        cron_history: Array.isArray(parsed.cron_history) ? parsed.cron_history : [],
      };
    } catch (error) {
      if (isNotFound(error) || error instanceof SyntaxError) return null;
      throw error;
    }
  }
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