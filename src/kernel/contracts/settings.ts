/** Settings contracts — model configuration exposed on the bus. */

import { Event, Request } from '../message';

export type ModelEffort = 'low' | 'high' | 'max';

export interface SettingsSnapshot {
  provider: string;
  baseUrl: string;
  model: string;
  /** Stored key is returned masked by the settings page, not on the bus. */
  apiKey: string;
  maxContextTokens: number;
  maxOutputTokens: number;
  /** Compact when provider prompt pressure exceeds this fraction of the window. */
  compactThresholdRatio: number;
  /** Whether the model may emit reasoning/thinking content. */
  thinkingEnabled: boolean;
  /** Reasoning effort when thinking is enabled. */
  effort: ModelEffort;
  /** Max long edge for request-time image variants (px). */
  imageMaxDimension: number;
  /** Maximum tool-call iterations per agent turn. */
  maxIterations: number;
  /** Generate a concise title for new sessions after their first turn. */
  sessionTitleGeneration?: boolean;
  /** Whether completed turns may trigger skill reflection automatically. */
  autoReflect: boolean;
  /** Minimum tool calls for an automatic reflection. */
  reflectionMinTools: number;
  /** Minimum tool calls when the turn includes write/edit. */
  reflectionMinWriteTools: number;
}

export class GetSettings extends Request<SettingsSnapshot> {}

export class UpdateSettings extends Request<SettingsSnapshot> {
  patch: Partial<SettingsSnapshot>;

  constructor(init: { patch?: Partial<SettingsSnapshot>; sessionId?: string } = {}) {
    super(init);
    this.patch = init.patch ?? {};
  }
}

export class SettingsUpdated extends Event {
  settings: SettingsSnapshot;

  constructor(init: { settings: SettingsSnapshot; sessionId?: string }) {
    super(init);
    this.settings = init.settings;
  }
}