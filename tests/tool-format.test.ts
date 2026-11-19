import { describe, expect, test } from 'bun:test';

import { formatToolOutput, summarizeToolArgs } from '../src/tui/tool-format';

describe('tool formatting', () => {
  test('summarizes tool arguments on one line', () => {
    expect(
      summarizeToolArgs({ command: 'ls -la', cwd: '/tmp', timeout_seconds: 15 }),
    ).toBe('command=ls -la cwd=/tmp timeout_seconds=15');
    expect(summarizeToolArgs({ pattern: 'needle', path: 'src' })).toBe(
      'pattern=needle path=src',
    );
    expect(summarizeToolArgs({ file_path: 'a.ts', content: 'x'.repeat(200) })).toContain(
      'file_path=a.ts',
    );
    expect(summarizeToolArgs({ items: [1, 2, 3] })).toBe('items=[3 items]');
    expect(summarizeToolArgs(null)).toBe('');
  });

  test('pretty-prints JSON output', () => {
    const preview = formatToolOutput('{"a":1,"b":[2,3]}', 100);
    expect(preview.lines).toEqual(['{', '  "a": 1,', '  "b": [', '    2,', '    3', '  ]', '}']);
    expect(preview.hidden).toBe(0);
  });

  test('bounds large output and reports hidden lines', () => {
    const output = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`).join('\n');
    const preview = formatToolOutput(output, 100, 12);
    expect(preview.lines).toHaveLength(12);
    expect(preview.lines[0]).toContain('line 1');
    expect(preview.hidden).toBe(18);
  });

  test('strips ANSI escape codes and truncates long lines', () => {
    const preview = formatToolOutput('\u001b[31mred\u001b[0m ' + 'x'.repeat(300), 40);
    expect(preview.lines[0]).not.toContain('\u001b[');
    expect(preview.lines[0]!.length).toBeLessThanOrEqual(33);
  });
});
