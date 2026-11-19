import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import { GetToolCatalog } from '../src/kernel/contracts/tools';
import {
  AgentTeamTaskCompleted,
  AgentTeamTaskRequested,
} from '../src/kernel/contracts/team';
import {
  TokenEmitted,
  TurnCompleted,
  TurnStarted,
  UserTurnRequested,
} from '../src/kernel/contracts/chat';

describe('agent team event foundation', () => {
  test('two agents only process commands addressed to them', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const session = new SessionModule();
    const a1 = new AgentModule({ agentId: 'a1', parentAgentId: 'lead', teamId: 't1' });
    const a2 = new AgentModule({ agentId: 'a2', parentAgentId: 'lead', teamId: 't1' });
    await session.start(bus);
    await a1.start(bus);
    await a2.start(bus);
    await bus.start();

    const a1Tokens: string[] = [];
    const a2Tokens: string[] = [];
    const a1Starts: TurnStarted[] = [];
    const completed: TurnCompleted[] = [];
    await bus.subscribe(TokenEmitted, (event) => { a1Tokens.push(event.delta); }, { agentId: 'a1' });
    await bus.subscribe(TokenEmitted, (event) => { a2Tokens.push(event.delta); }, { agentId: 'a2' });
    await bus.subscribe(TurnStarted, (event) => { a1Starts.push(event); }, { agentId: 'a1' });
    await bus.subscribe(TurnCompleted, (event) => { completed.push(event); }, { teamId: 't1' });

    bus.publish(
      new UserTurnRequested({
        sessionId: 's1',
        userText: 'hello a1',
        agentId: 'lead',
        targetAgentId: 'a1',
      }),
    );
    await a1.whenIdle();
    await a2.whenIdle();
    await bus.idle();

    expect(a1Tokens.join('')).toBe('Echo: hello a1');
    expect(a2Tokens).toEqual([]);
    expect(a1Starts).toHaveLength(1);
    expect(a1Starts[0]?.agentId).toBe('a1');
    expect(a1Starts[0]?.parentAgentId).toBe('lead');
    expect(a1Starts[0]?.teamId).toBe('t1');
    expect(completed).toHaveLength(1);
    expect(completed[0]?.agentId).toBe('a1');
    await bus.shutdown();
  });

  test('runs AgentTeamTaskRequested and publishes AgentTeamTaskCompleted', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const session = new SessionModule();
    const worker = new AgentModule({ agentId: 'worker' });
    await session.start(bus);
    await worker.start(bus);
    await bus.start();

    const completed: AgentTeamTaskCompleted[] = [];
    await bus.subscribe(AgentTeamTaskCompleted, (event) => {
      completed.push(event);
    });

    bus.publish(
      new AgentTeamTaskRequested({
        taskId: 'team-task-1',
        assigneeAgentId: 'worker',
        prompt: 'do team work',
        sessionId: 's-team',
        agentId: 'lead',
      }),
    );
    await worker.whenIdle();
    await bus.idle();

    expect(completed).toHaveLength(1);
    expect(completed[0]?.taskId).toBe('team-task-1');
    expect(completed[0]?.agentId).toBe('worker');
    expect(completed[0]?.result).toBe('Echo: do team work');
    await bus.shutdown();
  });

  test('queues turns per session instead of running them concurrently', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const session = new SessionModule();
    let active = 0;
    let maxActive = 0;
    const order: string[] = [];
    const worker = new AgentModule({
      agentId: 'worker',
      responder: {
        async *respond(params: { text: string }) {
          active += 1;
          maxActive = Math.max(maxActive, active);
          order.push(params.text);
          await new Promise((resolve) => setTimeout(resolve, 20));
          active -= 1;
          yield `done: ${params.text}`;
        },
      },
    });
    await session.start(bus);
    await worker.start(bus);
    await bus.start();
    bus.publish(
      new UserTurnRequested({
        sessionId: 'same-session',
        userText: 'first',
        targetAgentId: 'worker',
      }),
    );
    bus.publish(
      new UserTurnRequested({
        sessionId: 'same-session',
        userText: 'second',
        targetAgentId: 'worker',
      }),
    );
    await worker.whenIdle();
    await bus.idle();
    expect(order).toEqual(['first', 'second']);
    expect(maxActive).toBe(1);
    await bus.shutdown();
  });

  test('scoped subscriptions isolate streaming events by agent', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const session = new SessionModule();
    const a1 = new AgentModule({ agentId: 'a1' });
    const a2 = new AgentModule({ agentId: 'a2' });
    await session.start(bus);
    await a1.start(bus);
    await a2.start(bus);
    await bus.start();

    const received: Array<{ agent: string; delta: string }> = [];
    await bus.subscribe(TokenEmitted, (event) => {
      received.push({ agent: 'a1', delta: event.delta });
    }, { agentId: 'a1' });
    await bus.subscribe(TokenEmitted, (event) => {
      received.push({ agent: 'a2', delta: event.delta });
    }, { agentId: 'a2' });

    bus.publish(
      new UserTurnRequested({ sessionId: 's1', userText: 'a1', targetAgentId: 'a1' }),
    );
    bus.publish(
      new UserTurnRequested({ sessionId: 's2', userText: 'a2', targetAgentId: 'a2' }),
    );
    await a1.whenIdle();
    await a2.whenIdle();
    await bus.idle();

    expect(received.filter((item) => item.agent === 'a1').map((item) => item.delta).join('')).toBe(
      'Echo: a1',
    );
    expect(received.filter((item) => item.agent === 'a2').map((item) => item.delta).join('')).toBe(
      'Echo: a2',
    );
    await bus.shutdown();
  });
});