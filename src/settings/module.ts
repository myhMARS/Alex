/** SettingsModule — exposes persisted model settings on the bus. */

import type { AsyncEventBus } from '../kernel/bus';
import type { Module } from '../kernel/runtime';
import {
  GetSettings,
  SettingsUpdated,
  UpdateSettings,
  type SettingsSnapshot,
} from '../kernel/contracts/settings';
import { loadSettings, saveSettings, type AlexSettings } from './store';

export interface SettingsModuleOptions {
  settings?: AlexSettings;
  /** Override the settings file path (tests / demo). */
  path?: string;
  env?: Record<string, string | undefined>;
}

export class SettingsModule implements Module {
  readonly name = 'settings';
  readonly dependencies: readonly string[] = [];

  private current: SettingsSnapshot;
  private readonly path: string | undefined;
  private readonly env: Record<string, string | undefined>;

  constructor(options: SettingsModuleOptions = {}) {
    this.current = options.settings ?? {
      provider: 'deepseek',
      baseUrl: 'https://api.deepseek.com',
      model: 'deepseek-chat',
      apiKey: '',
      maxContextTokens: 131_072,
      maxOutputTokens: 4_096,
      compactThresholdRatio: 0.8,
      imageMaxDimension: 2_048,
      thinkingEnabled: true,
      effort: 'high',
      maxIterations: 10,
    };
    this.path = options.path;
    this.env = options.env ?? process.env;
  }

  async start(bus: AsyncEventBus): Promise<void> {
    if (!this.path && this.current.apiKey === '') {
      // Best-effort: if no explicit settings were supplied, load them now.
      this.current = await loadSettings(this.env);
    }
    bus.provide(GetSettings, () => this.current);
    bus.provide(UpdateSettings, async (req) => {
      this.current = await saveSettings(req.patch, this.env, this.path ?? undefined);
      bus.publish(new SettingsUpdated({ settings: this.current }));
      return this.current;
    });
  }
}