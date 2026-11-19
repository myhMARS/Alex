import { describe, expect, test } from 'bun:test';

import {
  ensureToolCallResponses,
  MISSING_TOOL_RESULT,
} from '../src/modules/agent/tool-history';
import type { MessageDTO } from '../src/kernel/dto/message';

function assistantWithCalls(ids: string[]): MessageDTO {
  return {
    role: 'assistant',
    content: '',
    tool_calls: ids.map((id) => ({
      id,
      type: 'function',
      function: { name: 'spawn_agent', arguments: '{}' },
    })),
  };
}

describe('ensureToolCallResponses', () => {
  test('inserts a synthetic tool result for every missing tool_call_id', () => {
    const messages: MessageDTO[] = [
      { role: 'user', content: 'run' },
      assistantWithCalls(['call-1', 'call-2']),
      { role: 'tool', tool_call_id: 'call-1', content: 'ok' },
      { role: 'user', content: 'next' },
    ];
    const repaired = ensureToolCallResponses(messages);
    expect(repaired.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'tool',
      'user',
    ]);
    const synthetic = repaired.filter(
      (message) => message.content === MISSING_TOOL_RESULT,
    );
    expect(synthetic).toHaveLength(1);
    expect(synthetic[0]?.tool_call_id).toBe('call-2');
    // The following user message still comes after all tool responses.
    expect(repaired[4]?.content).toBe('next');
  });

  test('leaves complete tool-call history unchanged', () => {
    const messages: MessageDTO[] = [
      assistantWithCalls(['call-1']),
      { role: 'tool', tool_call_id: 'call-1', content: 'ok' },
      { role: 'assistant', content: 'done' },
    ];
    const repaired = ensureToolCallResponses(messages);
    expect(repaired).toHaveLength(3);
    expect(repaired[1]?.content).toBe('ok');
  });

  test('ignores assistant messages without tool calls', () => {
    const messages: MessageDTO[] = [
      { role: 'user', content: 'hi' },
      { role: 'assistant', content: 'hello' },
    ];
    expect(ensureToolCallResponses(messages)).toEqual(messages);
  });

  test('repairs back-to-back dangling tool calls', () => {
    const messages: MessageDTO[] = [
      assistantWithCalls(['a']),
      assistantWithCalls(['b']),
      { role: 'user', content: 'next' },
    ];
    const repaired = ensureToolCallResponses(messages);
    expect(repaired.map((message) => message.role)).toEqual([
      'assistant',
      'tool',
      'assistant',
      'tool',
      'user',
    ]);
    expect(repaired[1]?.tool_call_id).toBe('a');
    expect(repaired[3]?.tool_call_id).toBe('b');
  });
});