/** Pure projection from append-only session events to model-visible messages. */

import type { MessageDTO } from '../kernel/dto/message';
import type {
  SessionEventRecord,
  SessionProjection,
  SessionTurnRecord,
} from '../kernel/contracts/session';

export type SessionMessageEventType =
  | 'user_message'
  | 'assistant_message'
  | 'tool_message';

/** Project message events; `replace_messages` replaces the whole projection. */
export function projectSessionEvents(
  events: readonly SessionEventRecord[],
  options: { applyCompaction?: boolean } = {},
): MessageDTO[] {
  const applyCompaction = options.applyCompaction !== false;
  let messages: MessageDTO[] = [];
  for (const event of events) {
    const data = event.data ?? {};
    if (
      event.type === 'user_message' ||
      event.type === 'assistant_message' ||
      event.type === 'tool_message'
    ) {
      if (data.message && typeof data.message === 'object') {
        messages.push(data.message as MessageDTO);
      }
    } else if (event.type === 'replace_messages' && applyCompaction) {
      messages = Array.isArray(data.messages)
        ? (data.messages as MessageDTO[]).map((message) => ({ ...message }))
        : [];
    } else if (event.type === 'clear_messages') {
      messages = [];
    }
  }
  return messages;
}

export function messageEvent(
  type: SessionMessageEventType,
  message: MessageDTO,
): SessionEventRecord {
  return {
    seq: 0,
    type,
    at: new Date().toISOString(),
    data: { message },
  };
}

export function replaceMessagesEvent(messages: MessageDTO[]): SessionEventRecord {
  return {
    seq: 0,
    type: 'replace_messages',
    at: new Date().toISOString(),
    data: { messages },
  };
}

/** Project messages plus turn/request surface for resume and inspection. */
const CORE_EVENT_TYPES = new Set([
  'user_message',
  'assistant_message',
  'tool_message',
  'replace_messages',
  'clear_messages',
  'turn_started',
  'request_header',
  'usage',
  'compaction_started',
  'context_compacted',
  'compaction_failed',
  'approval_requested',
  'approval_resolved',
  'skill_loaded',
  'skills_reflected',
  'tool_started',
  'tool_finished',
]);

export function projectSessionSurface(
  events: readonly SessionEventRecord[],
): SessionProjection {
  const turns: SessionTurnRecord[] = [];
  const customEvents: SessionEventRecord[] = [];
  const messageHeaderSeqs: Array<number | null> = [];
  let lastRequestHeader: Record<string, unknown> | undefined;
  let title: string | undefined;
  for (const event of events) {
    const data = event.data ?? {};
    if (
      event.type === 'user_message' ||
      event.type === 'assistant_message' ||
      event.type === 'tool_message'
    ) {
      messageHeaderSeqs.push(
        typeof data.headerSeq === 'number' ? data.headerSeq : null,
      );
    } else if (event.type === 'replace_messages') {
      const replaced = Array.isArray(data.messages) ? data.messages.length : 0;
      messageHeaderSeqs.splice(0, messageHeaderSeqs.length, ...Array(replaced).fill(null));
    }
    if (event.type === 'turn_started') {
      turns.push({
        turnId: String(data.turnId ?? ''),
        userInput: String(data.userInput ?? ''),
        kind: String(data.kind ?? 'user'),
        at: event.at,
        attachments: Array.isArray(data.attachments) ? data.attachments : [],
      });
    } else if (event.type === 'request_header') {
      lastRequestHeader = data;
    } else if (event.type === 'session_title' && typeof data.title === 'string') {
      title = data.title;
    }
    if (!CORE_EVENT_TYPES.has(event.type)) customEvents.push(event);
  }
  return {
    messages: projectSessionEvents(events),
    rawMessages: projectSessionEvents(events, { applyCompaction: false }),
    events: [...events],
    turns,
    lastRequestHeader,
    messageHeaderSeqs,
    customEvents,
    ...(title ? { title } : {}),
  };
}