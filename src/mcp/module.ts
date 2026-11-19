/**
 * MCPModule — connects external MCP servers and exposes their tools via the bus.
 *
 * Connections run in the background so an unreachable server never blocks
 * startup. As each server connects it publishes `ToolsProvided` with its tool
 * specs; the tools gateway merges them into the catalog and routes execution
 * back through `InvokeProviderTool`.
 */

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import type { ToolResult, ToolSpec } from '../kernel/dto/tool';
import {
  GetMCPStatus,
  InvokeProviderTool,
  ToolsProvided,
  type MCPStatusSnapshot,
} from '../kernel/contracts/tools';
import type { MCPServerConfig } from './config';
import {
  RegisterPromptSection,
  RuntimeContextNotice,
  UnregisterPromptSection,
} from '../kernel/contracts/prompt';
import { loadMcpConfig } from './config';
import { MCPClient } from './client';

export interface MCPModuleOptions {
  configPath?: string;
  /** Pre-parsed configs (tests); skips reading mcp.json. */
  configs?: MCPServerConfig[];
  connectTimeoutMs?: number;
  requestTimeoutMs?: number;
  /** Periodic health-check interval in ms (default 30s; 0 disables). */
  healthIntervalMs?: number;
  /** Set false to disable the periodic health loop (tests). */
  healthCheck?: boolean;
}

interface ServerToolDetail {
  name: string;
  description: string;
  jsonSchema: Record<string, unknown>;
}

interface ServerState {
  name: string;
  transport: string;
  command: string;
  url: string;
  enabled: boolean;
  status: string;
  toolCount: number;
  tools: ServerToolDetail[];
  instructions: string;
  error: string | null;
}

interface ToolEntry {
  client: MCPClient;
  server: string;
  toolName: string;
  spec: ToolSpec;
}

