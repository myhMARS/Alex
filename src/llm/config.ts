/** LLM configuration — TypeScript port of alex/config.py (LLM subset). */

import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

export interface LLMConfig {
  provider: string;
  baseUrl: string;
  model: string;
  apiKey: string;
  maxTokens: number;
  temperature: number;
  timeoutMs: number;
}

export interface LLMConfigOverrides {
  provider?: string;
  baseUrl?: string;
  model?: string;
  apiKey?: string;
  maxTokens?: number;
  temperature?: number;
  timeoutMs?: number;
}

function envNumber(
  env: Record<string, string | undefined>,
  name: string,
  fallback: number,
): number {
  const raw = env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function loadLLMConfig(
  env: Record<string, string | undefined> = process.env,
  overrides: LLMConfigOverrides = {},
): LLMConfig {
  return {
    provider: overrides.provider ?? env.ALEX_PROVIDER ?? 'deepseek',
    baseUrl: (overrides.baseUrl ?? env.ALEX_BASE_URL ?? 'https://api.deepseek.com').replace(
      /\/+$/,
      '',
    ),
    model: overrides.model ?? env.ALEX_MODEL ?? 'deepseek-chat',
    apiKey: overrides.apiKey ?? env.ALEX_API_KEY ?? env.DEEPSEEK_API_KEY ?? '',
    maxTokens: overrides.maxTokens ?? envNumber(env, 'ALEX_MAX_TOKENS', 4096),
    temperature: overrides.temperature ?? envNumber(env, 'ALEX_TEMPERATURE', 0),
    timeoutMs: overrides.timeoutMs ?? 180_000,
  };
}

/**
 * Minimal `.env` loader (CWD first, then ~/.alex/.env), mirroring Python's
 * dotenv behaviour without a dependency. Existing env vars win.
 */
export async function loadDotEnv(
  paths: string[] = [join(process.cwd(), '.env'), join(homedir(), '.alex', '.env')],
): Promise<string[]> {
  const loaded: string[] = [];
  for (const path of paths) {
    let raw: string;
    try {
      raw = await readFile(path, 'utf8');
    } catch {
      continue;
    }
    for (const line of raw.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const exportless = trimmed.startsWith('export ') ? trimmed.slice(7) : trimmed;
      const index = exportless.indexOf('=');
      if (index <= 0) continue;
      const key = exportless.slice(0, index).trim();
      let value = exportless.slice(index + 1).trim();
      if (
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }
      if (process.env[key] === undefined) process.env[key] = value;
    }
    loaded.push(path);
  }
  return loaded;
}