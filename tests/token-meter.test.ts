import { describe, expect, test } from 'bun:test';

import {
  estimateMessageTokens,
  estimateTextTokens,
  estimateToolsTokens,
} from '../src/session/token-meter';
import { assistantMessage, userMessage } from '../src/kernel/dto/message';

describe('session token meter', () => {
  test('prices narrow and wide text differently', () => {
    expect(estimateTextTokens('abcd')).toBe(1);
    expect(estimateTextTokens('你好')).toBeGreaterThanOrEqual(2);
    expect(estimateTextTokens('')).toBe(0);
  });

  test('prices messages, reasoning and tool calls', () => {
    const plain = estimateMessageTokens(userMessage('hello'));
    const withReasoning = estimateMessageTokens({
      ...assistantMessage('hello'),
      reasoning_content: 'thinking about it',
    });
    expect(plain).toBeGreaterThan(0);
    expect(withReasoning).toBeGreaterThan(plain);
    const withTools = estimateMessageTokens({
      ...assistantMessage(''),
      tool_calls: [
        {
          id: 'c1',
          type: 'function',
          function: { name: 'read', arguments: '{"file_path":"a.txt"}' },
        },
      ],
    });
    expect(withTools).toBeGreaterThan(plain);
  });

  test('prices tool schemas', () => {
    expect(estimateToolsTokens(null)).toBe(0);
    expect(estimateToolsTokens([])).toBe(0);
    expect(
      estimateToolsTokens([
        { type: 'function', function: { name: 'read', parameters: {} } },
      ]),
    ).toBeGreaterThan(0);
  });
});