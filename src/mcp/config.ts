/** MCP server config loader — TypeScript port of alex/config.py MCP section. */

import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

export type MCPTransport = 'stdio' | 'streamable-http' | 'sse';

export interface MCPServerConfig {
  name: string;
  transport: MCPTransport;
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  headers: Record<string, string>;
  timeout: number | null;
  sseReadTimeout: number | null;
  enabled: boolean;
}

export function defaultMcpConfigPath(): string {
  return join(homedir(), '.alex', 'mcp.json');
}

function normalizeTransport(
  raw: unknown,
  hasCommand: boolean,
  hasUrl: boolean,
): MCPTransport | null {
  const value = String(raw ?? '')
    .trim()
    .toLowerCase()
    .replace(/_/g, '-');
  if (!value) {
    if (hasUrl) return 'streamable-http';
    if (hasCommand) return 'stdio';
    return null;
  }
  const aliases: Record<string, MCPTransport> = {
    stdio: 'stdio',
    http: 'streamable-http',
    'streamable-http': 'streamable-http',
    streamablehttp: 'streamable-http',
    sse: 'sse',
    'http-sse': 'sse',
  };
  return aliases[value] ?? null;
}

function asStringRecord(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) result[String(key)] = String(item);
  return result;
}

export function parseMcpConfig(data: unknown): MCPServerConfig[] {
  const rawServers =
    data && typeof data === 'object' && !Array.isArray(data)
      ? (data as Record<string, unknown>).mcpServers
      : undefined;
  if (!rawServers || typeof rawServers !== 'object' || Array.isArray(rawServers)) {
    return [];
  }

  const configs: MCPServerConfig[] = [];
  for (const [name, payload] of Object.entries(rawServers as Record<string, unknown>)) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) continue;
    const record = payload as Record<string, unknown>;
    const command = String(record.command ?? '').trim();
    const url = String(record.url ?? '').trim();
    const transport = normalizeTransport(record.transport, Boolean(command), Boolean(url));
    if (!transport) continue;
    if (transport === 'stdio' && !command) continue;
    if ((transport === 'streamable-http' || transport === 'sse') && !url) continue;

    const rawArgs = Array.isArray(record.args) ? record.args : [];
    const timeout = record.timeout;
    const sseReadTimeout = record.sse_read_timeout;

    configs.push({
      name,
      transport,
      command,
      args: rawArgs.map((arg) => String(arg)),
      env: asStringRecord(record.env),
      url,
      headers: asStringRecord(record.headers),
      timeout: typeof timeout === 'number' ? timeout : null,
      sseReadTimeout: typeof sseReadTimeout === 'number' ? sseReadTimeout : null,
      enabled: record.disabled !== true,
    });
  }
  return configs;
}

/** Load `~/.alex/mcp.json` (or an explicit path). Returns [] when absent. */
export async function loadMcpConfig(path?: string): Promise<MCPServerConfig[]> {
  const target = path ?? defaultMcpConfigPath();
  let raw: string;
  try {
    raw = await readFile(target, 'utf8');
  } catch (error) {
    if (isNotFound(error)) return [];
    throw error;
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch (error) {
    throw new Error(`failed to parse ${target}: ${error instanceof Error ? error.message : String(error)}`);
  }
  return parseMcpConfig(data);
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'code' in error &&
    (error as { code?: string }).code === 'ENOENT'
  );
}