/** Parser for DeepSeek-style DSML tool-call markup emitted as plain content. */

import type { ToolCall } from './client';

const START = /<[^>\n]{0,100}calls>/i;
const END = /<\/[^>\n]{0,100}calls>/i;
const INVOKE = /<[^>\n]{0,100}invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/[^>\n]{0,100}invoke>/gi;
const PARAMETER =
  /<[^>\n]{0,100}parameter\s+name="([^"]+)"([^>]*)>([\s\S]*?)<\/[^>\n]{0,100}parameter>/gi;

function decodeEntities(value: string): string {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

function parseArguments(inner: string): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  PARAMETER.lastIndex = 0;
  for (const match of inner.matchAll(PARAMETER)) {
    const name = match[1] ?? '';
    const attrs = match[2] ?? '';
    const rawValue = decodeEntities((match[3] ?? '').trim());
    if (!name) continue;
    if (/string="false"/i.test(attrs)) {
      try {
        args[name] = JSON.parse(rawValue);
      } catch {
        args[name] = rawValue;
      }
    } else {
      args[name] = rawValue;
    }
  }
  return args;
}

/** Parse one complete DSML tool-call block. */
export function parseDsmlBlock(block: string): ToolCall[] {
  const calls: ToolCall[] = [];
  INVOKE.lastIndex = 0;
  let index = 0;
  for (const match of block.matchAll(INVOKE)) {
    const name = match[1] ?? '';
    if (!name) continue;
    calls.push({
      id: `call_dsml_${index}`,
      type: 'function',
      function: {
        name,
        arguments: JSON.stringify(parseArguments(match[2] ?? '')),
      },
    });
    index += 1;
  }
  return calls;
}

export interface DsmlExtraction {
  cleaned: string;
  calls: ToolCall[];
}

/** Pull DSML tool-call blocks out of plain content. */
export function extractDsml(text: string): DsmlExtraction {
  let cleaned = '';
  const calls: ToolCall[] = [];
  let rest = text;
  for (;;) {
    const start = START.exec(rest);
    if (!start) {
      cleaned += rest;
      break;
    }
    cleaned += rest.slice(0, start.index);
    const after = rest.slice(start.index + start[0].length);
    const end = END.exec(after);
    if (!end) {
      // Unclosed block: leave it untouched so the user can see the raw output.
      cleaned += rest.slice(start.index);
      break;
    }
    const block = rest.slice(start.index, start.index + start[0].length + end.index + end[0].length);
    calls.push(...parseDsmlBlock(block));
    rest = after.slice(end.index + end[0].length);
  }
  return { cleaned, calls };
}

/** Streaming filter that suppresses DSML blocks while forwarding normal text. */
export class DsmlStreamFilter {
  private pending = '';
  private buffer = '';
  private capturing = false;
  private readonly calls: ToolCall[] = [];

  feed(delta: string): string {
    if (!delta && !this.pending) return '';
    const text = this.pending + delta;
    this.pending = '';
    if (!this.capturing) {
      const start = START.exec(text);
      if (!start) {
        // Hold a short tail so a marker split across stream chunks is not
        // emitted as ordinary content.
        const keep = Math.min(160, text.length);
        this.pending = text.slice(text.length - keep);
        return text.slice(0, text.length - keep);
      }
      this.capturing = true;
      this.buffer = text.slice(start.index);
      const prefix = text.slice(0, start.index);
      return prefix + this.consumeIfClosed();
    }
    this.buffer += text;
    return this.consumeIfClosed();
  }

  finish(): string {
    if (this.capturing) {
      const raw = this.buffer;
      this.buffer = '';
      this.capturing = false;
      return raw;
    }
    const rest = this.pending;
    this.pending = '';
    return rest;
  }

  takeCalls(): ToolCall[] {
    return [...this.calls];
  }

  private consumeIfClosed(): string {
    const end = END.exec(this.buffer);
    if (!end) return '';
    const block = this.buffer.slice(0, end.index + end[0].length);
    this.buffer = this.buffer.slice(end.index + end[0].length);
    this.calls.push(...parseDsmlBlock(block));
    this.capturing = false;
    return this.feed(this.buffer);
  }
}