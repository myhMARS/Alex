/** Chat contracts — TypeScript port of alex/kernel/contracts/chat.py. */

import { Command, Event, Request, type MessageContext } from '../message';
import type { AttachmentDTO, MessageDTO } from '../dto/message';
import type { ModelEffort } from './settings';

export type TurnKind = 'user' | 'cron';

/** TUI publishes this when the user submits a message. */
export class UserTurnRequested extends Command {
  userText: string;
  attachments: AttachmentDTO[];

  constructor(
    init: MessageContext & { userText?: string; attachments?: AttachmentDTO[] } = {},
  ) {
    super(init);
    this.userText = init.userText ?? '';
    this.attachments = init.attachments ?? [];
  }
}

/** Streaming text delta for the chat bubble. */
export class TokenEmitted extends Event {
  delta: string;
  streamId: string;

  constructor(init: MessageContext & { delta?: string; streamId?: string } = {}) {
    super(init);
    this.delta = init.delta ?? '';
    this.streamId = init.streamId ?? '';
  }
}

/** Update the live model effort / thinking switch without a restart. */
export class SetModelEffort extends Request<{
  thinkingEnabled: boolean;
  effort: ModelEffort;
}> {
  thinkingEnabled: boolean;
  effort: ModelEffort;

  constructor(
    init: {
      thinkingEnabled?: boolean;
      effort?: ModelEffort;
      sessionId?: string;
    } = {},
  ) {
    super(init);
    this.thinkingEnabled = init.thinkingEnabled ?? true;
    this.effort = init.effort ?? 'high';
  }
}

/** Base system prompt currently configured on the agent. */
export class GetSystemPrompt extends Request<string> {}

/** Provider-reported token usage for one model request. */
export class TokenUsageReported extends Event {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cacheHitTokens: number;
  cacheMissTokens: number;
  streamId: string;

  constructor(
    init: MessageContext & {
      promptTokens?: number;
      completionTokens?: number;
      totalTokens?: number;
      cacheHitTokens?: number;
      cacheMissTokens?: number;
      streamId?: string;
    } = {},
  ) {
    super(init);
    this.promptTokens = init.promptTokens ?? 0;
    this.completionTokens = init.completionTokens ?? 0;
    this.totalTokens = init.totalTokens ?? 0;
    this.cacheHitTokens = init.cacheHitTokens ?? 0;
    this.cacheMissTokens = init.cacheMissTokens ?? 0;
    this.streamId = init.streamId ?? '';
  }
}

/** Streaming thinking/reasoning delta. */
export class ThinkingUpdated extends Event {
  delta: string;
  streamId: string;

  constructor(init: MessageContext & { delta?: string; streamId?: string } = {}) {
    super(init);
    this.delta = init.delta ?? '';
    this.streamId = init.streamId ?? '';
  }
}

/** A conversation turn has begun. */
export class TurnStarted extends Event {
  kind: TurnKind;
  userInput: string;
  attachments: AttachmentDTO[];
  streamId: string;

  constructor(
    init: MessageContext & {
      kind?: TurnKind;
      userInput?: string;
      attachments?: AttachmentDTO[];
      streamId?: string;
    } = {},
  ) {
    super(init);
    this.kind = init.kind ?? 'user';
    this.userInput = init.userInput ?? '';
    this.attachments = init.attachments ?? [];
    this.streamId = init.streamId ?? '';
  }
}

/** A conversation turn finished successfully. */
export class TurnCompleted extends Event {
  kind: TurnKind;
  messages: MessageDTO[];
  messageBatch: MessageDTO[];
  content: string;
  thinking: string;
  streamId: string;

  constructor(
    init: MessageContext & {
      kind?: TurnKind;
      messages?: MessageDTO[];
      messageBatch?: MessageDTO[];
      content?: string;
      thinking?: string;
      streamId?: string;
    } = {},
  ) {
    super(init);
    this.kind = init.kind ?? 'user';
    this.messages = init.messages ?? [];
    this.messageBatch = init.messageBatch ?? [];
    this.content = init.content ?? '';
    this.thinking = init.thinking ?? '';
    this.streamId = init.streamId ?? '';
  }
}

/** A conversation turn failed. */
export class TurnFailed extends Event {
  error: string;
  streamId: string;

  constructor(init: MessageContext & { error?: string; streamId?: string } = {}) {
    super(init);
    this.error = init.error ?? '';
    this.streamId = init.streamId ?? '';
  }
}

/** User rated a turn (triggers skill reflection). */
export class FeedbackSubmitted extends Command {
  positive: boolean;

  constructor(init: MessageContext & { positive?: boolean } = {}) {
    super(init);
    this.positive = init.positive ?? true;
  }
}