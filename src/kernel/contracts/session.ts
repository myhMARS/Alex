/** Session contracts — TypeScript port of alex/kernel/contracts/session.py. */

import { Event, Request, type MessageContext } from '../message';
import type { MessageDTO } from '../dto/message';

export interface SessionMeta {
  sessionId: string;
  createdAt: string;
  /** Last write time (ISO) — used for recency sorting. */
  updatedAt?: string;
  firstMessage: string;
  messageCount: number;
  /** Auto-generated or user-provided session title. */
  title?: string;
  /** Pinned sessions sort before unpinned ones. */
  pinned?: boolean;
  /** Archived sessions are hidden from the default /sessions view. */
  archived?: boolean;
  /** Session this one was forked from, when applicable. */
  parentSessionId?: string;
  forkedAt?: string;
  forkBoundarySeq?: number;
  /** Agent that produced this session, derived from turn_started events. */
  agentId?: string;
  /** Absolute working directory this session belongs to. */
  workspace?: string;
}

export interface SessionEventRecord {
  /** Monotonic position in the append-only log. */
  seq: number;
  /** Event kind name (user_message / assistant_message / tool_message / usage / compaction ...). */
  type: string;
  at: string;
  data: Record<string, unknown>;
}

export interface SessionBundle {
  sessionId: string;
  createdAt?: string;
  firstMessage?: string;
  title?: string;
  messages: MessageDTO[];
  cronHistory: Record<string, unknown>[];
  /** Append-only sidecar events (v2 format). */
  events?: SessionEventRecord[];
  formatVersion?: number;
  parentSessionId?: string;
  forkedAt?: string;
  forkBoundarySeq?: number;
  /** Agent that produced this session, derived from turn_started events. */
  agentId?: string;
  /** Absolute working directory this session belongs to. */
  workspace?: string;
}

export class ListSessions extends Request<SessionMeta[]> {}

export interface SessionMetaPatch {
  title?: string;
  pinned?: boolean;
  archived?: boolean;
}

export class UpdateSessionMeta extends Request<boolean> {
  targetSessionId: string;
  patch: SessionMetaPatch;

  constructor(
    init: { targetSessionId?: string; sessionId?: string; patch?: SessionMetaPatch } = {},
  ) {
    super(init);
    this.targetSessionId = init.targetSessionId ?? init.sessionId ?? '';
    this.patch = init.patch ?? {};
  }
}

export class RemoveSession extends Request<boolean> {
  targetSessionId: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId ?? init.sessionId ?? '';
  }
}

export interface SessionExportResult {
  path: string;
  markdown: string;
  messageCount: number;
}

export class ExportSession extends Request<SessionExportResult | null> {
  targetSessionId: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId ?? init.sessionId ?? '';
  }
}

export class RegenerateSessionTitle extends Request<{ title: string } | null> {
  targetSessionId: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId ?? init.sessionId ?? '';
  }
}

/** Append typed events to the session log. */
export class AppendSessionEvents extends Request<{ appended: number }> {
  events: SessionEventRecord[];

  constructor(
    init: { events?: SessionEventRecord[]; sessionId?: string } = {},
  ) {
    super(init);
    this.events = init.events ?? [];
  }
}

export class ReplaceSessionEvents extends Request<{ replaced: number }> {
  events: SessionEventRecord[];

  constructor(
    init: { events?: SessionEventRecord[]; sessionId?: string } = {},
  ) {
    super(init);
    this.events = init.events ?? [];
  }
}

export interface SessionTurnRecord {
  turnId: string;
  userInput: string;
  kind: string;
  at: string;
  attachments?: unknown[];
}

export interface SessionProjection {
  messages: MessageDTO[];
  /** Original messages before any `replace_messages` compaction. */
  rawMessages: MessageDTO[];
  events: SessionEventRecord[];
  turns: SessionTurnRecord[];
  /** Effective request header of the latest model request. */
  lastRequestHeader?: Record<string, unknown>;
  /** Header event seq for each projected message (null when absent). */
  messageHeaderSeqs: Array<number | null>;
  /** Plugin/custom event records that are not part of the core schema. */
  customEvents: SessionEventRecord[];
  /** Latest session title, when one has been generated. */
  title?: string;
}

export class SessionTitleUpdated extends Event {
  title: string;

  constructor(init: MessageContext & { title?: string } = {}) {
    super(init);
    this.title = init.title ?? '';
  }
}

export class GetSessionProjection extends Request<SessionProjection> {}

export class SessionEventAppended extends Event {
  events: SessionEventRecord[];

  constructor(
    init: { events?: SessionEventRecord[]; sessionId?: string } = {},
  ) {
    super(init);
    this.events = init.events ?? [];
  }
}

export interface RepairResult {
  repaired: boolean;
  validEvents: number;
  droppedEvents: number;
}

export class CheckpointSession extends Request<{
  checkpointed: boolean;
  bytes: number;
}> {
  targetSessionId: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId ?? init.sessionId ?? '';
  }
}

export class RepairSession extends Request<RepairResult | null> {
  targetSessionId: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId ?? init.sessionId ?? '';
  }
}

export class ForkSession extends Request<SessionBundle | null> {
  sourceSessionId: string;
  targetSessionId: string;
  /** Inclusive event seq to fork at; omitted copies the full log. */
  boundarySeq?: number;

  constructor(
    init: {
      sourceSessionId?: string;
      targetSessionId?: string;
      boundarySeq?: number;
      sessionId?: string;
    } = {},
  ) {
    super(init);
    this.sourceSessionId = init.sourceSessionId ?? '';
    this.targetSessionId = init.targetSessionId ?? '';
    this.boundarySeq = init.boundarySeq;
  }
}

export class LoadSession extends Request<SessionBundle | null> {
  /** Explicit target; falls back to the request's `sessionId`. */
  targetSessionId?: string;

  constructor(init: { targetSessionId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetSessionId = init.targetSessionId;
  }
}