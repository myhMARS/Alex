/** In-process async message bus — TypeScript port of alex/bus/in_memory.py. */

import { Event, Request, correlationId } from './message';
import {
  BusError,
  CapabilityTimeout,
  CapabilityUnavailable,
  HandlerError,
} from './errors';

export type EventClass<E extends Event = Event> = abstract new (...args: never[]) => E;
export type RequestClass<R extends Request<unknown> = Request<unknown>> = abstract new (
  ...args: never[]
) => R;

export type EventHandler<E extends Event = Event> = (
  event: E,
  ...args: any[]
) => unknown | Promise<unknown>;
export type RequestHandler<R extends Request<unknown>, T = unknown> = (
  req: R,
) => T | Promise<T>;

function isBailed(value: unknown): boolean {
  return value !== null && value !== false && value !== undefined;
}

export interface MessageBusOptions {
  /** Called when an event handler throws; defaults to console.error. */
  onHandlerError?: (event: Event, error: unknown) => void;
}

export interface SubscriptionOptions {
  /** Only receive events emitted by this agent. */
  agentId?: string;
  /** Only receive events for this session. */
  sessionId?: string;
  /** Only receive events for this team. */
  teamId?: string;
  /**
   * Global observers (no agentId) receive targeted events so infrastructure
   * such as TUI/store can audit them. Set false to ignore targeted traffic.
   */
  observeTargeted?: boolean;
}

interface Subscription extends SubscriptionOptions {
  type: Function;
  handler: EventHandler;
}

/**
 * In-process publish/subscribe + request/reply bus.
 *
 * Delivery guarantees mirror the Python implementation:
 *  - events published before `start()` are buffered and drained on start
 *  - one serial dispatch loop, so handlers run in publish order
 *  - events with a `sessionId` are additionally serialized per session
 *  - a failing handler never breaks the bus or other handlers
 *  - request handlers are single-provider and correlated by id
 */
export class AsyncEventBus {
  private queue: Event[] = [];
  private preStart: Event[] = [];
  private readonly subscriptions: Subscription[] = [];
  private readonly providers = new Map<Function, (req: Request<unknown>) => unknown>();
  private readonly sessionTail = new Map<string, Promise<void>>();
  private readonly options: MessageBusOptions;

  private running = false;
  private scheduled = false;
  private drainPromise: Promise<void> | null = null;
  private draining = false;

  constructor(options: MessageBusOptions = {}) {
    this.options = options;
  }

