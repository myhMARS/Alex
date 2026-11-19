import { describe, expect, test } from 'bun:test';

import { analyzeReflectionSignal } from '../src/skill/reflection-signal';
import { buildReflectionTranscript } from '../src/skill/reflector';
import { buildReflectionPrompt } from '../src/prompts/reflection';
import { mergeSkillsPrompt } from '../src/prompts/merge-skills';
import { parseMergeResponse } from '../src/skill/merge';
import { createSkill } from '../src/skill/models';
import type { MessageDTO } from '../src/kernel/dto/message';

function toolCall(id: string, name: string): MessageDTO['tool_calls'] {
  return [{ id, type: 'function', function: { name, arguments: '{}' } }];
}

describe('reflection signal', () => {
  test('qualifies a multi-tool successful workflow', () => {
    const messages: MessageDTO[] = [
      { role: 'user', content: 'fix the build' },
      { role: 'assistant', content: 'running checks', tool_calls: toolCall('a', 'read') },
      { role: 'tool', tool_call_id: 'a', content: 'file contents' },
      { role: 'assistant', content: '', tool_calls: toolCall('b', 'write') },
      { role: 'tool', tool_call_id: 'b', content: 'written' },
      { role: 'assistant', content: '', tool_calls: toolCall('c', 'bash') },
      { role: 'tool', tool_call_id: 'c', content: 'tests passed' },
      { role: 'assistant', content: 'done' },
    ];
    const signal = analyzeReflectionSignal(messages);
    expect(signal.qualified).toBe(true);
    expect(signal.toolCalls).toBe(3);
    expect(signal.failedTools).toBe(0);
    expect(signal.hasWrite).toBe(true);
    expect(signal.hasShell).toBe(true);
    expect(signal.distinctTools).toBe(3);
  });

  test('skips plain chat and failed tool runs', () => {
    expect(
      analyzeReflectionSignal([
        { role: 'user', content: 'hello' },
        { role: 'assistant', content: 'hi' },
      ]).qualified,
    ).toBe(false);

    const failed = analyzeReflectionSignal([
      { role: 'user', content: 'try' },
      { role: 'assistant', content: '', tool_calls: toolCall('a', 'bash') },
      { role: 'tool', tool_call_id: 'a', content: 'Error: command failed' },
    ]);
    expect(failed.qualified).toBe(false);
    expect(failed.failedTools).toBe(1);
  });

  test('builds a tool-aware transcript for reflection', () => {
    const transcript = buildReflectionTranscript([
      { role: 'user', content: 'deploy it' },
      {
        role: 'assistant',
        content: 'running deploy',
        tool_calls: [
          {
            id: 'call-1',
            type: 'function',
            function: { name: 'bash', arguments: '{"command":"make deploy"}' },
          },
        ],
      },
      { role: 'tool', tool_call_id: 'call-1', content: 'Error: deploy failed' },
      { role: 'assistant', content: 'I fixed the command.' },
    ]);
    expect(transcript).toContain('[tool_call name=bash args={"command":"make deploy"}]');
    expect(transcript).toContain('[tool_result id=call-1 error]');
    expect(transcript).toContain('[assistant] I fixed the command.');
  });
});

describe('reflection and merge prompts', () => {
  test('reflection prompt includes existing skill instructions and tags', () => {
    const skill = createSkill({
      name: 'debug-import-error',
      pattern: 'python import errors',
      instruction: '1. reproduce\n2. inspect traceback',
      tags: ['python', 'debug'],
    });
    const prompt = buildReflectionPrompt([skill], []);
    expect(prompt).toContain('instruction: 1. reproduce');
    expect(prompt).toContain('["python","debug"]');
    expect(prompt).toContain('untrusted data');
    expect(prompt).not.toContain('0 rounds');
  });

  test('merge prompt documents the valid empty result', () => {
    const prompt = mergeSkillsPrompt([]);
    expect(prompt).toContain('{"merged_groups": [], "deprecate_ids": []}');
    expect(prompt).toContain('each id may appear at most once');
  });

  test('merge parser treats an empty result as a valid no-op', () => {
    expect(parseMergeResponse('{"merged_groups":[],"deprecate_ids":[]}')).toEqual({
      groups: [],
      deprecateIds: [],
    });
  });
});