import { describe, expect, test } from 'bun:test';

import { highlightCode, highlightLine } from '../src/tui/highlight';

function kinds(line: string, language: string): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  for (const token of highlightLine(line, language)) {
    (result[token.kind] ??= []).push(token.text);
  }
  return result;
}

describe('syntax highlighting', () => {
  test('highlights Python keywords, strings and comments', () => {
    const tokens = kinds('def run(x): return "value"  # note', 'python');
    expect(tokens.keyword).toEqual(expect.arrayContaining(['def', 'return']));
    expect(tokens.string).toContain('"value"');
    expect(tokens.comment?.[0]).toContain('# note');
  });

  test('highlights TypeScript keywords, numbers and comments', () => {
    const tokens = kinds('const count = 42; // answer', 'ts');
    expect(tokens.keyword).toContain('const');
    expect(tokens.number).toContain('42');
    expect(tokens.comment?.[0]).toContain('// answer');
  });

  test('falls back to plain text for unknown languages', () => {
    const tokens = kinds('just some prose here', 'unknown-lang');
    expect(tokens.plain?.join('')).toBe('just some prose here');
  });

  test('highlightCode splits lines', () => {
    const lines = highlightCode('const a = 1;\nconst b = 2;', 'typescript');
    expect(lines).toHaveLength(2);
    expect(lines[1]?.find((token) => token.kind === 'number')?.text).toBe('2');
  });
});