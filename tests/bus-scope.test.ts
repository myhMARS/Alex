import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { Event } from '../src/kernel/message';
import {
  AgentTeamMessage,
  AgentTeamTaskRequested,
} from '../src/kernel/contracts/team';

class Ping extends Event {
  label: string;

  constructor(init: { label?: string; agentId?: string; sessionId?: string; teamId?: string; targetAgentId?: string } = {}) {
    super(init);
    this.label = init.label ?? '';
  }
}

describe('agent-scoped event routing', () => {
  test('scoped subscriptions only receive their own agent events', async () => {
    const bus = new AsyncEventBus();
    const a1: string[] = [];
    const a2: string[] = [];
    await bus.subscribe(Ping, (event) => { a1.push(event.label); }, { agentId: 'a1' });
    await bus.subscribe(Ping, (event) => { a2.push(event.label); }, { agentId: 'a2' });
    await bus.start();

    bus.publish(new Ping({ label: 'one', agentId: 'a1' }));
    bus.publish(new Ping({ label: 'two', agentId: 'a2' }));
    await bus.idle();

    expect(a1).toEqual(['one']);
    expect(a2).toEqual(['two']);
    await bus.shutdown();
  });

  test('targeted events reach the target and global observers only', async () => {
    const bus = new AsyncEventBus();
    const a1: string[] = [];
    const a2: string[] = [];
    const global: string[] = [];
    await bus.subscribe(Ping, (event) => { a1.push(event.label); }, { agentId: 'a1' });
    await bus.subscribe(Ping, (event) => { a2.push(event.label); }, { agentId: 'a2' });
    await bus.subscribe(Ping, (event) => { global.push(event.label); });
    await bus.start();

    bus.publish(new Ping({ label: 'targeted', agentId: 'lead', targetAgentId: 'a1' }));
    await bus.idle();

    expect(a1).toEqual(['targeted']);
    expect(a2).toEqual([]);
    expect(global).toEqual(['targeted']);
    await bus.shutdown();
  });

  test('agent team messages carry point-to-point targets', async () => {
    const bus = new AsyncEventBus();
    const worker: string[] = [];
    const other: string[] = [];
    await bus.subscribe(
      AgentTeamMessage,
      (event) => { worker.push(`${event.fromAgentId}:${event.content}`); },
      { agentId: 'worker' },
    );
    await bus.subscribe(
      AgentTeamMessage,
      (event) => { other.push(event.content); },
      { agentId: 'other' },
    );
    await bus.start();

    bus.publish(
      new AgentTeamMessage({
        teamId: 'team-1',
        fromAgentId: 'lead',
        toAgentId: 'worker',
        content: 'hello worker',
      }),
    );
    await bus.idle();

    expect(worker).toEqual(['lead:hello worker']);
    expect(other).toEqual([]);
    await bus.shutdown();
  });

  test('task events are routed to the assignee', async () => {
    const bus = new AsyncEventBus();
    const tasks: string[] = [];
    await bus.subscribe(
      AgentTeamTaskRequested,
      (event) => { tasks.push(event.prompt); },
      { agentId: 'worker' },
    );
    await bus.start();
    bus.publish(
      new AgentTeamTaskRequested({
        assigneeAgentId: 'worker',
        prompt: 'do the thing',
      }),
    );
    await bus.idle();
    expect(tasks).toEqual(['do the thing']);
    await bus.shutdown();
  });

  test('per-agent dispatch is serialized', async () => {
    const bus = new AsyncEventBus();
    const order: string[] = [];
    await bus.subscribe(
      Ping,
      async (event) => {
        order.push(`start:${event.label}`);
        await new Promise((resolve) => setTimeout(resolve, 5));
        order.push(`end:${event.label}`);
      },
      { agentId: 'a1' },
    );
    await bus.start();
    bus.publish(new Ping({ label: '1', agentId: 'a1' }));
    bus.publish(new Ping({ label: '2', agentId: 'a1' }));
    await bus.idle();
    expect(order).toEqual(['start:1', 'end:1', 'start:2', 'end:2']);
    await bus.shutdown();
  });
});

describe('event dispatch modes', () => {
  test('serial awaits in order and stops at the first bail value', async () => {
    const bus = new AsyncEventBus();
    const order: string[] = [];
    await bus.subscribe(Ping, async (event) => {
      order.push(`first:${event.label}`);
      return false;
    });
    await bus.subscribe(Ping, async (event) => {
      order.push(`second:${event.label}`);
      return { stopped: true };
    });
    await bus.subscribe(Ping, async (event) => {
      order.push(`third:${event.label}`);
      return false;
    });
    await bus.start();

    const result = await bus.serial(new Ping({ label: 'x' }));
    expect(result).toEqual({ stopped: true });
    expect(order).toEqual(['first:x', 'second:x']);
    await bus.shutdown();
  });

  test('waterfall composes middleware and can veto with next', async () => {
    const bus = new AsyncEventBus();
    const order: string[] = [];
    await bus.subscribe(Ping, async (event, next) => {
      order.push('outer-before');
      const value = await next();
      order.push('outer-after');
      return value;
    });
    await bus.subscribe(Ping, async (event, next) => {
      order.push('inner');
      return next();
    });
    await bus.start();

    await bus.waterfall(new Ping({ label: 'x' }));
    expect(order).toEqual(['outer-before', 'inner', 'outer-after']);

    const vetoBus = new AsyncEventBus();
    let reached = false;
    await vetoBus.subscribe(Ping, async () => {
      order.push('veto');
      return 'vetoed';
    });
    await vetoBus.subscribe(Ping, async () => {
      reached = true;
    });
    await vetoBus.start();
    expect(await vetoBus.waterfall(new Ping({ label: 'y' }))).toBe('vetoed');
    expect(reached).toBe(false);
    await vetoBus.shutdown();
    await bus.shutdown();
  });

  test('parallel runs listeners concurrently and collects failures', async () => {
    const bus = new AsyncEventBus();
    let active = 0;
    let maxActive = 0;
    await bus.subscribe(Ping, async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
    });
    await bus.subscribe(Ping, async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
    });
    await bus.start();
    await bus.parallel(new Ping({ label: 'p' }));
    expect(maxActive).toBe(2);
    await bus.shutdown();
  });
});