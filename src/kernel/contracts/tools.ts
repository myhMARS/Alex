/** Tool contracts — TypeScript port of alex/kernel/contracts/tools.py. */

import { Event, Request, type MessageContext } from '../message';
import type { ToolExecutionContext, ToolResult, ToolSpec } from '../dto/tool';

// ── Requests (agent → tools gateway) ─────────────────────────────────────────

export class GetToolCatalog extends Request<ToolSpec[]> {}

export interface FileTreeEntry {
  /** Path relative to the tree root, using forward slashes. */
  path: string;
  name: string;
  depth: number;
  type: 'file' | 'dir';
}

export interface DirectorySnapshot {
  root: string;
  /** Directory path relative to the root ('' for the root itself). */
  path: string;
  entries: FileTreeEntry[];
  isGitRepo: boolean;
  gitStatus: Record<string, string>;
}

/** List one directory's immediate children (lazy file-tree browsing). */
export class ListDirectory extends Request<DirectorySnapshot> {
  constructor(
    public readonly path = '',
    public readonly maxEntries = 1000,
  ) {
    super();
  }
}

export interface FileTreeSnapshot {
  root: string;
  entries: FileTreeEntry[];
  truncated: boolean;
  /** True when the root contains a .git directory/worktree. */
  isGitRepo: boolean;
  /** Git porcelain status keyed by repo-relative path (e.g. `src/app.ts`). */
  gitStatus: Record<string, string>;
}

/** Read a shallow file tree from the tools module's first allowed root. */
export class ListFileTree extends Request<FileTreeSnapshot> {
  constructor(
    public readonly maxDepth = 2,
    public readonly maxEntries = 200,
  ) {
    super();
  }
}

export class ExecuteTool extends Request<ToolResult> {
  name: string;
  args: Record<string, unknown>;
  ctx?: ToolExecutionContext;
  timeoutSeconds: number;
  /** Caller cancellation signal. */
  signal?: AbortSignal;

  constructor(
    init: {
      name?: string;
      args?: Record<string, unknown>;
      ctx?: ToolExecutionContext;
      timeoutSeconds?: number;
      signal?: AbortSignal;
    } & {
      sessionId?: string;
      turnId?: string;
      traceId?: string;
      agentId?: string;
      parentAgentId?: string;
      teamId?: string;
    } = {},
  ) {
    super(init);
    this.name = init.name ?? '';
    this.args = init.args ?? {};
    this.ctx = init.ctx;
    this.timeoutSeconds = init.timeoutSeconds ?? 180;
    this.signal = init.signal;
  }
}

export class RegisterTool extends Request<void> {
  name: string;
  description: string;
  jsonSchema: Record<string, unknown>;
  callable?: unknown;
  metadata: Record<string, unknown>;

  constructor(
    init: {
      name?: string;
      description?: string;
      jsonSchema?: Record<string, unknown>;
      callable?: unknown;
      metadata?: Record<string, unknown>;
    } & { sessionId?: string } = {},
  ) {
    super(init);
    this.name = init.name ?? '';
    this.description = init.description ?? '';
    this.jsonSchema = init.jsonSchema ?? {};
    this.callable = init.callable;
    this.metadata = init.metadata ?? {};
  }
}

export class SetAgentPermissions extends Request<void> {
  override agentId: string;
  allowed?: string[];
  denied?: string[];

  constructor(
    init: {
      agentId?: string;
      allowed?: string[];
      denied?: string[];
      sessionId?: string;
    } = {},
  ) {
    super(init);
    this.agentId = init.agentId ?? '';
    this.allowed = init.allowed;
    this.denied = init.denied;
  }
}

export class UnregisterTool extends Request<void> {
  name: string;

  constructor(init: { name?: string } & { sessionId?: string } = {}) {
    super(init);
    this.name = init.name ?? '';
  }
}

// ── Permission modes ────────────────────────────────────────────────────────

export type PermissionMode = 'read-only' | 'manual' | 'model' | 'full';

export class GetPermissionMode extends Request<PermissionMode> {}

export class SetPermissionMode extends Request<PermissionMode> {
  mode: PermissionMode;

