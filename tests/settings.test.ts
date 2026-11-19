import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  formatTokenCount,
  loadSettings,
  maskApiKey,
  parseTokenCount,
  saveSettings,
} from '../src/settings/store';
import { AsyncEventBus } from '../src/kernel/bus';
import { SettingsModule } from '../src/settings/module';
import { GetSettings, SettingsUpdated, UpdateSettings } from '../src/kernel/contracts/settings';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-settings-'));
  tempDirs.push(dir);
  return dir;
}

describe('settings parsing', () => {
  test('parses K / M token units', () => {
    expect(parseTokenCount('128K')).toBe(131_072);
    expect(parseTokenCount('128k')).toBe(131_072);
    expect(parseTokenCount('1M')).toBe(1_048_576);
    expect(parseTokenCount('1.5M')).toBe(1_572_864);
    expect(parseTokenCount('4096')).toBe(4_096);
    expect(parseTokenCount('64K')).toBe(65_536);
    expect(parseTokenCount('abc')).toBeNull();
    expect(parseTokenCount('0')).toBeNull();
  });

  test('formats token counts', () => {
    expect(formatTokenCount(131_072)).toBe('128K');
    expect(formatTokenCount(1_048_576)).toBe('1M');
    expect(formatTokenCount(4_096)).toBe('4K');
  });

  test('masks API keys', () => {
    expect(maskApiKey('')).toBe('(not set)');
    expect(maskApiKey('sk-1234567890')).toBe('sk-1…7890');
    expect(maskApiKey('short')).toBe('sh****');
  });

  test('persists and merges settings with env fallbacks', async () => {
    const dir = await tempDir();
    const path = join(dir, 'settings.json');
    const env = {
      ALEX_MODEL: 'deepseek-chat',
      ALEX_CONTEXT_TOKENS: '64K',
      ALEX_COMPACT_THRESHOLD: '0.7',
      ALEX_THINKING: 'off',
      ALEX_EFFORT: 'low',
    } as Record<string, string>;
    const loaded = await loadSettings(env, path);
    expect(loaded.model).toBe('deepseek-chat');
    expect(loaded.maxContextTokens).toBe(65_536);
    expect(loaded.maxOutputTokens).toBe(4_096);
    expect(loaded.compactThresholdRatio).toBe(0.7);
    expect(loaded.thinkingEnabled).toBe(false);
    expect(loaded.effort).toBe('low');

    const saved = await saveSettings(
      { maxContextTokens: 1_048_576, maxOutputTokens: 65_536, apiKey: 'sk-secret' },
      env,
      path,
    );
    expect(saved.maxContextTokens).toBe(1_048_576);
    expect((await readFile(path, 'utf8')).includes('sk-secret')).toBe(true);

    const reloaded = await loadSettings(env, path);
    expect(reloaded.maxContextTokens).toBe(1_048_576);
    expect(reloaded.maxOutputTokens).toBe(65_536);
  });
});

describe('SettingsModule over the bus', () => {
  test('gets and updates settings, publishing SettingsUpdated', async () => {
    const dir = await tempDir();
    const path = join(dir, 'settings.json');
    const bus = new AsyncEventBus();
    const module = new SettingsModule({
      path,
      env: {},
      settings: {
        provider: 'deepseek',
        baseUrl: 'https://api.deepseek.com',
        model: 'deepseek-chat',
        apiKey: 'sk-x',
        maxContextTokens: 131_072,
        maxOutputTokens: 4_096,
        compactThresholdRatio: 0.8,
        thinkingEnabled: true,
        effort: 'high',
      },
    });
    const updates: number[] = [];
    await module.start(bus);
    await bus.start();
    await bus.subscribe(SettingsUpdated, (event) => {
      updates.push(event.settings.maxOutputTokens);
    });

    const initial = await bus.request(new GetSettings());
    expect(initial.maxContextTokens).toBe(131_072);

    const updated = await bus.request(
      new UpdateSettings({ patch: { maxOutputTokens: 65_536 } }),
    );
    expect(updated.maxOutputTokens).toBe(65_536);
    expect(updates).toEqual([65_536]);
    expect((await loadSettings({}, path)).maxOutputTokens).toBe(65_536);
    await bus.shutdown();
  });
});