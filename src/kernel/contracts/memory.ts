/** Memory contracts — TypeScript port of alex/kernel/contracts/memory.py. */

import { Request } from '../message';
import type { MessageDTO } from '../dto/message';

export class GetContext extends Request<MessageDTO[]> {}

export interface MemoryStatus {
  messageCount: number;
  /** Approximate token budget (0 = count-based window only). */
  maxTokens: number;
  estimatedTokens: number;
  /** Configured model context window (0 = not configured). */
  maxContextTokens: number;
}

export class GetMemoryStatus extends Request<MemoryStatus> {}

export class AppendMessages extends Request<void> {
  messages: MessageDTO[];

  constructor(init: { messages?: MessageDTO[]; sessionId?: string } = {}) {
    super(init);
    this.messages = init.messages ?? [];
  }
}

export class ReplaceMemory extends Request<void> {
  messages: MessageDTO[];

  constructor(init: { messages?: MessageDTO[]; sessionId?: string } = {}) {
    super(init);
    this.messages = init.messages ?? [];
  }
}

export class ClearMemory extends Request<void> {}