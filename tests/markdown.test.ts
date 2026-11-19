import { describe, expect, test } from 'bun:test';

import { formatMarkdown, parseInline, parseMarkdown } from '../src/tui/markdown';

describe('formatMarkdown', () => {
  test('renders headings, lists and rules', () => {
    const text = ['# Title', '', '- first', '* second', '', '---'].join('\n');
    const output = formatMarkdown(text);
    expect(output).toContain('▍Title');
    expect(output).toContain('• first');
    expect(output).toContain('• second');
    expect(output).toContain('────');
  });

  test('keeps fenced code and strips fence lines', () => {
    const output = formatMarkdown('```ts\nconst x = 1;\n```');
    expect(output).toContain('│ const x = 1;');
    expect(output).not.toContain('```');
  });

  test('strips inline formatting and expands links', () => {
    const output = formatMarkdown('**bold** and `code` and [docs](https://example.com)');
    expect(output).toBe('bold and code and docs (https://example.com)');
  });

  test('renders blockquotes with a gutter', () => {
    expect(formatMarkdown('> quoted')).toBe('│ quoted');
  });

  test('parses structured blocks for the rich renderer', () => {
    const blocks = parseMarkdown(
      ['# Title', '', 'Some **bold** text', '', '- item one', '- item two', '', '```ts', 'const x = 1;', '```', '', '> note', '', '---'].join(
        '\n',
      ),
    );
    expect(blocks.map((block) => block.type)).toEqual([
      'heading',
      'paragraph',
      'list',
      'list',
      'code',
      'quote',
      'hr',
    ]);
    const heading = blocks[0];
    expect(heading?.type === 'heading' && heading.level).toBe(1);
    const code = blocks.find((block) => block.type === 'code');
    expect(code?.type === 'code' && code.content).toBe('const x = 1;');
  });

  test('parses inline tokens', () => {
    const tokens = parseInline('a **bold** and `code` and [docs](https://x.dev)');
    expect(tokens.find((token) => token.bold)?.text).toBe('bold');
    expect(tokens.find((token) => token.code)?.text).toBe('code');
    expect(tokens.find((token) => token.link)?.text).toBe('docs (https://x.dev)');
  });
});