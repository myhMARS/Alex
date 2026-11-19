import { describe, expect, test } from 'bun:test';

import { formatApprovalPreview } from '../src/tui/approval-format';

describe('approval preview formatting', () => {
  test('wraps long lines instead of cutting them off', () => {
    const preview = formatApprovalPreview('x'.repeat(200), 40);
    expect(preview.lines.length).toBeGreaterThan(1);
    for (const line of preview.lines) expect(line.length).toBeLessThanOrEqual(34);
    expect(preview.lines.join('').replace(/\s/g, '')).toBe('x'.repeat(200));
  });

  test('wraps CJK text by display width', () => {
    const preview = formatApprovalPreview('中'.repeat(40), 40);
    expect(preview.lines.length).toBeGreaterThan(1);
    for (const line of preview.lines) {
      const cols = [...line].reduce((sum, char) => sum + (char === ' ' ? 1 : 2), 0);
      expect(cols).toBeLessThanOrEqual(34);
    }
  });

  test('expands tabs and drops trailing blank lines', () => {
    const preview = formatApprovalPreview('a\tb\n\n\n', 100);
    expect(preview.lines).toEqual(['a  b']);
  });

  test('caps very long previews and reports truncation', () => {
    const text = Array.from({ length: 500 }, (_, index) => `line ${index + 1}`).join('\n');
    const preview = formatApprovalPreview(text, 100, 200);
    expect(preview.lines).toHaveLength(200);
    expect(preview.total).toBe(500);
    expect(preview.truncated).toBe(true);
  });
});