  // ── lifecycle ────────────────────────────────────────────────────────────

  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    if (this.preStart.length > 0) {
      this.queue.push(...this.preStart);
      this.preStart = [];
    }
    this.schedule();
  }

  async shutdown(): Promise<void> {
    this.running = false;
    this.preStart = [];
    this.queue = [];
    this.providers.clear();
    const drain = this.drainPromise;
    if (drain) await drain.catch(() => undefined);
    this.subscriptions.length = 0;
    this.sessionTail.clear();
    this.scheduled = false;
    this.drainPromise = null;
    this.draining = false;
  }

  /** Wait until the dispatch queue is empty (useful for tests and shutdown). */
  async idle(): Promise<void> {
    for (;;) {
      if (!this.scheduled && this.queue.length === 0) {
        const drain = this.drainPromise;
        if (drain) await drain.catch(() => undefined);
        if (!this.scheduled && !this.draining && this.queue.length === 0) return;
        continue;
      }
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
      const drain = this.drainPromise;
      if (drain) await drain.catch(() => undefined);
    }
  }

  get isRunning(): boolean {
    return this.running;
  }

  // ── event plane ──────────────────────────────────────────────────────────

  publish(event: Event): void {
    if (!(event instanceof Event)) {
      throw new TypeError('publish() expects an Event instance');
    }
    if (!this.running) {
      this.preStart.push(event);
      return;
    }
    this.queue.push(event);
    this.schedule();
  }

  async subscribe<E extends Event>(
    type: EventClass<E>,
    handler: EventHandler<E>,
    options: SubscriptionOptions = {},
  ): Promise<() => Promise<void>> {
    this.assertClass(type, 'subscribe');
    const subscription: Subscription = {
      type,
      handler: handler as EventHandler,
      ...options,
    };
    this.subscriptions.push(subscription);
    return async () => {
      const index = this.subscriptions.indexOf(subscription);
      if (index >= 0) this.subscriptions.splice(index, 1);
    };
  }

  async unsubscribe<E extends Event>(
    type: EventClass<E>,
    handler: EventHandler<E>,
  ): Promise<void> {
    const index = this.subscriptions.findIndex(
      (s) => s.type === type && s.handler === handler,
    );
    if (index >= 0) this.subscriptions.splice(index, 1);
  }

  /** Dispatch listeners concurrently and wait for all of them. */
  async parallel(event: Event): Promise<void> {
    this.assertEvent(event);
    const handlers = this.handlersFor(event);
    const results = await Promise.allSettled(
      handlers.map((handler) => Promise.resolve().then(() => handler(event))),
    );
    const errors = results.filter(
      (result): result is PromiseRejectedResult => result.status === 'rejected',
    );
    if (errors.length > 0) {
      throw new AggregateError(errors.map((error) => error.reason));
    }
  }

  /**
   * Dispatch listeners in subscription order, awaiting each one, and stop at
   * the first bail value (non-null, non-false, non-undefined).
   */
  async serial(event: Event): Promise<unknown> {
    this.assertEvent(event);
    for (const handler of this.handlersFor(event)) {
      try {
        const value = await handler(event);
        if (isBailed(value)) return value;
      } catch (error) {
        this.reportHandlerError(event, error);
      }
    }
    return undefined;
  }

  /**
   * Compose listeners as middleware around a final `next`. A listener that
   * does not call `next()` vetoes the remaining chain.
   */
  async waterfall(event: Event): Promise<unknown> {
    this.assertEvent(event);
    const handlers = this.handlersFor(event);
    const dispatch = async (index: number): Promise<unknown> => {
      const handler = handlers[index] as
        | ((event: Event, next: () => Promise<unknown>) => unknown)
        | undefined;
      if (!handler) return undefined;
      let called = false;
      const next = async (): Promise<unknown> => {
        if (called) throw new Error('next() called multiple times');
        called = true;
        return dispatch(index + 1);
      };
      return await handler(event, next);
    };
    return dispatch(0);
  }

  // ── request plane ────────────────────────────────────────────────────────

  provide<R extends Request<unknown>, T>(
    type: RequestClass<R>,
    handler: RequestHandler<R, T>,
  ): void {
    this.assertClass(type, 'provide');
    this.providers.set(type, handler as (req: Request<unknown>) => unknown);
  }

  async request<R extends Request<unknown>, T = R extends Request<infer U> ? U : unknown>(
    req: R,
    timeoutMs = 30_000,
  ): Promise<T> {
    if (!(req instanceof Request)) {
      throw new TypeError('request() expects a Request instance');
    }
    const typeName = req.constructor.name;
    const handler = this.providers.get(req.constructor);
    if (!handler) throw new CapabilityUnavailable(typeName);

    const cid = correlationId();
    req._correlationId = cid;

    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(
        () => reject(new CapabilityTimeout(typeName, timeoutMs, cid)),
        timeoutMs,
      );
    });

    try {
      const result = await Promise.race([
        Promise.resolve().then(() => handler(req)),
        timeout,
      ]);
      return result as T;
    } catch (error) {
      if (error instanceof BusError) throw error;
      throw new HandlerError(typeName, error);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  }

  // ── dispatch ─────────────────────────────────────────────────────────────

  private schedule(): void {
    if (this.scheduled || this.draining) return;
    this.scheduled = true;
    this.drainPromise = Promise.resolve().then(() => this.drain());
  }

  private async drain(): Promise<void> {
    this.draining = true;
    this.scheduled = false;
    try {
      while (this.running) {
        const event = this.queue.shift();
        if (!event) break;
        await this.dispatch(event);
      }
    } finally {
      this.draining = false;
      if (this.running && this.queue.length > 0) this.schedule();
    }
  }

  private async dispatch(event: Event): Promise<void> {
    const key = this.serialKey(event);
    if (!key) {
      await this.dispatchOne(event);
      return;
    }
    const previous = this.sessionTail.get(key) ?? Promise.resolve();
    const run = previous.then(() => this.dispatchOne(event));
    const tail = run.then(
      () => undefined,
      () => undefined,
    );
    this.sessionTail.set(key, tail);
    try {
      await run;
    } finally {
      if (this.sessionTail.get(key) === tail) this.sessionTail.delete(key);
    }
  }

  private async dispatchOne(event: Event): Promise<void> {
    for (const handler of this.handlersFor(event)) {
      try {
        await handler(event);
      } catch (error) {
        this.reportHandlerError(event, error);
      }
    }
  }

  private handlersFor(event: Event): EventHandler[] {
    const handlers: EventHandler[] = [];
    for (const subscription of this.subscriptions) {
      if (!(event instanceof subscription.type)) continue;
      if (this.matchesSubscription(subscription, event)) {
        handlers.push(subscription.handler);
      }
    }
    return handlers;
  }

  private matchesSubscription(subscription: Subscription, event: Event): boolean {
    if (event.targetAgentId) {
      if (subscription.agentId) {
        return subscription.agentId === event.targetAgentId;
      }
      return subscription.observeTargeted !== false;
    }
    if (subscription.agentId && event.agentId !== subscription.agentId) {
      return false;
    }
    if (subscription.sessionId && event.sessionId !== subscription.sessionId) {
      return false;
    }
    if (subscription.teamId && event.teamId !== subscription.teamId) {
      return false;
    }
    return true;
  }

  private serialKey(event: Event): string {
    if (event.targetAgentId) return `agent:${event.targetAgentId}`;
    if (event.agentId) return `agent:${event.agentId}`;
    return event.sessionId;
  }

  private reportHandlerError(event: Event, error: unknown): void {
    if (this.options.onHandlerError) {
      try {
        this.options.onHandlerError(event, error);
      } catch {
        // never let error reporting break dispatch
      }
      return;
    }
    console.error(`[bus] handler for ${event.constructor.name} failed:`, error);
  }

  private assertEvent(event: Event): void {
    if (!(event instanceof Event)) {
      throw new TypeError('event dispatch expects an Event instance');
    }
  }

  private assertClass(type: unknown, operation: string): void {
    if (typeof type !== 'function') {
      throw new TypeError(`${operation}() expects a class, got ${typeof type}`);
    }
  }
}