import { describe, expect, test } from 'bun:test';

import { unifiedDiff } from '../src/tools/diff';

describe('unifiedDiff', () => {
  test('reports unchanged content', () => {
    expect(unifiedDiff('same\n', 'same\n', 'a.txt')).toContain('no changes');
  });

  test('renders an added file', () => {
    const diff = unifiedDiff('', 'hello\nworld', 'new.txt');
    expect(diff).toContain('--- a/new.txt');
    expect(diff).toContain('+++ b/new.txt');
    expect(diff).toContain('+hello');
    expect(diff).toContain('+world');
  });

  test('renders context, removals and additions', () => {
    const diff = unifiedDiff('a\nb\nc\n', 'a\nB\nc\n', 'file.txt');
    expect(diff).toContain('@@ -1,4 +1,4 @@');
    expect(diff).toContain(' a');
    expect(diff).toContain('-b');
    expect(diff).toContain('+B');
    expect(diff).toContain(' c');
  });

  test('truncates very long diffs', () => {
    const old = Array.from({ length: 400 }, (_, index) => `old-${index}`).join('\n');
    const next = Array.from({ length: 400 }, (_, index) => `new-${index}`).join('\n');
    const diff = unifiedDiff(old, next, 'big.txt', { maxLines: 30 });
    expect(diff).toContain('more diff lines');
  });
});