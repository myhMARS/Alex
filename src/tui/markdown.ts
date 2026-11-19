/** Markdown formatting/parsing helpers for the terminal UI. */

export interface InlineToken {
  text: string;
  bold?: boolean;
  code?: boolean;
  link?: boolean;
}

export type MarkdownBlock =
  | { type: 'heading'; level: number; tokens: InlineToken[] }
  | { type: 'paragraph'; tokens: InlineToken[] }
  | { type: 'list'; tokens: InlineToken[] }
  | { type: 'quote'; tokens: InlineToken[] }
  | { type: 'code'; language: string; content: string }
  | { type: 'hr' };

function cleanInline(text: string): string {
  return text
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1$2')
    .replace(/(^|[^_])_([^_]+)_/g, '$1$2')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1 ($2)');
}

/**
 * Strip Markdown syntax while keeping structure readable in a terminal:
 * headings get a marker, list bullets are normalized, fenced code is indented.
 */
export function formatMarkdown(text: string): string {
  if (!text) return '';
  const output: string[] = [];
  let inFence = false;

  for (const rawLine of text.split('\n')) {
    if (/^\s*```/.test(rawLine)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) {
      output.push(`│ ${rawLine}`);
      continue;
    }
    const heading = rawLine.match(/^\s{0,3}#{1,6}\s+(.*)$/);
    if (heading) {
      output.push(`▍${cleanInline(heading[1] ?? '')}`);
      continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(rawLine)) {
      output.push('────');
      continue;
    }
    const quote = rawLine.match(/^\s*>\s?(.*)$/);
    if (quote) {
      output.push(`│ ${cleanInline(quote[1] ?? '')}`);
      continue;
    }
    const bullet = rawLine.match(/^(\s*)[-*+]\s+(.*)$/);
    if (bullet) {
      output.push(`${bullet[1] ?? ''}• ${cleanInline(bullet[2] ?? '')}`);
      continue;
    }
    output.push(cleanInline(rawLine));
  }

  return output.join('\n');
}

const INLINE_PATTERN = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)/g;

export function parseInline(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  INLINE_PATTERN.lastIndex = 0;

  while ((match = INLINE_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({ text: text.slice(lastIndex, match.index) });
    }
    if (match[1] !== undefined) {
      tokens.push({ text: match[1], bold: true });
    } else if (match[2] !== undefined) {
      tokens.push({ text: match[2], code: true });
    } else if (match[3] !== undefined) {
      tokens.push({ text: `${match[3]}${match[4] ? ` (${match[4]})` : ''}`, link: true });
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) tokens.push({ text: text.slice(lastIndex) });
  return tokens.length > 0 ? tokens : [{ text }];
}

export function parseMarkdown(text: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let fence: { language: string; lines: string[] } | null = null;

  for (const line of text.split('\n')) {
    const fenceMatch = line.match(/^\s*```(.*)$/);
    if (fenceMatch) {
      if (fence) {
        blocks.push({ type: 'code', language: fence.language, content: fence.lines.join('\n') });
        fence = null;
      } else {
        fence = { language: (fenceMatch[1] ?? '').trim(), lines: [] };
      }
      continue;
    }
    if (fence) {
      fence.lines.push(line);
      continue;
    }
    if (!line.trim()) continue;

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/);
    if (heading) {
      blocks.push({
        type: 'heading',
        level: (heading[1] ?? '#').length,
        tokens: parseInline(heading[2] ?? ''),
      });
      continue;
    }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      blocks.push({ type: 'hr' });
      continue;
    }
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      blocks.push({ type: 'quote', tokens: parseInline(quote[1] ?? '') });
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      blocks.push({ type: 'list', tokens: parseInline(bullet[1] ?? '') });
      continue;
    }
    blocks.push({ type: 'paragraph', tokens: parseInline(line) });
  }
  if (fence) {
    blocks.push({ type: 'code', language: fence.language, content: fence.lines.join('\n') });
  }
  return blocks;
}