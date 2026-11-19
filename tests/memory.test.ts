import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import { BufferMemory, MemoryModule } from '../src/modules/memory/module';
import {
  AppendMessages,
  ClearMemory,
  GetContext,
  GetMemoryStatus,
  ReplaceMemory,
} from '../src/kernel/contracts/memory';
import { userMessage } from '../src/kernel/dto/message';

describe('BufferMemory', () => {
  test('keeps a sliding window', () => {
    const memory = new BufferMemory({ maxMessages: 2 });
    memory.append('s1', [userMessage('one')]);
    memory.append('s1', [userMessage('two'), userMessage('three')]);

    expect(memory.get('s1').map((message) => message.content)).toEqual([
      'two',
      'three',
    ]);
  });

  test('replace and clear', () => {
    const memory = new BufferMemory();
    memory.append('s1', [userMessage('one')]);
    memory.replace('s1', [userMessage('replaced')]);
    expect(memory.get('s1').map((message) => message.content)).toEqual(['replaced']);

    memory.clear('s1');
    expect(memory.get('s1')).toEqual([]);
  });

  test('trims by approximate token budget', () => {
    const memory = new BufferMemory({ maxMessages: 40, maxTokens: 18 });
    memory.append('s1', [userMessage('aaaa')]);
    memory.append('s1', [userMessage('bbbb'), userMessage('cccc')]);
    // Each 'aaaa' message prices at 9 tokens; the newest two fit in 18 tokens.
    expect(memory.get('s1').map((message) => message.content)).toEqual(['bbbb', 'cccc']);
    expect(memory.estimatedTokens('s1')).toBe(18);
  });

  test('isolates sessions', () => {
    const memory = new BufferMemory();
    memory.append('a', [userMessage('a1')]);
    memory.append('b', [userMessage('b1')]);
    expect(memory.get('a')[0]?.content).toBe('a1');
    expect(memory.get('b')[0]?.content).toBe('b1');
  });
});

describe('MemoryModule over the bus', () => {
  test('append / get / replace / clear', async () => {
    const bus = new AsyncEventBus();
    const module = new MemoryModule(new BufferMemory({ maxMessages: 3 }));
    await module.start(bus);
    await bus.start();

    await bus.request(new AppendMessages({ sessionId: 's1', messages: [userMessage('hi')] }));
    await bus.request(
      new AppendMessages({ sessionId: 's1', messages: [userMessage('again')] }),
    );

    const context = await bus.request(new GetContext({ sessionId: 's1' }));
    expect(context.map((message) => message.content)).toEqual(['hi', 'again']);

    const status = await bus.request(new GetMemoryStatus({ sessionId: 's1' }));
    expect(status).toEqual({ messageCount: 2, maxTokens: 0, estimatedTokens: 19, maxContextTokens: 0 });

    await bus.request(new ReplaceMemory({ sessionId: 's1', messages: [userMessage('reset')] }));
    expect((await bus.request(new GetContext({ sessionId: 's1' })))[0]?.content).toBe('reset');

    await bus.request(new ClearMemory({ sessionId: 's1' }));
    expect(await bus.request(new GetContext({ sessionId: 's1' }))).toEqual([]);

    await bus.shutdown();
  });
});