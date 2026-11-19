/**
 * Base message types — the TypeScript port of alex/kernel/bus.py.
 *
 * Three semantics:
 *  - `Event`   broadcast, fire-and-forget
 *  - `Command` point-to-point action request (subclass of Event, so a command
 *              can also be observed by other modules)
 *  - `Request` request/reply with a correlated result
 *
 * Class identity (not string tags) is what the bus matches on, mirroring the
 * Python implementation's `isinstance` dispatch.
 */

export interface MessageContext {
  sessionId?: string;
  turnId?: string;
  source?: string;
  traceId?: string;
}

let idCounter = 0;

function nextId(): string {
  idCounter += 1;
  return `${idCounter.toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export abstract class Message {
  readonly eventId: string;
  readonly ts: number;
  sessionId: string;
  turnId: string;
  source: string;
  traceId: string;

  constructor(ctx: MessageContext = {}) {
    this.eventId = nextId();
    this.ts = Date.now() / 1000;
    this.sessionId = ctx.sessionId ?? '';
    this.turnId = ctx.turnId ?? '';
    this.source = ctx.source ?? '';
    this.traceId = ctx.traceId ?? '';
  }
}

/** Broadcast state change. */
export abstract class Event extends Message {}

/** Action request that a single module handles. */
export abstract class Command extends Event {}

export interface RequestContext {
  sessionId?: string;
  turnId?: string;
  traceId?: string;
}

/** Point-to-point call with a returned value. */
export abstract class Request<TResult = unknown> {
  sessionId: string;
  turnId: string;
  traceId: string;
  /** Set by the bus right before dispatch. */
  _correlationId = '';

  /** Phantom field: carries the result type for `bus.request<T>()`. */
  declare readonly __result?: TResult;

  constructor(ctx: RequestContext = {}) {
    this.sessionId = ctx.sessionId ?? '';
    this.turnId = ctx.turnId ?? '';
    this.traceId = ctx.traceId ?? '';
  }
}

/** Short correlation id for request/reply pairing. */
export function correlationId(): string {
  return Math.random().toString(36).slice(2, 14);
}