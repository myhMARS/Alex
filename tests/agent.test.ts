import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule, type Responder } from '../src/modules/agent/module';
import { MemoryModule } from '../src/modules/memory/module';
import { UserTurnRequested, TokenEmitted, TurnFailed } from '../src/kernel/contracts/chat';
import { GetContext } from '../src/kernel/contracts/memory';

async function setup(agent: AgentModule): Promise<{ bus: AsyncEventBus }> {
  const bus = new AsyncEventBus();
  const memory = new MemoryModule();
  await memory.start(bus);
  await agent.start(bus);
  await bus.start();
  return { bus };
}

describe('AgentModule', () => {
  test('runs an echo turn and streams tokens', async () => {
    const agent = new AgentModule();
    const { bus } = await setup(agent);
    const tokens: string[] = [];
    await bus.subscribe(TokenEmitted, (event) => {
      tokens.push(event.delta);
    });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'hello' }));
    await agent.whenIdle();
    await bus.idle();

    expect(tokens.join('')).toBe('Echo: hello');
    const history = await bus.request(new GetContext({ sessionId: 's1' }));
    expect(history.map((message) => message.content)).toEqual(['hello', 'Echo: hello']);
    await bus.shutdown();
  });

  test('publishes TurnFailed when the responder fails', async () => {
    const failing: Responder = {
      async *respond() {
        yield 'partial';
        throw new Error('llm exploded');
      },
    };
    const agent = new AgentModule(failing);
    const { bus } = await setup(agent);
    const failures: TurnFailed[] = [];
    await bus.subscribe(TurnFailed, (event) => {
      failures.push(event);
    });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'boom' }));
    await agent.whenIdle();
    await bus.idle();

    expect(failures).toHaveLength(1);
    expect(failures[0]?.error).toContain('llm exploded');
    await bus.shutdown();
  });

  test('serializes turn work without blocking the dispatch loop', async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const slow: Responder = {
      async *respond(input) {
        await gate;
        yield input.text;
      },
    };
    const agent = new AgentModule(slow);
    const { bus } = await setup(agent);

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'slow' }));
    // The dispatch loop must stay responsive while the responder is blocked.
    await bus.idle();
    let observed = false;
    await bus.subscribe(UserTurnRequested, () => {
      observed = true;
    });
    release?.();
    await agent.whenIdle();
    await bus.idle();

    expect(observed).toBe(false); // no second turn was published
    await bus.shutdown();
  });
});