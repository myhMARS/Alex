/** Bus errors — mirror alex/kernel/errors.py. */

export class BusError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

export class CapabilityUnavailable extends BusError {
  readonly requestType: string;

  constructor(requestType: string) {
    super(`No handler registered for ${requestType}`);
    this.requestType = requestType;
  }
}

export class CapabilityTimeout extends BusError {
  readonly requestType: string;
  readonly timeoutMs: number;
  readonly correlationId: string;

  constructor(requestType: string, timeoutMs: number, correlationId: string) {
    super(`Request ${requestType} timed out after ${timeoutMs}ms (cid=${correlationId})`);
    this.requestType = requestType;
    this.timeoutMs = timeoutMs;
    this.correlationId = correlationId;
  }
}

export class HandlerError extends BusError {
  readonly requestType: string;
  override readonly cause: unknown;

  constructor(requestType: string, cause: unknown) {
    const detail = cause instanceof Error ? `${cause.name}: ${cause.message}` : String(cause);
    super(`Handler for ${requestType} failed: ${detail}`);
    this.requestType = requestType;
    this.cause = cause;
  }
}