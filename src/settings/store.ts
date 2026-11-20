/** Persisted model settings (~/.alex/settings.json) with env fallbacks. */

import { chmod, mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

import type { ModelEffort, SettingsSnapshot } from '../kernel/contracts/settings';

export const DEFAULT_MAX_CONTEXT_TOKENS = 131_072;
export const DEFAULT_MAX_OUTPUT_TOKENS = 4_096;
export const DEFAULT_COMPACT_THRESHOLD_RATIO = 0.8;
export const DEFAULT_EFFORT: ModelEffort = 'high';
export const DEFAULT_IMAGE_MAX_DIMENSION = 2_048;
export const DEFAULT_MAX_ITERATIONS = 10;
export const DEFAULT_SESSION_TITLE_GENERATION = false;
export const DEFAULT_AUTO_REFLECT = true;
export const DEFAULT_REFLECTION_MIN_TOOLS = 3;
export const DEFAULT_REFLECTION_MIN_WRITE_TOOLS = 2;

export interface AlexSettings extends SettingsSnapshot {}

export function settingsPath(env: Record<string, string | undefined> = process.env): string {
  return env.ALEX_SETTINGS_PATH || join(homedir(), '.alex', 'settings.json');
}

/** Parse `128K`, `1M`, `4096` (binary units: K=1024, M=1_048_576). */
export function parseTokenCount(input: string | number): number | null {
  if (typeof input === 'number') {
    return Number.isFinite(input) && input > 0 ? Math.floor(input) : null;
  }
  const match = input.trim().match(/^(\d+(?:\.\d+)?)\s*([kKmM])?$/);
  if (!match) return null;
  const value = Number(match[1]);
  if (!Number.isFinite(value) || value <= 0) return null;
  const unit = (match[2] ?? '').toLowerCase();
  const multiplier = unit === 'm' ? 1_048_576 : unit === 'k' ? 1_024 : 1;
  return Math.floor(value * multiplier);
}

export function formatTokenCount(tokens: number): string {
  if (tokens >= 1_048_576 && tokens % 1_048_576 === 0) return `${tokens / 1_048_576}M`;
  if (tokens >= 1_024 && tokens % 1_024 === 0) return `${tokens / 1_024}K`;
  return tokens.toLocaleString();
}

export function maskApiKey(apiKey: string): string {
  if (!apiKey) return '(not set)';
  if (apiKey.length <= 8) return `${apiKey.slice(0, 2)}****`;
  return `${apiKey.slice(0, 4)}…${apiKey.slice(-4)}`;
}

function envTokens(
  env: Record<string, string | undefined>,
  name: string,
  fallback: number,
): number {
  const raw = env[name];
  if (!raw) return fallback;
  return parseTokenCount(raw) ?? fallback;
}

function envBool(
  env: Record<string, string | undefined>,
  name: string,
  fallback: boolean,
): boolean {
  const raw = env[name]?.trim().toLowerCase();
  if (raw === undefined || raw === '') return fallback;
  if (['1', 'true', 'on', 'yes', 'enabled'].includes(raw)) return true;
  if (['0', 'false', 'off', 'no', 'disabled'].includes(raw)) return false;
  return fallback;
}

function envEffort(
  env: Record<string, string | undefined>,
  fallback: ModelEffort,
): ModelEffort {
  const raw = env.ALEX_EFFORT?.trim().toLowerCase();
  return raw === 'low' || raw === 'high' || raw === 'max' ? raw : fallback;
}

function envInt(
  env: Record<string, string | undefined>,
  name: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const raw = env[name];
  if (!raw || raw.trim() === '') return fallback;
  const parsed = Number.parseInt(raw.trim(), 10);
  return Number.isFinite(parsed) && parsed >= min && parsed <= max
    ? parsed
    : fallback;
}

function envRatio(
  env: Record<string, string | undefined>,
  name: string,
  fallback: number,
): number {
  const raw = env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 && parsed < 1 ? parsed : fallback;
}

function normalize(partial: Partial<AlexSettings>, base: AlexSettings): AlexSettings {
  return {
    provider: partial.provider ?? base.provider,
    baseUrl: partial.baseUrl ?? base.baseUrl,
    model: partial.model ?? base.model,
    apiKey: partial.apiKey ?? base.apiKey,
    maxContextTokens:
      partial.maxContextTokens ?? base.maxContextTokens,
    maxOutputTokens: partial.maxOutputTokens ?? base.maxOutputTokens,
    compactThresholdRatio:
      partial.compactThresholdRatio ?? base.compactThresholdRatio,
    thinkingEnabled: partial.thinkingEnabled ?? base.thinkingEnabled,
    effort: partial.effort ?? base.effort,
    imageMaxDimension: partial.imageMaxDimension ?? base.imageMaxDimension,
    maxIterations: partial.maxIterations ?? base.maxIterations,
    sessionTitleGeneration:
      partial.sessionTitleGeneration ?? base.sessionTitleGeneration ?? DEFAULT_SESSION_TITLE_GENERATION,
    autoReflect: partial.autoReflect ?? base.autoReflect,
    reflectionMinTools:
      partial.reflectionMinTools ?? base.reflectionMinTools,
    reflectionMinWriteTools:
      partial.reflectionMinWriteTools ?? base.reflectionMinWriteTools,
  };
}

/** Merge defaults ← env ← settings file (file wins for explicitly saved fields). */
export async function loadSettings(
  env: Record<string, string | undefined> = process.env,
  path = settingsPath(env),
): Promise<AlexSettings> {
  let base: AlexSettings = {
    provider: env.ALEX_PROVIDER ?? 'deepseek',
    baseUrl: (env.ALEX_BASE_URL ?? 'https://api.deepseek.com').replace(/\/+$/, ''),
    model: env.ALEX_MODEL ?? 'deepseek-chat',
    apiKey: env.ALEX_API_KEY ?? env.DEEPSEEK_API_KEY ?? '',
    maxContextTokens: envTokens(env, 'ALEX_CONTEXT_TOKENS', DEFAULT_MAX_CONTEXT_TOKENS),
    maxOutputTokens: envTokens(env, 'ALEX_MAX_TOKENS', DEFAULT_MAX_OUTPUT_TOKENS),
    compactThresholdRatio: envRatio(env, 'ALEX_COMPACT_THRESHOLD', DEFAULT_COMPACT_THRESHOLD_RATIO),
    thinkingEnabled: envBool(env, 'ALEX_THINKING', true),
    effort: envEffort(env, DEFAULT_EFFORT),
    imageMaxDimension: envTokens(
      env,
      'ALEX_IMAGE_MAX_DIMENSION',
      DEFAULT_IMAGE_MAX_DIMENSION,
    ),
    maxIterations: envInt(env, 'ALEX_MAX_ITERATIONS', DEFAULT_MAX_ITERATIONS, 1, 1_000),
    sessionTitleGeneration: envBool(
      env,
      'ALEX_SESSION_TITLE_GENERATION',
      DEFAULT_SESSION_TITLE_GENERATION,
    ),
    autoReflect: envBool(env, 'ALEX_AUTO_REFLECT', DEFAULT_AUTO_REFLECT),
    reflectionMinTools: envInt(
      env,
      'ALEX_REFLECTION_MIN_TOOLS',
      DEFAULT_REFLECTION_MIN_TOOLS,
      1,
      100,
    ),
    reflectionMinWriteTools: envInt(
      env,
      'ALEX_REFLECTION_MIN_WRITE_TOOLS',
      DEFAULT_REFLECTION_MIN_WRITE_TOOLS,
      1,
      100,
    ),
  };
  try {
    const raw = JSON.parse(await readFile(path, 'utf8')) as Partial<AlexSettings>;
    base = normalize(raw, base);
  } catch {
    // missing or invalid settings file → env defaults
  }
  return base;
}

/** Persist a patch (merged into the existing settings file; 0600 permissions). */
export async function saveSettings(
  patch: Partial<AlexSettings>,
  env: Record<string, string | undefined> = process.env,
  path = settingsPath(env),
): Promise<AlexSettings> {
  const current = await loadSettings(env, path);
  const next = normalize(patch, current);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(next, null, 2)}\n`, 'utf8');
  await chmod(path, 0o600).catch(() => undefined);
  return next;
}