/** MCP client — initialize / tools.list / tools.call over a JSON-RPC transport. */

import type { MCPServerConfig } from './config';
import { createTransport, type JsonRpcTransport } from './transport';

export interface MCPToolInfo {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface MCPClientOptions {
  requestTimeoutMs?: number;
}

interface ToolCallResult {
  content?: unknown[];
  isError?: boolean;
  structuredContent?: unknown;
}

function normalizeTool(raw: unknown): MCPToolInfo | null {
  if (!raw || typeof raw !== 'object') return null;
  const record = raw as Record<string, unknown>;
  const name = typeof record.name === 'string' ? record.name : '';
  if (!name) return null;
  const schema =
    record.inputSchema ?? record.input_schema ?? record.parameters ?? undefined;
  return {
    name,
    description: typeof record.description === 'string' ? record.description : '',
    inputSchema:
      schema && typeof schema === 'object' && !Array.isArray(schema)
        ? (schema as Record<string, unknown>)
        : { type: 'object', properties: {} },
  };
}

export function formatMCPToolResult(result: ToolCallResult): string {
  const parts: string[] = [];
  for (const item of result.content ?? []) {
    if (!item || typeof item !== 'object') {
      parts.push(String(item));
      continue;
    }
    const record = item as Record<string, unknown>;
    if (record.type === 'text' && typeof record.text === 'string') {
      parts.push(record.text);
    } else if (record.type === 'image') {
      parts.push(`[image: ${String(record.mimeType ?? 'unknown')}]`);
    } else if (record.type === 'resource') {
      const resource = record.resource;
      if (resource && typeof resource === 'object') {
        const text = (resource as Record<string, unknown>).text;
        parts.push(typeof text === 'string' ? text : JSON.stringify(resource));
      }
    } else {
      parts.push(JSON.stringify(record));
    }
  }
  if (result.structuredContent !== undefined) {
    parts.push(JSON.stringify(result.structuredContent, null, 2));
  }
  const text = parts.join('\n').trim();
  const body = text || JSON.stringify(result);
  return result.isError ? `Error: ${body}` : body;
}

export class MCPClient {
  private readonly transport: JsonRpcTransport;
  private readonly config: MCPServerConfig;
  private readonly options: MCPClientOptions;
  tools: MCPToolInfo[] = [];

  constructor(config: MCPServerConfig, options: MCPClientOptions = {}) {
    this.config = config;
    this.options = options;
    this.transport = createTransport(config);
  }

  private get timeoutMs(): number {
    if (this.config.timeout && this.config.timeout > 0) return this.config.timeout * 1000;
    return this.options.requestTimeoutMs ?? 30_000;
  }

  /** Handshake: initialize + notifications/initialized. */
  async initialize(): Promise<void> {
    await this.transport.request(
      'initialize',
      {
        protocolVersion: '2025-03-26',
        capabilities: {},
        clientInfo: { name: 'alex-ts', version: '0.1.0' },
      },
      this.timeoutMs,
    );
    await this.transport.notify('notifications/initialized', {});
  }

  /** List tools, following pagination cursors. */
  async listTools(): Promise<MCPToolInfo[]> {
    const collected: MCPToolInfo[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < 50; page += 1) {
      const result = await this.transport.request<{
        tools?: unknown[];
        nextCursor?: unknown;
      }>('tools/list', cursor ? { cursor } : {}, this.timeoutMs);
      for (const raw of result.tools ?? []) {
        const tool = normalizeTool(raw);
        if (tool) collected.push(tool);
      }
      cursor =
        typeof result.nextCursor === 'string' && result.nextCursor
          ? result.nextCursor
          : undefined;
      if (!cursor) break;
    }
    this.tools = collected;
    return collected;
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<string> {
    const result = await this.transport.request<ToolCallResult>(
      'tools/call',
      { name, arguments: args },
      this.timeoutMs,
    );
    return formatMCPToolResult(result);
  }

  async close(): Promise<void> {
    await this.transport.close();
  }
}