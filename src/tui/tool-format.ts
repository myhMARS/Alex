/** Compact, readable formatting for tool calls in the TUI. */

export interface ToolOutputPreview {
  lines: string[];
  hidden: number;
}

const ANSI_PATTERN = /\u001b\[[0-9;]*[A-Za-z]/g;

function compactValue(value: unknown, maxLength: number): string {
  if (typeof value === 'string') {
    const singleLine = value.replace(/\s+/g, ' ').trim();
    return singleLine.length > maxLength ? `${singleLine.slice(0, maxLength)}…` : singleLine;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return `[${value.length} item${value.length === 1 ? '' : 's'}]`;
  if (value && typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>);
    return `{${keys.slice(0, 3).join(', ')}${keys.length > 3 ? ', …' : ''}}`;
  }
  return String(value ?? '');
}

/** One-line argument summary: `command=ls -la cwd=/tmp`. */
export function summarizeToolArgs(input: unknown, maxLength = 90): string {
  if (input === undefined || input === null) return '';
  if (typeof input !== 'object' || Array.isArray(input)) {
    return compactValue(input, maxLength);
  }
  const entries = Object.entries(input as Record<string, unknown>).filter(
    ([, value]) => value !== undefined && value !== null && value !== '',
  );
  const parts: string[] = [];
  for (const [key, value] of entries) {
    const rendered = `${key}=${compactValue(value, 36)}`;
    if (parts.join(' ').length + rendered.length > maxLength) break;
    parts.push(rendered);
  }
  return parts.join(' ');
}

/** Pretty-print common output shapes and bound the number of rendered lines. */
export function formatToolOutput(
  output: string,
  width: number,
  maxLines = 12,
): ToolOutputPreview {
  let text = output.replace(ANSI_PATTERN, '').replace(/\r\n/g, '\n').trim();
  if (!text) return { lines: [], hidden: 0 };

  const trimmed = text.trim();
  if (
    (trimmed.startsWith('{') && trimmed.endsWith('}')) ||
    (trimmed.startsWith('[') && trimmed.endsWith(']'))
  ) {
    try {
      text = JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      // keep raw text
    }
  }

  const maxLineLength = Math.max(24, width - 8);
  const all = text
    .split('\n')
    .map((line) => line.replace(/\t/g, '  ').replace(/\s+$/, ''))
    .map((line) =>
      line.length > maxLineLength ? `${line.slice(0, maxLineLength)}…` : line,
    );
  while (all.length > 0 && all[all.length - 1] === '') all.pop();

  const visible = all.slice(0, maxLines);
  return { lines: visible, hidden: Math.max(0, all.length - visible.length) };
}