let idCounter = 0;
function nextId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-${idCounter.toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function qualifyMcpToolName(serverName: string, toolName: string): string {
  return `mcp__${serverName}__${toolName}`.toLowerCase();
}

export class MCPModule implements Module {
  readonly name = 'mcp';
  readonly dependencies: readonly string[] = ['tools'];

  private readonly options: MCPModuleOptions;
  private readonly clients = new Map<string, MCPClient>();
  private readonly entries = new Map<string, ToolEntry>();
  private serverState: ServerState[] = [];
  private statusMessage = '';
  private connectTask: Promise<void> | null = null;
  private healthTimer: ReturnType<typeof setInterval> | null = null;
  private bus: AsyncEventBus | null = null;
  private lastAvailability = new Map<string, { status: string; tools: string }>();
  private lastPromptContent = '';

  constructor(options: MCPModuleOptions = {}) {
    this.options = options;
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    bus.provide(InvokeProviderTool, (req) => this.invokeProviderTool(req));
    bus.provide(GetMCPStatus, () => this.status());

    let configs: MCPServerConfig[];
    try {
      configs = this.options.configs ?? (await loadMcpConfig(this.options.configPath));
    } catch (error) {
      this.serverState = [];
      this.statusMessage = `MCP config error: ${error instanceof Error ? error.message : String(error)}`;
      this.publishToolsProvided('config_error');
      return;
    }

    this.serverState = configs.map((config) => this.initialState(config));
    if (configs.length === 0) {
      this.statusMessage = 'No MCP servers configured';
      this.publishToolsProvided('no_servers');
      return;
    }

    this.statusMessage = 'Connecting to MCP servers…';
    this.publishToolsProvided('connecting');
    // Background connection: never block module startup / TUI rendering.
    this.connectTask = this.connectAll(configs).catch((error) => {
      this.statusMessage = `MCP connection failed: ${error instanceof Error ? error.message : String(error)}`;
    });
    if (this.options.healthCheck !== false) this.startHealthLoop();
  }

  async stop(): Promise<void> {
    if (this.healthTimer) {
      clearInterval(this.healthTimer);
      this.healthTimer = null;
    }
    if (this.connectTask) {
      await this.connectTask.catch(() => undefined);
      this.connectTask = null;
    }
    for (const client of this.clients.values()) {
      await client.close().catch(() => undefined);
    }
    this.clients.clear();
    this.entries.clear();
    this.serverState = [];
    this.bus = null;
  }

  /** Wait for the background connection pass (tests / graceful shutdown). */
  async whenIdle(): Promise<void> {
    if (this.connectTask) await this.connectTask;
  }

  // ── health loop ────────────────────────────────────────────────────────

  private startHealthLoop(): void {
    const interval = this.options.healthIntervalMs ?? 30_000;
    if (interval <= 0) return;
    this.healthTimer = setInterval(() => {
      void this.refreshHealth().catch(() => undefined);
    }, interval);
    (this.healthTimer as { unref?: () => void }).unref?.();
  }

  /** Re-list tools for every connected server and refresh status/entries. */
  async refreshHealth(): Promise<void> {
    if (this.clients.size === 0) return;
    for (const [name, client] of [...this.clients]) {
      const state = this.serverState.find((item) => item.name === name);
      if (!state) continue;
      this.removeServerTools(name);
      try {
        const tools = await client.listTools();
        state.status = 'CONNECTED';
        state.error = null;
        state.toolCount = tools.length;
        state.tools = tools.map((tool) => ({
          name: tool.name,
          description: tool.description,
          jsonSchema: tool.inputSchema,
        }));
        this.installServerTools(name, client, tools);
      } catch (error) {
        state.status = 'ERROR';
        state.error = error instanceof Error ? error.message : String(error);
        state.toolCount = 0;
        state.tools = [];
      }
    }
    this.updateStatusMessage();
    this.publishToolsProvided('health');
  }

  private removeServerTools(serverName: string): void {
    for (const [qualified, entry] of [...this.entries]) {
      if (entry.server === serverName) this.entries.delete(qualified);
    }
  }

  private installServerTools(
    serverName: string,
    client: MCPClient,
    tools: { name: string; description: string; inputSchema: Record<string, unknown> }[],
  ): void {
    for (const tool of tools) {
      const qualified = qualifyMcpToolName(serverName, tool.name);
      this.entries.set(qualified, {
        client,
        server: serverName,
        toolName: tool.name,
        spec: {
          name: qualified,
          description: `[MCP:${serverName}] ${tool.description}`.trim(),
          jsonSchema: tool.inputSchema,
          provider: 'mcp',
          metadata: {
            requiredPermission: 'network',
            mcpServer: serverName,
            mcpTool: tool.name,
          },
        },
      });
    }
  }

  private updateStatusMessage(): void {
    const connected = this.serverState.filter((state) => state.status === 'CONNECTED').length;
    const failed = this.serverState.filter((state) => state.status === 'ERROR').length;
    if (connected === 0 && failed > 0) {
      this.statusMessage = 'MCP servers unavailable';
    } else if (failed > 0) {
      this.statusMessage = `MCP: ${connected} connected, ${failed} failed`;
    } else if (connected > 0) {
      this.statusMessage = `MCP: ${connected} server(s) connected`;
    } else {
      this.statusMessage = 'No MCP servers configured';
    }
  }

  get tools(): ToolSpec[] {
    return [...this.entries.values()].map((entry) => entry.spec);
  }

  // ── connection ─────────────────────────────────────────────────────────

  private async connectAll(configs: MCPServerConfig[]): Promise<void> {
    const enabled = configs.filter((config) => config.enabled);
    await Promise.all(enabled.map((config) => this.connectServer(config)));

    const connected = this.serverState.filter((state) => state.status === 'CONNECTED').length;
    const failed = this.serverState.filter((state) => state.status === 'ERROR').length;
    if (connected === 0 && failed > 0) {
      this.statusMessage = 'MCP servers unavailable';
    } else if (failed > 0) {
      this.statusMessage = `MCP: ${connected} connected, ${failed} failed`;
    } else if (connected > 0) {
      this.statusMessage = `MCP: ${connected} server(s) connected`;
    }
    this.publishToolsProvided(connected > 0 ? 'connected' : 'error');
  }

  private async connectServer(config: MCPServerConfig): Promise<void> {
    const state = this.serverState.find((item) => item.name === config.name);
    if (!state) return;

    const client = new MCPClient(config, {
      requestTimeoutMs: this.options.requestTimeoutMs,
    });
    const connectTimeoutMs = this.options.connectTimeoutMs ?? 15_000;

    try {
      await withTimeout(
        (async () => {
          await client.initialize();
          return client.listTools();
        })(),
        connectTimeoutMs,
      );
      const tools = client.tools;
      this.clients.set(config.name, client);
      state.instructions = client.instructions;
      state.status = 'CONNECTED';
      state.toolCount = tools.length;
      state.error = null;
      state.tools = tools.map((tool) => ({
        name: tool.name,
        description: tool.description,
        jsonSchema: tool.inputSchema,
      }));
      for (const tool of tools) {
        const qualified = qualifyMcpToolName(config.name, tool.name);
        this.entries.set(qualified, {
          client,
          server: config.name,
          toolName: tool.name,
          spec: {
            name: qualified,
            description: `[MCP:${config.name}] ${tool.description}`.trim(),
            jsonSchema: tool.inputSchema,
            provider: 'mcp',
            metadata: {
              requiredPermission: 'network',
              mcpServer: config.name,
              mcpTool: tool.name,
            },
          },
        });
      }
    } catch (error) {
      state.status = 'ERROR';
      state.error = error instanceof Error ? error.message : String(error);
      state.toolCount = 0;
      state.tools = [];
      await client.close().catch(() => undefined);
    }
    this.publishToolsProvided('connecting');
  }

  // ── bus handlers ───────────────────────────────────────────────────────

  private status(): MCPStatusSnapshot {
    return {
      servers: this.serverState.map((state) => ({ ...state, tools: [...state.tools] })),
      statusMessage: this.statusMessage,
    };
  }

  private async invokeProviderTool(req: InvokeProviderTool): Promise<ToolResult> {
    const runId = nextId('run');
    if (req.provider !== 'mcp') {
      return {
        name: req.name,
        error: `Provider '${req.provider}' not handled by MCP module`,
        runId,
      };
    }
    const entry = this.entries.get(req.name);
    if (!entry) {
      return { name: req.name, error: `MCP tool '${req.name}' not found`, runId };
    }
    try {
      const output = await entry.client.callTool(entry.toolName, req.args);
      return { name: req.name, output, runId };
    } catch (error) {
      return {
        name: req.name,
        error: error instanceof Error ? error.message : String(error),
        runId,
      };
    }
  }

  // ── helpers ────────────────────────────────────────────────────────────

  private initialState(config: MCPServerConfig): ServerState {
    return {
      name: config.name,
      transport: config.transport,
      command: config.command,
      url: config.url,
      enabled: config.enabled,
      status: config.enabled ? 'connecting' : 'DISABLED',
      toolCount: 0,
      tools: [],
      instructions: '',
      error: null,
    };
  }

  /** Publish/refresh the MCP section consumed by PromptModule. */
  private refreshPromptSection(): void {
    if (!this.bus) return;
    // Only healthy servers belong in the prompt. Failed/connecting servers are
    // surfaced through a one-shot context notice when their state changes.
    const available = this.serverState.filter(
      (state) => state.status === 'CONNECTED' && state.instructions.trim().length > 0,
    );
    if (available.length === 0) {
      if (this.lastPromptContent) {
        this.lastPromptContent = '';
        this.bus.publish(new UnregisterPromptSection({ id: 'mcp' }));
      }
      return;
    }
    const lines = available.map(
      (state) => `### ${state.name}\n${state.instructions.trim()}`,
    );
    const content = ['## MCP Servers', '', ...lines].join('\n');
    if (content === this.lastPromptContent) return;
    this.lastPromptContent = content;
    this.bus.publish(
      new RegisterPromptSection({
        section: {
          id: 'mcp',
          title: 'MCP Servers',
          content,
          order: 40,
          source: 'mcp',
        },
      }),
    );
  }

  /** Emit one context notice when a server becomes available/unavailable or changes tools. */
  private publishAvailabilityChanges(): void {
    if (!this.bus) return;
    const next = new Map<string, { status: string; tools: string }>();
    for (const state of this.serverState) {
      const signature = state.tools.map((tool) => tool.name).sort().join(',');
      next.set(state.name, { status: state.status, tools: signature });
      const previous = this.lastAvailability.get(state.name);
      const wasAvailable = previous?.status === 'CONNECTED';
      const isAvailable = state.status === 'CONNECTED';
      const toolList = state.tools.map((tool) => tool.name).join(', ');
      if (!wasAvailable && isAvailable) {
        this.bus.publish(
          new RuntimeContextNotice({
            key: `mcp:${state.name}`,
            source: 'mcp',
            text: `MCP server '${state.name}' is now available. Tools: ${toolList || '(none)'}.`,
          }),
        );
      } else if (wasAvailable && !isAvailable) {
        this.bus.publish(
          new RuntimeContextNotice({
            key: `mcp:${state.name}`,
            source: 'mcp',
            text: `MCP server '${state.name}' is unavailable; its tools were removed. Do not call them.`,
          }),
        );
      } else if (wasAvailable && isAvailable && previous?.tools !== signature) {
        this.bus.publish(
          new RuntimeContextNotice({
            key: `mcp:${state.name}`,
            source: 'mcp',
            text: `MCP server '${state.name}' tool list changed. Available tools: ${toolList || '(none)'}.`,
          }),
        );
      }
    }
    this.lastAvailability = next;
  }

  private publishToolsProvided(status: string): void {
    if (!this.bus) return;
    const specs = this.tools;
    this.publishAvailabilityChanges();
    this.refreshPromptSection();
    this.bus.publish(
      new ToolsProvided({
        provider: 'mcp',
        specs,
        metadata: {
          status,
          servers: this.serverState.map((state) => ({ ...state, tools: [...state.tools] })),
          statusMessage: this.statusMessage,
        },
      }),
    );
  }
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolvePromise, rejectPromise) => {
    const timer = setTimeout(
      () => rejectPromise(new Error(`MCP connection timed out after ${timeoutMs}ms`)),
      timeoutMs,
    );
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolvePromise(value);
      },
      (error) => {
        clearTimeout(timer);
        rejectPromise(error);
      },
    );
  });
}