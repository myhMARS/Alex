/** Compaction contracts — context summarization requests and UI events. */

import { Event, Request } from '../message';

export type CompactionReason = 'auto' | 'manual';

export interface CompactionResult {
  id: string;
  reason: CompactionReason;
  compactedMessages: number;
  keptMessages: number;
  freedTokens: number;
  summary: string;
  thresholdTokens: number;
  /** True when there was nothing safe to compact. */
  skipped: boolean;
}

export class CompactContext extends Request<CompactionResult> {}

export class CompactionStarted extends Event {
  id: string;
  reason: CompactionReason;
  thresholdTokens: number;

  constructor(
    init: {
      id?: string;
      reason?: CompactionReason;
      thresholdTokens?: number;
      sessionId?: string;
      turnId?: string;
      source?: string;
      traceId?: string;
    } = {},
  ) {
    super(init);
    this.id = init.id ?? '';
    this.reason = init.reason ?? 'manual';
    this.thresholdTokens = init.thresholdTokens ?? 0;
  }
}

export class ContextCompacted extends Event {
  result: CompactionResult;

  constructor(init: { result: CompactionResult; sessionId?: string; turnId?: string } = {
    result: {
      id: '',
      reason: 'manual',
      compactedMessages: 0,
      keptMessages: 0,
      freedTokens: 0,
      summary: '',
      thresholdTokens: 0,
      skipped: true,
    },
  }) {
    super(init);
    this.result = init.result;
  }
}

export class CompactionFailed extends Event {
  id: string;
  reason: CompactionReason;
  error: string;

  constructor(
    init: {
      id?: string;
      reason?: CompactionReason;
      error?: string;
      sessionId?: string;
      turnId?: string;
    } = {},
  ) {
    super(init);
    this.id = init.id ?? '';
    this.reason = init.reason ?? 'manual';
    this.error = init.error ?? '';
  }
}