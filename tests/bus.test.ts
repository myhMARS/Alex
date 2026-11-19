import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { Command, Event, Request } from '../src/kernel/message';
import {
  CapabilityTimeout,
  CapabilityUnavailable,
  HandlerError,
} from '../src/kernel/errors';

class Ping extends Event {
  readonly value: number;
  constructor(value = 0) {
    super();
    this.value = value;
  }
}

class Run extends Command {
  readonly text: string;
  constructor(text = '') {
    super();
    this.text = text;
  }
}

class Echo extends Request<string> {
  readonly value: string;
  constructor(value = '') {
    super();
    this.value = value;
  }
}

class Missing extends Request<string> {}

class Boom extends Request<string> {}

class Slow extends Request<string> {}

describe('AsyncEventBus', () => {
  test('buffers events published before start and drains them on start', async () => {
    const bus = new AsyncEventBus();
    const seen: number[] = [];
    await bus.subscribe(Ping, (event) => {
      seen.push(event.value);
    });

    bus.publish(new Ping(1));
    expect(seen).toEqual([]);

    await bus.start();
    await bus.idle();
    expect(seen).toEqual([1]);
  });

  test('dispatches subclass events to base-class subscribers', async () => {
    const bus = new AsyncEventBus();
    const seen: string[] = [];
    await bus.subscribe(Event, (event) => {
      seen.push(event.constructor.name);
    });
    await bus.subscribe(Command, (event) => {
      seen.push(`cmd:${event.constructor.name}`);
    });
    await bus.start();

    bus.publish(new Run('go'));
    await bus.idle();

    expect(seen).toContain('Run');
    expect(seen).toContain('cmd:Run');
  });

  test('isolates handler failures from other handlers', async () => {
    const errors: unknown[] = [];
    const bus = new AsyncEventBus({ onHandlerError: (_event, error) => errors.push(error) });
    const seen: number[] = [];

    await bus.subscribe(Ping, () => {
      throw new Error('boom');
    });
    await bus.subscribe(Ping, (event) => {
      seen.push(event.value);
    });
    await bus.start();

    bus.publish(new Ping(7));
    await bus.idle();

    expect(seen).toEqual([7]);
    expect(errors).toHaveLength(1);
  });

  test('serializes events per session', async () => {
    const bus = new AsyncEventBus();
    const order: string[] = [];

    await bus.subscribe(Ping, async (event) => {
      order.push(`start:${event.value}`);
      await new Promise((resolve) => setTimeout(resolve, 5));
      order.push(`end:${event.value}`);
    });
    await bus.start();

    bus.publish(Object.assign(new Ping(1), { sessionId: 's1' }));
    bus.publish(Object.assign(new Ping(2), { sessionId: 's1' }));
    await bus.idle();

    expect(order).toEqual(['start:1', 'end:1', 'start:2', 'end:2']);
  });

  test('request/reply returns the handler result', async () => {
    const bus = new AsyncEventBus();
    bus.provide(Echo, (req) => `echo:${req.value}`);
    await bus.start();

    const result = await bus.request(new Echo('hi'));
    expect(result).toBe('echo:hi');
  });

  test('request raises CapabilityUnavailable without a provider', async () => {
    const bus = new AsyncEventBus();
    await bus.start();
    await expect(bus.request(new Missing())).rejects.toBeInstanceOf(CapabilityUnavailable);
  });

  test('request times out', async () => {
    const bus = new AsyncEventBus();
    bus.provide(Slow, async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
      return 'late';
    });
    await expect(bus.request(new Slow(), 5)).rejects.toBeInstanceOf(CapabilityTimeout);
  });

  test('wraps provider exceptions in HandlerError', async () => {
    const bus = new AsyncEventBus();
    bus.provide(Boom, () => {
      throw new Error('nope');
    });

    let caught: unknown;
    try {
      await bus.request(new Boom());
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(HandlerError);
    expect((caught as HandlerError).cause).toBeInstanceOf(Error);
  });

  test('shutdown stops delivery', async () => {
    const bus = new AsyncEventBus();
    const seen: number[] = [];
    await bus.subscribe(Ping, (event) => {
      seen.push(event.value);
    });
    await bus.start();
    await bus.shutdown();

    bus.publish(new Ping(9));
    await bus.idle();
    expect(seen).toEqual([]);
  });
});