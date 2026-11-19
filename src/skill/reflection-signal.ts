/** Heuristic gate for deciding whether a turn is worth reflecting on. */

import type { MessageDTO } from '../kernel/dto/message';
import type { ReflectionSignal } from '../kernel/contracts/skills';

const WRITE_TOOLS = new Set(['write', 'edit']);
const SHELL_TOOLS = new Set(['bash', 'pwsh', 'git_inspect']);

function toolCallName(call: unknown): string {
  if (!call || typeof call !== 'object') return '';
  const fn = (call as { function?: { name?: unknown } }).function;
  return typeof fn?.name === 'string' ? fn.name : '';
}

function contentText(message: MessageDTO): string {
  if (typeof message.content === 'string') return message.content;
  if (!Array.isArray(message.content)) return '';
  return message.content
    .map((part) =>
      part && typeof part === 'object' && (part as { type?: string }).type === 'text'
        ? String((part as { text?: unknown }).text ?? '')
        : '',
    )
    .join('\n');
}

/**
 * Signals used by the automatic reflection gate:
 * - complexity: enough tool calls / a write or shell workflow
 * - success: no tool result starts with `Error:`
 * - scope: only count user turns, not system/compaction messages
 */
export interface ReflectionGateOptions {
  minToolCalls?: number;
  minWriteToolCalls?: number;
}

export function analyzeReflectionSignal(
  messages: MessageDTO[],
  options: ReflectionGateOptions = {},
): ReflectionSignal {
  const minToolCalls = Math.max(1, options.minToolCalls ?? 3);
  const minWriteToolCalls = Math.max(1, options.minWriteToolCalls ?? 2);
  let toolCalls = 0;
  let failedTools = 0;
  let userTurns = 0;
  let hasWrite = false;
  let hasShell = false;
  const distinctTools = new Set<string>();

  for (const message of messages) {
    if (message.role === 'user') {
      userTurns += 1;
      continue;
    }
    if (message.role === 'tool') {
      if (/^\s*Error:/i.test(contentText(message))) failedTools += 1;
      continue;
    }
    if (message.role !== 'assistant' || !Array.isArray(message.tool_calls)) continue;
    for (const call of message.tool_calls) {
      const name = toolCallName(call);
      toolCalls += 1;
      if (name) distinctTools.add(name);
      if (WRITE_TOOLS.has(name)) hasWrite = true;
      if (SHELL_TOOLS.has(name)) hasShell = true;
    }
  }

  let score = 0;
  if (toolCalls >= 3) score += 2;
  else if (toolCalls > 0) score += 1;
  if (hasWrite) score += 1;
  if (hasShell) score += 1;
  if (userTurns > 1) score += 1;
  if (failedTools > 0) score -= 1;

  const reasons: string[] = [];
  if (toolCalls > 0) reasons.push(`${toolCalls} tool call(s)`);
  if (hasWrite) reasons.push('file write/edit');
  if (hasShell) reasons.push('shell/git workflow');
  if (failedTools > 0) reasons.push(`${failedTools} failed tool result(s)`);
  if (userTurns > 1) reasons.push(`${userTurns} user turns`);

  const qualified =
    failedTools === 0 &&
    (toolCalls >= minToolCalls ||
      (hasWrite && toolCalls >= minWriteToolCalls));

  return {
    score,
    qualified,
    reasons,
    toolCalls,
    failedTools,
    distinctTools: distinctTools.size,
    hasWrite,
    hasShell,
    userTurns,
  };
}