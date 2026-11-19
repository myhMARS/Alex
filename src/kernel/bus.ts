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

export type EventHandler<E extends Event = Event> = (event: E) => void | Promise<void>;
export type RequestHandler<R extends Request<unknown>, T = unknown> = (
  req: R,
) => T | Promise<T>;

export interface MessageBusOptions {
  /** Called when an event handler throws; defaults to console.error. */
  onHandlerError?: (event: Event, error: unknown) => void;
}

interface Subscription {
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
  }

  /** Wait until the dispatch queue is empty (useful for tests and shutdown). */
  async idle(): Promise<void> {
    for (;;) {
      if (!this.scheduled && this.queue.length === 0) {
        const drain = this.drainPromise;
        if (drain) await drain.catch(() => undefined);
        if (!this.scheduled && this.queue.length === 0) return;
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

  async subscribe<E extends Event>(type: EventClass<E>, handler: EventHandler<E>): Promise<void> {
    this.assertClass(type, 'subscribe');
    this.subscriptions.push({
      type,
      handler: handler as EventHandler,
    });
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
    if (this.scheduled) return;
    this.scheduled = true;
    this.drainPromise = Promise.resolve().then(() => this.drain());
  }

  private async drain(): Promise<void> {
    this.scheduled = false;
    try {
      while (this.running) {
        const event = this.queue.shift();
        if (!event) break;
        await this.dispatch(event);
      }
    } finally {
      if (this.running && this.queue.length > 0) this.schedule();
    }
  }

  private async dispatch(event: Event): Promise<void> {
    const sid = event.sessionId;
    if (!sid) {
      await this.dispatchOne(event);
      return;
    }
    const previous = this.sessionTail.get(sid) ?? Promise.resolve();
    const run = previous.then(() => this.dispatchOne(event));
    const tail = run.then(
      () => undefined,
      () => undefined,
    );
    this.sessionTail.set(sid, tail);
    try {
      await run;
    } finally {
      if (this.sessionTail.get(sid) === tail) this.sessionTail.delete(sid);
    }
  }

  private async dispatchOne(event: Event): Promise<void> {
    const handlers: EventHandler[] = [];
    for (const subscription of this.subscriptions) {
      if (event instanceof subscription.type) handlers.push(subscription.handler);
    }
    for (const handler of handlers) {
      try {
        await handler(event);
      } catch (error) {
        if (this.options.onHandlerError) {
          try {
            this.options.onHandlerError(event, error);
          } catch {
            // never let error reporting break dispatch
          }
        } else {
          console.error(
            `[bus] handler for ${event.constructor.name} failed:`,
            error,
          );
        }
      }
    }
  }

  private assertClass(type: unknown, operation: string): void {
    if (typeof type !== 'function') {
      throw new TypeError(`${operation}() expects a class, got ${typeof type}`);
    }
  }
}