  constructor(init: { mode?: PermissionMode; sessionId?: string } = {}) {
    super(init);
    this.mode = init.mode ?? 'manual';
  }
}

export class PermissionModeChanged extends Event {
  mode: PermissionMode;

  constructor(init: MessageContext & { mode?: PermissionMode } = {}) {
    super(init);
    this.mode = init.mode ?? 'manual';
  }
}

// ── Events (broadcast) ───────────────────────────────────────────────────────

export class ToolsProvided extends Event {
  provider: string;
  specs: ToolSpec[];
  metadata: Record<string, unknown>;

  constructor(
    init: MessageContext & {
      provider?: string;
      specs?: ToolSpec[];
      metadata?: Record<string, unknown>;
    } = {},
  ) {
    super(init);
    this.provider = init.provider ?? '';
    this.specs = init.specs ?? [];
    this.metadata = init.metadata ?? {};
  }
}

export class ToolStarted extends Event {
  toolId: string;
  toolName: string;
  toolInput: unknown;
  streamId: string;

  constructor(
    init: MessageContext & {
      toolId?: string;
      toolName?: string;
      toolInput?: unknown;
      streamId?: string;
    } = {},
  ) {
    super(init);
    this.toolId = init.toolId ?? '';
    this.toolName = init.toolName ?? '';
    this.toolInput = init.toolInput;
    this.streamId = init.streamId ?? '';
  }
}

export class ToolFinished extends Event {
  toolId: string;
  output: unknown;
  streamId: string;

  constructor(
    init: MessageContext & { toolId?: string; output?: unknown; streamId?: string } = {},
  ) {
    super(init);
    this.toolId = init.toolId ?? '';
    this.output = init.output;
    this.streamId = init.streamId ?? '';
  }
}

// ── Tool approval (event + correlation) ──────────────────────────────────────

export class ToolApprovalRequested extends Event {
  reqId: string;
  toolName: string;
  preview: string;
  permission: string;

  constructor(
    init: MessageContext & {
      reqId?: string;
      toolName?: string;
      preview?: string;
      permission?: string;
    } = {},
  ) {
    super(init);
    this.reqId = init.reqId ?? '';
    this.toolName = init.toolName ?? '';
    this.preview = init.preview ?? '';
    this.permission = init.permission ?? '';
  }
}

export class ToolApprovalResolved extends Event {
  reqId: string;
  granted: boolean;
  remember: boolean;

  constructor(
    init: MessageContext & { reqId?: string; granted?: boolean; remember?: boolean } = {},
  ) {
    super(init);
    this.reqId = init.reqId ?? '';
    this.granted = init.granted ?? false;
    this.remember = init.remember ?? false;
  }
}

// ── MCP status (TUI → MCP module) ────────────────────────────────────────────

export interface MCPReloadResult {
  ok: boolean;
  servers: number;
  connected: number;
  tools: number;
  message: string;
}

export class ReloadMCP extends Request<MCPReloadResult> {}

export interface MCPServerToggleResult {
  ok: boolean;
  enabled: boolean;
  message: string;
}

export class SetMCPServerEnabled extends Request<MCPServerToggleResult> {
  name: string;
  enabled: boolean;

  constructor(init: { name?: string; enabled?: boolean } = {}) {
    super();
    this.name = init.name ?? '';
    this.enabled = init.enabled ?? false;
  }
}

export interface MCPStatusSnapshot {
  servers: Record<string, unknown>[];
  statusMessage?: string;
}

export class GetMCPStatus extends Request<MCPStatusSnapshot> {}

// ── Provider-level tool invocation (mcp / plugin → tools gateway) ────────────

export class InvokeProviderTool extends Request<ToolResult> {
  provider: string;
  name: string;
  args: Record<string, unknown>;
  ctx?: ToolExecutionContext;

  constructor(
    init: {
      provider?: string;
      name?: string;
      args?: Record<string, unknown>;
      ctx?: ToolExecutionContext;
    } & { sessionId?: string } = {},
  ) {
    super(init);
    this.provider = init.provider ?? '';
    this.name = init.name ?? '';
    this.args = init.args ?? {};
    this.ctx = init.ctx;
  }
}
