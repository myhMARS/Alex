/**
 * ToolsModule — TypeScript port of alex/tools/module.py.
 *
 * Provides `GetToolCatalog` / `ExecuteTool` / `RegisterTool` / `UnregisterTool`
 * on the bus and gates side-effect tools through `ToolApprovalRequested` /
 * `ToolApprovalResolved` with an audit trail.
 */

import { homedir } from 'node:os';
import { isAbsolute, join, relative, resolve, sep } from 'node:path';
import { readdir, stat } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import type { ToolExecutionContext, ToolResult, ToolSpec } from '../kernel/dto/tool';
import {
  ExecuteTool,
  GetToolCatalog,
  InvokeProviderTool,
  ListDirectory,
  ListFileTree,
  RegisterTool,
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolsProvided,
  UnregisterTool,
  type FileTreeEntry,
  type FileTreeSnapshot,
} from '../kernel/contracts/tools';
import { ToolRegistry } from './registry';
import type { Permission, Tool, ToolMetadata } from './tool';
import { AuditLogger, PermissionPolicy } from './permissions';
import { registerBuiltinTools } from './builtin';
import { loadPlugins, type PluginLoadResult } from './plugins';
import { resolveInRoots } from './path';

const execFileAsync = promisify(execFile);

/** Parse `git status --porcelain -z` output into path → short status code. */
export function parseGitStatusPorcelainZ(raw: string): Record<string, string> {
  const statuses: Record<string, string> = {};
  const tokens = raw.split('\0');
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (!token || token.length < 4) continue;
    const code = token.slice(0, 2);
    const file = token.slice(3).split(sep).join('/');
    if (code[0] === 'R' || code[0] === 'C') index += 1; // skip original path
    statuses[file] = normalizeGitCode(code);
  }
  return statuses;
}

function normalizeGitCode(code: string): string {
  if (code === '??') return '?';
  if (code.includes('U')) return 'U';
  if (code.includes('D')) return 'D';
  if (code.includes('R')) return 'R';
  if (code.includes('A')) return 'A';
  if (code.includes('M')) return 'M';
  return code.trim()[0] ?? '?';
}

export interface ToolsModuleOptions {
  roots?: string[];
  policy?: PermissionPolicy;
  audit?: AuditLogger;
  approvalTimeoutMs?: number;
  /** Set false to start with an empty registry (tests, custom tools). */
  builtin?: boolean;
  /** Plugin directories scanned at start; defaults to ~/.alex/plugins. */
  pluginDirs?: string[];
  /** Set false to skip plugin loading (tests). */
  plugins?: boolean;
}

type ApprovalOutcome = { granted: boolean; remember: boolean } | 'timeout';
type ApprovalResolver = (granted: boolean, remember: boolean) => void;

