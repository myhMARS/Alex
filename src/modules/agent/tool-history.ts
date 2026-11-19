/** Repair OpenAI-style tool-call history before resending it to a model. */

import type { MessageDTO } from '../../kernel/dto/message';
import { toolMessage } from '../../kernel/dto/message';

export const MISSING_TOOL_RESULT =
  'Error: tool call was interrupted and returned no result. Continue without it.';

/**
 * Every assistant message with tool_calls must be followed by one tool message
 * per tool_call_id. Failed/cancelled turns used to leave dangling calls in the
 * session log, which providers reject with HTTP 400 on the next request.
 *
 * This is a request-time repair: it never mutates the persisted session.
 */
export function ensureToolCallResponses(messages: MessageDTO[]): MessageDTO[] {
  const repaired: MessageDTO[] = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]!;
    repaired.push(message);
    if (message.role !== 'assistant' || !Array.isArray(message.tool_calls)) {
      continue;
    }
    const pending = new Set<string>();
    for (const call of message.tool_calls) {
      if (typeof call.id === 'string' && call.id) pending.add(call.id);
    }
    if (pending.size === 0) continue;

    let next = index + 1;
    while (next < messages.length && messages[next]!.role === 'tool') {
      const tool = messages[next]!;
      const toolCallId =
        typeof tool.tool_call_id === 'string' ? tool.tool_call_id : '';
      if (toolCallId) pending.delete(toolCallId);
      repaired.push(tool);
      next += 1;
    }
    for (const toolCallId of pending) {
      repaired.push(toolMessage(MISSING_TOOL_RESULT, toolCallId));
    }
    index = next - 1;
  }
  return repaired;
}