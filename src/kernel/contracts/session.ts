/** Session contracts — TypeScript port of alex/kernel/contracts/session.py. */

import { Request } from '../message';
import type { MessageDTO } from '../dto/message';

export interface SessionMeta {
  sessionId: string;
  createdAt: string;
  /** Last write time (ISO) — used for recency sorting. */
  updatedAt?: string;
  firstMessage: string;
  messageCount: number;
}

export interface SessionBundle {
  sessionId: string;
  createdAt?: string;
  firstMessage?: string;
  messages: MessageDTO[];
  cronHistory: Record<string, unknown>[];
}

export class ListSessions extends Request<SessionMeta[]> {}

export class LoadSession extends Request<SessionBundle | null> {
  /** Explicit target; falls back to the request's `sessionId`. */
  targetSessionId?: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId;
  }
}