let idCounter = 0;
function nextId(prefix: string): string {
  idCounter += 1;
  return `${prefix}-${idCounter.toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export class ToolsModule implements Module {
  readonly name = 'tools';
  readonly dependencies: readonly string[] = [];

  readonly registry = new ToolRegistry();
  private readonly policy: PermissionPolicy;
  private readonly audit: AuditLogger;
  private readonly roots: string[];
  private readonly approvalTimeoutMs: number;
  private readonly pluginDirs: string[];
  private readonly loadPluginsAtStart: boolean;
  private readonly pending = new Map<string, ApprovalResolver>();
  private readonly providerTools = new Map<string, { provider: string; spec: ToolSpec }>();
  private readonly providerNames = new Map<string, Set<string>>();
  private pluginResult: PluginLoadResult = { loaded: [], errors: [] };
  private bus: AsyncEventBus | null = null;

  constructor(options: ToolsModuleOptions = {}) {
    this.roots = (options.roots ?? [process.cwd()]).map((root) => resolve(root));
    this.policy = options.policy ?? new PermissionPolicy();
    this.audit = options.audit ?? new AuditLogger();
    this.approvalTimeoutMs = options.approvalTimeoutMs ?? 120_000;
    this.pluginDirs = options.pluginDirs ?? [join(homedir(), '.alex', 'plugins')];
    this.loadPluginsAtStart = options.plugins !== false;
    if (options.builtin !== false) {
      registerBuiltinTools(this.registry, { roots: this.roots });
    }
  }

  async start(bus: AsyncEventBus): Promise<void> {
    if (this.loadPluginsAtStart) {
      this.pluginResult = await loadPlugins(this.registry, this.pluginDirs);
    }
    this.bus = bus;
    bus.provide(GetToolCatalog, () => this.catalog());
    bus.provide(ListFileTree, (req) => this.listFileTree(req.maxDepth, req.maxEntries));
    bus.provide(ListDirectory, (req) => this.listDirectory(req.path, req.maxEntries));
    bus.provide(ExecuteTool, (req) => this.execute(req));
    bus.provide(RegisterTool, (req) => this.registerExternal(req));
    bus.provide(UnregisterTool, (req) => {
      this.registry.unregister(req.name);
    });
    await bus.subscribe(ToolApprovalResolved, (event) => {
      this.pending.get(event.reqId)?.(event.granted, event.remember);
    });
    await bus.subscribe(ToolsProvided, (event) => {
      this.onToolsProvided(event.provider, event.specs);
    });
  }

  /** Merge tool specs announced by a provider (MCP, plugins, …). */
  private onToolsProvided(provider: string, specs: ToolSpec[]): void {
    const previous = this.providerNames.get(provider);
    if (previous) {
      for (const name of previous) this.providerTools.delete(name);
    }
    const names = new Set<string>();
    for (const spec of specs) {
      this.providerTools.set(spec.name, { provider, spec });
      names.add(spec.name);
    }
    this.providerNames.set(provider, names);
  }

  async stop(): Promise<void> {
    for (const [, resolveApproval] of this.pending) resolveApproval(false, false);
    this.pending.clear();
    this.bus = null;
  }

  get permissionPolicy(): PermissionPolicy {
    return this.policy;
  }

  get plugins(): PluginLoadResult {
    return this.pluginResult;
  }

  // ── catalog ────────────────────────────────────────────────────────────

  /** Immediate children of one directory (lazy TUI file tree). */
  async listDirectory(path = '', maxEntries = 1000): Promise<import('../kernel/contracts/tools').DirectorySnapshot> {
    const root = this.roots[0] ?? process.cwd();
    const target = path
      ? resolveInRoots(isAbsolute(path) ? path : join(root, path), this.roots, {
          defaultToCwd: true,
          label: 'path',
        })
      : root;
    const entries: FileTreeEntry[] = [];
    const ignored = new Set([
      'node_modules',
      '.git',
      '.venv',
      '__pycache__',
      '.pytest_cache',
      'dist',
      'build',
      '.idea',
    ]);
    let listing;
    try {
      listing = await readdir(target, { withFileTypes: true });
    } catch {
      return { root, path, entries: [], isGitRepo: false, gitStatus: {} };
    }
    listing.sort((a, b) => {
      if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const item of listing) {
      if (entries.length >= maxEntries) break;
      if (ignored.has(item.name)) continue;
      const absolute = join(target, item.name);
      const relativePath = relative(root, absolute).split(sep).join('/');
      entries.push({
        path: relativePath,
        name: item.name,
        depth: 0,
        type: item.isDirectory() ? 'dir' : 'file',
      });
    }
    const git = target === root
      ? await this.readGitStatus(root)
      : { isGitRepo: false, gitStatus: {} };
    return { root, path, entries, ...git };
  }

  /** Shallow tree of the first allowed root (used by the TUI files tab). */
  async listFileTree(maxDepth = 2, maxEntries = 200): Promise<FileTreeSnapshot> {
    const root = this.roots[0] ?? process.cwd();
    const entries: FileTreeEntry[] = [];
    const ignored = new Set([
      'node_modules',
      '.git',
      '.venv',
      '__pycache__',
      '.pytest_cache',
      'dist',
      'build',
      '.idea',
    ]);
    const walk = async (directory: string, depth: number): Promise<void> => {
      if (depth > maxDepth || entries.length >= maxEntries) return;
      let listing;
      try {
        listing = await readdir(directory, { withFileTypes: true });
      } catch {
        return;
      }
      listing.sort((a, b) => {
        if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      for (const item of listing) {
        if (entries.length >= maxEntries) return;
        if (ignored.has(item.name)) continue;
        const absolute = join(directory, item.name);
        const path = relative(root, absolute).split(sep).join('/');
        if (item.isDirectory()) {
          entries.push({ path, name: item.name, depth, type: 'dir' });
          await walk(absolute, depth + 1);
        } else if (item.isFile()) {
          entries.push({ path, name: item.name, depth, type: 'file' });
        }
      }
    };
    await walk(root, 0);
    const git = await this.readGitStatus(root);
    return { root, entries, truncated: entries.length >= maxEntries, ...git };
  }

  private async readGitStatus(
    root: string,
  ): Promise<{ isGitRepo: boolean; gitStatus: Record<string, string> }> {
    const marker = await stat(join(root, '.git')).catch(() => null);
    if (!marker) return { isGitRepo: false, gitStatus: {} };
    try {
      const { stdout } = await execFileAsync(
        'git',
        ['-C', root, 'status', '--porcelain', '-z', '--untracked-files=all'],
        { timeout: 5_000, maxBuffer: 5 * 1024 * 1024 },
      );
      return { isGitRepo: true, gitStatus: parseGitStatusPorcelainZ(stdout) };
    } catch {
      return { isGitRepo: true, gitStatus: {} };
    }
  }

  private catalog(): ToolSpec[] {
    const specs = new Map<string, ToolSpec>();
    for (const tool of this.registry.list()) {
      specs.set(tool.name, {
        name: tool.name,
        description: tool.description,
        jsonSchema: tool.parameters,
        provider: 'builtin',
        metadata: {},
      });
    }
    for (const [name, entry] of this.providerTools) {
      if (!specs.has(name)) specs.set(name, entry.spec);
    }
    return [...specs.values()];
  }

  private registerExternal(req: RegisterTool): void {
    const callable = req.callable;
    if (typeof callable !== 'function') {
      throw new Error('RegisterTool requires a callable invoke function');
    }
    const metadata = req.metadata as ToolMetadata;
    this.registry.register({
      name: req.name,
      description: req.description,
      parameters: req.jsonSchema,
      metadata,
      invoke: callable as Tool['invoke'],
    });
  }

  // ── execution ──────────────────────────────────────────────────────────

  private async execute(req: ExecuteTool): Promise<ToolResult> {
    const local = this.registry.get(req.name);
    const provided = this.providerTools.get(req.name);
    if (!local && !provided) {
      return { name: req.name, error: `Unknown tool: ${req.name}` };
    }

    const permission =
      local?.metadata?.requiredPermission ??
      (typeof provided?.spec.metadata?.requiredPermission === 'string'
        ? (provided.spec.metadata.requiredPermission as Permission)
        : undefined);
    const summarise = local?.metadata?.summarise;

    if (permission) {
      const decision = this.policy.check(permission);
      if (decision === 'deny') {
        await this.auditDecision(req.name, summarise, req, permission, 'denied', false);
        return blocked(req.name, `tool '${req.name}' blocked: permission '${permission}' explicitly denied`);
      }
      if (decision === 'ask') {
        const outcome = await this.requestApproval(req.name, summarise, req, permission);
        if (outcome === 'timeout') {
          await this.auditDecision(req.name, summarise, req, permission, 'timeout', false);
          return blocked(req.name, `tool '${req.name}' blocked: permission '${permission}' approval timed out`);
        }
        await this.auditDecision(
          req.name,
          summarise,
          req,
          permission,
          outcome.granted ? 'granted' : 'denied',
          outcome.remember,
        );
        if (!outcome.granted) {
          return blocked(req.name, `tool '${req.name}' blocked: user denied permission '${permission}'`);
        }
        if (outcome.remember) this.policy.remember(permission);
      }
    }

    const runId = nextId('run');
    const ctx: ToolExecutionContext = {
      sessionId: req.sessionId,
      turnId: req.turnId,
      source: 'user',
    };
    const timeoutMs = Math.max(1, req.timeoutSeconds) * 1000;
    try {
      if (local) {
        const output = await withTimeout(local.invoke(req.args, ctx), timeoutMs);
        return { name: local.name, output, runId };
      }
      const bus = this.bus;
      if (!bus) return { name: req.name, error: 'tools module is not started', runId };
      const result = await withTimeout(
        bus.request<InvokeProviderTool, ToolResult>(
          new InvokeProviderTool({
            provider: provided!.provider,
            name: req.name,
            args: req.args,
            ctx,
          }),
        ),
        timeoutMs,
      );
      return { name: req.name, output: result.output, error: result.error, runId: result.runId ?? runId };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { name: req.name, error: message, runId };
    }
  }

  // ── approval ───────────────────────────────────────────────────────────

  private requestApproval(
    toolName: string,
    summarise: ((args: Record<string, unknown>) => string) | undefined,
    req: ExecuteTool,
    permission: Permission,
  ): Promise<ApprovalOutcome> {
    const bus = this.bus;
    if (!bus) return Promise.resolve('timeout');

    const reqId = nextId('req');
    const preview = summarise?.(req.args) ?? defaultPreview(req.args);

    return new Promise<ApprovalOutcome>((resolveApproval) => {
      const timer = setTimeout(() => {
        this.pending.delete(reqId);
        resolveApproval('timeout');
      }, this.approvalTimeoutMs);

      this.pending.set(reqId, (granted, remember) => {
        clearTimeout(timer);
        this.pending.delete(reqId);
        resolveApproval({ granted, remember });
      });

      try {
        bus.publish(
          new ToolApprovalRequested({
            sessionId: req.sessionId,
            turnId: req.turnId,
            reqId,
            toolName,
            permission,
            preview,
          }),
        );
      } catch {
        clearTimeout(timer);
        this.pending.delete(reqId);
        resolveApproval('timeout');
      }
    });
  }

  private async auditDecision(
    toolName: string,
    summarise: ((args: Record<string, unknown>) => string) | undefined,
    req: ExecuteTool,
    permission: string,
    decision: 'granted' | 'denied' | 'timeout',
    remember: boolean,
  ): Promise<void> {
    await this.audit.write({
      tool: toolName,
      permission,
      decision,
      remember,
      sessionId: req.sessionId,
      preview: summarise?.(req.args) ?? defaultPreview(req.args),
    });
  }
}

function defaultPreview(args: Record<string, unknown>): string {
  try {
    const text = JSON.stringify(args, null, 2);
    return text.length > 500 ? `${text.slice(0, 500)}\n…` : text;
  } catch {
    return String(args);
  }
}

function blocked(name: string, message: string): ToolResult {
  return { name, error: message };
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolvePromise, rejectPromise) => {
    const timer = setTimeout(
      () => rejectPromise(new Error(`tool execution timed out after ${timeoutMs}ms`)),
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