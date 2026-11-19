/**
 * TuiModule — the terminal UI as a first-class bus module.
 *
 * This is the architectural core of the TS rewrite: instead of the TUI owning
 * an ad-hoc event client (Python's WS bridge), the UI is a `Module` like any
 * other. It subscribes to UI events, folds them into a view model, and
 * publishes commands (`UserTurnRequested`, `FeedbackSubmitted`,
 * `ToolApprovalResolved`) back onto the same bus.
 *
 * A renderer adapter (OpenTUI) consumes `viewState` and calls the public
 * intent methods below.
 */

import type { Module } from '../../kernel/runtime';
import type { AttachmentDTO } from '../../kernel/dto/message';
import type { AsyncEventBus, EventClass, EventHandler } from '../../kernel/bus';
import type { Event } from '../../kernel/message';
import {
  FeedbackSubmitted,
  ThinkingUpdated,
  TokenEmitted,
  TokenUsageReported,
  TurnCompleted,
  TurnFailed,
  TurnStarted,
  UserTurnRequested,
  SetModelEffort,
} from '../../kernel/contracts/chat';
import { SkillLoaded, SkillsReflected, SkillReflectError } from '../../kernel/contracts/skills';
import {
  CompactContext,
  CompactionFailed,
  CompactionStarted,
  ContextCompacted,
  type CompactionResult,
} from '../../kernel/contracts/compaction';
import {
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolFinished,
  ToolsProvided,
  ToolStarted,
} from '../../kernel/contracts/tools';
import { CronJobEvent, ListCronJobs } from '../../kernel/contracts/cron';
import {
  GetMCPStatus,
  GetToolCatalog,
  ListDirectory,
  type FileTreeEntry,
  type FileTreeSnapshot,
} from '../../kernel/contracts/tools';
import { ListSkills } from '../../kernel/contracts/skills';
import { ListSessions, LoadSession, type SessionMeta } from '../../kernel/contracts/session';
import {
  GetContext,
  GetMemoryStatus,
  ReplaceMemory,
} from '../../kernel/contracts/memory';
import {
  GetSettings,
  UpdateSettings,
  type ModelEffort,
  type SettingsSnapshot,
} from '../../kernel/contracts/settings';
import { formatTokenCount, maskApiKey, parseTokenCount } from '../../settings/store';
import {
  TuiViewState,
  type ApprovalView,
  type CronEventView,
  type TurnView,
} from './view-state';

function messageContent(message: { content?: unknown }): string {
  if (typeof message.content === 'string') return message.content;
  if (Array.isArray(message.content)) {
    return message.content
      .map((part) => {
        if (part && typeof part === 'object') {
          const record = part as Record<string, unknown>;
          if (record.type === 'text' && typeof record.text === 'string') return record.text;
          if (record.type === 'image_url') return '[image]';
        }
        return '[attachment]';
      })
      .join('\n');
  }
  return message.content == null ? '' : String(message.content);
}

function contextPreview(message: { content?: unknown }): string {
  const text = messageContent(message).replace(/\s+/g, ' ').trim();
  if (!text) return '(empty)';
  return text.length > 48 ? `${text.slice(0, 48)}…` : text;
}

function formatSessionTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const UI_EVENTS: EventClass[] = [
  TurnStarted,
  TokenEmitted,
  TokenUsageReported,
  ThinkingUpdated,
  SkillLoaded,
  ToolStarted,
  ToolFinished,
  TurnCompleted,
  TurnFailed,
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolsProvided,
  SkillsReflected,
  SkillReflectError,
  CronJobEvent,
  CompactionStarted,
  ContextCompacted,
  CompactionFailed,
];

export type TuiPageKind =
  | 'help'
  | 'skills'
  | 'cron'
  | 'mcp'
  | 'sessions'
  | 'reflections'
  | 'context'
  | 'settings';

export interface TuiPage {
  kind: TuiPageKind;
  title: string;
  body: string;
}

export type TuiSidebarTab = 'overview' | 'files';

export interface TuiFileRow {
  path: string;
  name: string;
  depth: number;
  type: 'file' | 'dir';
  expanded: boolean;
  gitStatus: string;
}

export interface TuiSidebar {
  tools: number;
  skills: number;
  cron: number;
  mcpStatus: string;
}

export interface TuiSnapshot {
  revision: number;
  sidebar: TuiSidebar;
  turns: TurnView[];
  streaming: TurnView[];
  approvals: ApprovalView[];
  cron: CronEventView[];
  reflection: SkillsReflected | null;
  page: TuiPage | null;
  sessionId: string;
  sidebarTab: TuiSidebarTab;
  fileTree: FileTreeSnapshot;
  fileRows: TuiFileRow[];
  fileSelected: number;
  thinkingExpanded: boolean;
  skillsExpanded: boolean;
  toolOutputExpanded: boolean;
  sidebarCollapsed: boolean;
  toast: string | null;
  thinkingEnabled: boolean;
  effort: string;
  model: string;
  contextPercent: number | null;
}

const HELP_TEXT = [
  '可用命令：',
  '',
  '  /help            显示本帮助',
  '  /skills          技能列表（name / status / pattern）',
  '  /cron            定时任务列表',
  '  /mcp             MCP 服务器状态',
  '  /sessions        已持久化会话列表',
  '  /reflections     最近的技能反思记录',
  '  /context         当前对话上下文窗口状态',
  '  /settings        模型设置（API Key / 上下文 / 输出 token）',
  '  /set <key> <val> 修改设置，如 /set max_context 1M',
  '  /compact         手动压缩上下文（较早历史摘要为一条消息）',
  '  /image <path>   发送图片（png/jpg/gif/webp）',
  '  /file <path>    发送文本或二进制文件',
  '  @<path>         在消息中内联附件路径（支持 "带空格 路径"）',
  '  /effort <level> 设置模型强度：nothinking / low / high / max',
  '  /clear           清空当前视图',
  '  /quit            退出',
  '',
  '  Ctrl+T           展开/收起 thinking',
  '  Ctrl+K           展开/收起 skills',
  '  Ctrl+O           展开/收起工具输出',
  '  Ctrl+S           展开/收起侧边栏',
  '  F1 / F2          侧栏 Overview / Files 标签',
  '  ↑↓ / →← / Enter  FILES 树选择、展开、收起',
  '  F5               刷新文件树',
  '  F6               粘贴剪贴板图片（截图后直接附加）',
  '',
  '页面打开时：:q 或 Esc 关闭。',
  '审批弹窗：y 批准 · s 批准并记住 · n/Esc 拒绝。',
  '全局：Ctrl+C 退出。',
].join('\n');

export class TuiModule implements Module {
  readonly name = 'tui';
  readonly dependencies: readonly string[] = ['agent'];
  readonly viewState = new TuiViewState();

  private readonly subscriptions: { type: EventClass; handler: EventHandler }[] = [];
  private readonly listeners = new Set<() => void>();
  private bus: AsyncEventBus | null = null;
  private revision = 0;
  private snapshot: TuiSnapshot;
  private page: TuiPage | null = null;
  private sessionId = '';
  private sessionChoices: SessionMeta[] = [];
  private mcpStatusMessage = '';
  private mcpServers: Record<string, unknown>[] = [];
  private readonly mcpExpanded = new Set<number>();
  private sidebar: TuiSidebar = { tools: 0, skills: 0, cron: 0, mcpStatus: 'unknown' };
  private sidebarTab: TuiSidebarTab = 'overview';
  private fileTree: FileTreeSnapshot = {
    root: '',
    entries: [],
    truncated: false,
    isGitRepo: false,
    gitStatus: {},
  };
  private readonly expandedDirs = new Set<string>();
  private readonly children = new Map<string, FileTreeEntry[]>();
  private fileRows: TuiFileRow[] = [];
  private fileSelected = 0;
  private selectedPath = '';
  private sidebarTimer: ReturnType<typeof setInterval> | null = null;
  private thinkingExpanded = false;
  private skillsExpanded = false;
  private toolOutputExpanded = false;
  private sidebarCollapsed = true;
  private toast: string | null = null;
  private toastTimer: ReturnType<typeof setTimeout> | null = null;
  private compactRatioForDisplay = 0.8;
  private thinkingEnabled = true;
  private effort: ModelEffort = 'high';
  private model = '';
  private contextWindowTokens = 0;

  constructor(
    options: {
      thinkingEnabled?: boolean;
      effort?: ModelEffort;
      model?: string;
      contextWindowTokens?: number;
    } = {},
  ) {
    this.thinkingEnabled = options.thinkingEnabled ?? true;
    this.effort = options.effort ?? 'high';
    this.model = options.model ?? '';
    this.contextWindowTokens = options.contextWindowTokens ?? 0;
    this.snapshot = this.buildSnapshot();
  }

  /** React `useSyncExternalStore` subscribe. */
  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** Stable snapshot until the next bus event. */
  readonly getSnapshot = (): TuiSnapshot => this.snapshot;

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    for (const type of UI_EVENTS) {
      const handler: EventHandler = (event) => this.handle(event);
      await bus.subscribe(type, handler);
      this.subscriptions.push({ type, handler });
    }
    void this.refreshSidebar();
    this.sidebarTimer = setInterval(() => {
      void this.refreshSidebar();
    }, 5_000);
    (this.sidebarTimer as { unref?: () => void }).unref?.();
  }

  async stop(): Promise<void> {
    if (this.toastTimer) {
      clearTimeout(this.toastTimer);
      this.toastTimer = null;
    }
    if (this.sidebarTimer) {
      clearInterval(this.sidebarTimer);
      this.sidebarTimer = null;
    }
    if (this.bus) {
      for (const { type, handler } of this.subscriptions) {
        await this.bus.unsubscribe(type, handler);
      }
    }
    this.subscriptions.length = 0;
    this.bus = null;
  }

  // ── user intents (published on the bus) ────────────────────────────────

  submitUserTurn(
    text: string,
    sessionId?: string,
    attachments: AttachmentDTO[] = [],
  ): void {
    if (sessionId) this.sessionId = sessionId;
    this.publish(
      new UserTurnRequested({
        sessionId: this.sessionId,
        userText: text,
        attachments,
      }),
    );
  }

  submitFeedback(positive: boolean, sessionId?: string, turnId = ''): void {
    const lastTurn = this.viewState.turns[this.viewState.turns.length - 1];
    this.publish(
      new FeedbackSubmitted({
        sessionId: sessionId ?? this.sessionId,
        turnId: turnId || lastTurn?.turnId || '',
        positive,
      }),
    );
  }

  get currentSessionId(): string {
    return this.sessionId;
  }

  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
    this.refresh();
  }

  /** Load a persisted session into memory and rebuild the chat view. */
  async loadSession(targetSessionId: string): Promise<boolean> {
    const bus = this.bus;
    if (!bus) return false;
    try {
      const bundle = await bus.request(new LoadSession({ targetSessionId }));
      if (!bundle) return false;
      const sessionId = bundle.sessionId || targetSessionId;
      await bus.request(
        new ReplaceMemory({ sessionId, messages: bundle.messages ?? [] }),
      );
      this.sessionId = sessionId;
      this.viewState.loadHistory(bundle.messages ?? []);
      this.page = null;
      this.sessionChoices = [];
      this.refresh();
      return true;
    } catch {
      return false;
    }
  }

  /** Select the n-th (1-based) session from the `/sessions` page. */
  async chooseSession(index: number): Promise<boolean> {
    const choice = this.sessionChoices[index - 1];
    if (!choice) return false;
    return this.loadSession(choice.sessionId);
  }

  resolveApproval(reqId: string, granted: boolean, remember = false): void {
    this.publish(new ToolApprovalResolved({ reqId, granted, remember }));
  }

  toggleThinking(): boolean {
    this.thinkingExpanded = !this.thinkingExpanded;
    this.refresh();
    return this.thinkingExpanded;
  }

  toggleSkills(): boolean {
    this.skillsExpanded = !this.skillsExpanded;
    this.refresh();
    return this.skillsExpanded;
  }

  async setSidebarTab(tab: TuiSidebarTab): Promise<void> {
    this.sidebarTab = tab;
    this.refresh();
    if (tab === 'files') await this.refreshFileTree();
  }

  toggleSidebar(): boolean {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    this.refresh();
    return this.sidebarCollapsed;
  }

  toggleToolOutput(): boolean {
    this.toolOutputExpanded = !this.toolOutputExpanded;
    this.refresh();
    return this.toolOutputExpanded;
  }

  setToast(message: string, durationMs = 2_500): void {
    this.toast = message;
    if (this.toastTimer) clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => {
      this.toast = null;
      this.toastTimer = null;
      this.refresh();
    }, durationMs);
    (this.toastTimer as { unref?: () => void }).unref?.();
    this.refresh();
  }

  clearToast(): void {
    if (this.toastTimer) {
      clearTimeout(this.toastTimer);
      this.toastTimer = null;
    }
    if (this.toast === null) return;
    this.toast = null;
    this.refresh();
  }

  resetView(): void {
    this.viewState.reset();
    this.page = null;
    this.refresh();
  }

  /** Refresh the root + expanded directories (best effort, lazy loading). */
  /** Refresh the file tree (manual F5 / tab switch; no 5s polling). */
  async refreshFileTree(): Promise<void> {
    const bus = this.bus;
    if (!bus) return;
    const previousExpanded = new Set(this.expandedDirs);
    const previousSelected = this.selectedPath;
    try {
      const root = await bus.request(new ListDirectory(''));
      this.children.clear();
      this.children.set('', root.entries);
      this.fileTree = {
        root: root.root,
        entries: [],
        truncated: false,
        isGitRepo: root.isGitRepo,
        gitStatus: root.gitStatus,
      };
      this.expandedDirs.clear();
      for (const path of previousExpanded) {
        try {
          const snapshot = await bus.request(new ListDirectory(path));
          this.children.set(path, snapshot.entries);
          this.expandedDirs.add(path);
        } catch {
          // directory disappeared or became unreadable
        }
      }
    } catch {
      this.children.clear();
      this.fileTree = {
        root: '',
        entries: [],
        truncated: false,
        isGitRepo: false,
        gitStatus: {},
      };
      this.expandedDirs.clear();
    }
    this.rebuildFileRows(previousSelected);
    this.refresh();
  }

  /** `/effort` — switch model strength, including turning thinking off. */
  async setEffort(value: string): Promise<{ ok: boolean; message: string }> {
    const raw = value.trim().toLowerCase();
    if (!raw) {
      return {
        ok: true,
        message: `当前 effort：${this.thinkingEnabled ? this.effort : 'nothinking'}`,
      };
    }
    if (raw === 'nothinking' || raw === 'off' || raw === 'none') {
      this.thinkingEnabled = false;
    } else if (raw === 'low' || raw === 'high' || raw === 'max') {
      this.thinkingEnabled = true;
      this.effort = raw;
    } else {
      return {
        ok: false,
        message: `无效 effort：${value}（nothinking / low / high / max）`,
      };
    }
    const bus = this.bus;
    if (bus) {
      await bus
        .request(
          new UpdateSettings({
            patch: { thinkingEnabled: this.thinkingEnabled, effort: this.effort },
          }),
        )
        .catch(() => undefined);
      await bus
        .request(
          new SetModelEffort({
            thinkingEnabled: this.thinkingEnabled,
            effort: this.effort,
          }),
        )
        .catch(() => undefined);
    }
    this.refresh();
    return {
      ok: true,
      message: `effort 已设为 ${this.thinkingEnabled ? this.effort : 'nothinking'}`,
    };
  }

  /** Manually compact the current session's context. */
  async compactContext(): Promise<CompactionResult> {
    const bus = this.bus;
    if (!bus) throw new Error('TuiModule.start() has not been called');
    return bus.request(new CompactContext({ sessionId: this.sessionId }));
  }

  /** Apply one `/set <key> <value>` change (persists + updates memory settings). */
  async updateSetting(key: string, value: string): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: '设置服务不可用' };
    const normalized = key.trim().toLowerCase().replace(/[-_]/g, '');
    const patch: Partial<SettingsSnapshot> = {};
    if (normalized === 'provider') patch.provider = value.trim();
    else if (normalized === 'model') patch.model = value.trim();
    else if (normalized === 'apikey') patch.apiKey = value.trim();
    else if (normalized === 'baseurl') patch.baseUrl = value.trim();
    else if (normalized === 'maxcontext' || normalized === 'context') {
      const parsed = parseTokenCount(value);
      if (parsed === null) return { ok: false, message: `无效的上下文长度：${value}（支持 128K / 1M）` };
      patch.maxContextTokens = parsed;
    } else if (normalized === 'thinking') {
      const raw = value.trim().toLowerCase();
      if (!['on', 'off', 'true', 'false', '1', '0'].includes(raw)) {
        return { ok: false, message: `无效的 thinking 值：${value}（on/off）` };
      }
      patch.thinkingEnabled = ['on', 'true', '1'].includes(raw);
    } else if (normalized === 'effort') {
      const raw = value.trim().toLowerCase();
      if (raw !== 'low' && raw !== 'high' && raw !== 'max') {
        return { ok: false, message: `无效的 effort：${value}（low/high/max）` };
      }
      patch.effort = raw;
    } else if (normalized === 'compactthreshold' || normalized === 'threshold') {
      const parsed = Number(value);
      if (!Number.isFinite(parsed) || parsed <= 0 || parsed >= 1) {
        return { ok: false, message: `无效的压缩阈值：${value}（应为 0~1 之间，如 0.8）` };
      }
      patch.compactThresholdRatio = parsed;
    } else if (normalized === 'maxoutput' || normalized === 'output') {
      const parsed = parseTokenCount(value);
      if (parsed === null) return { ok: false, message: `无效的输出 token 数：${value}（支持 64K / 1M）` };
      patch.maxOutputTokens = parsed;
    } else {
      return { ok: false, message: `未知设置项：${key}` };
    }
    try {
      await bus.request(new UpdateSettings({ patch }));
    } catch (error) {
      return { ok: false, message: `保存失败：${error instanceof Error ? error.message : String(error)}` };
    }
    if (patch.thinkingEnabled !== undefined) this.thinkingEnabled = patch.thinkingEnabled;
    if (patch.effort !== undefined) this.effort = patch.effort;
    if (patch.thinkingEnabled !== undefined || patch.effort !== undefined) {
      await this.bus
        ?.request(
          new SetModelEffort({
            thinkingEnabled: this.thinkingEnabled,
            effort: this.effort,
          }),
        )
        .catch(() => undefined);
      this.refresh();
    }
    if (this.page?.kind === 'settings') await this.showPage('settings');
    const runtime = patch.thinkingEnabled !== undefined || patch.effort !== undefined;
    return {
      ok: true,
      message: `已保存 ${key} = ${value}（${runtime ? '已即时生效' : '重启后生效'}）`,
    };
  }

  /** Move the file-tree cursor by `delta` rows. */
  moveFileSelection(delta: number): void {
    if (this.fileRows.length === 0) return;
    this.fileSelected = Math.max(
      0,
      Math.min(this.fileRows.length - 1, this.fileSelected + delta),
    );
    this.selectedPath = this.fileRows[this.fileSelected]?.path ?? '';
    this.refresh();
  }

  /** Expand the selected directory (no-op for files). */
  async expandSelectedDir(): Promise<void> {
    const row = this.fileRows[this.fileSelected];
    if (row?.type !== 'dir' || this.expandedDirs.has(row.path)) return;
    this.expandedDirs.add(row.path);
    if (!this.children.has(row.path)) {
      const bus = this.bus;
      if (bus) {
        try {
          const snapshot = await bus.request(new ListDirectory(row.path));
          this.children.set(row.path, snapshot.entries);
        } catch {
          this.children.set(row.path, []);
        }
      }
    }
    this.rebuildFileRows(row.path);
    this.refresh();
  }

  /** Collapse the selected directory, or jump to its parent directory. */
  collapseSelectedDir(): void {
    const row = this.fileRows[this.fileSelected];
    if (!row) return;
    if (row.type === 'dir' && this.expandedDirs.has(row.path)) {
      this.expandedDirs.delete(row.path);
      this.rebuildFileRows(row.path);
      this.refresh();
      return;
    }
    const parent = row.path.includes('/')
      ? row.path.slice(0, row.path.lastIndexOf('/'))
      : null;
    if (!parent) return;
    const parentIndex = this.fileRows.findIndex((item) => item.path === parent);
    if (parentIndex >= 0) {
      this.fileSelected = parentIndex;
      this.selectedPath = this.fileRows[parentIndex]?.path ?? '';
      this.refresh();
    }
  }

  /** Toggle the selected directory (used by Enter). */
  async toggleSelectedDir(): Promise<void> {
    const row = this.fileRows[this.fileSelected];
    if (row?.type !== 'dir') return;
    if (this.expandedDirs.has(row.path)) this.collapseSelectedDir();
    else await this.expandSelectedDir();
  }

  private rebuildFileRows(preservePath = this.selectedPath): void {
    const rows: TuiFileRow[] = [];
    const walk = (dirPath: string, depth: number): void => {
      for (const entry of this.children.get(dirPath) ?? []) {
        const expanded = entry.type === 'dir' && this.expandedDirs.has(entry.path);
        rows.push({
          ...entry,
          depth,
          expanded,
          gitStatus:
            entry.type === 'file'
              ? this.fileTree.gitStatus[entry.path] ?? ''
              : this.directoryGitStatus(entry.path),
        });
        if (expanded) walk(entry.path, depth + 1);
      }
    };
    walk('', 0);
    this.fileRows = rows;
    const preserved = rows.findIndex((row) => row.path === preservePath);
    this.fileSelected =
      preserved >= 0
        ? preserved
        : Math.max(0, Math.min(this.fileSelected, rows.length - 1));
    this.selectedPath = this.fileRows[this.fileSelected]?.path ?? '';
  }

  private directoryGitStatus(path: string): string {
    const prefix = `${path}/`;
    const statuses = Object.entries(this.fileTree.gitStatus)
      .filter(([file]) => file.startsWith(prefix))
      .map(([, status]) => status);
    for (const candidate of ['U', 'D', 'R', 'A', 'M', '?']) {
      if (statuses.includes(candidate)) return candidate;
    }
    return '';
  }

  /** Refresh the sidebar counters/status from the bus (best effort). */
  async refreshSidebar(): Promise<void> {
    const bus = this.bus;
    if (!bus) return;
    const safe = async <T>(promise: Promise<T>, fallback: T): Promise<T> => {
      try {
        return await promise;
      } catch {
        return fallback;
      }
    };
    const [catalog, skills, cron, mcp] = await Promise.all([
      safe(bus.request(new GetToolCatalog()), []),
      safe(bus.request(new ListSkills({ includeDeprecated: false })), []),
      safe(bus.request(new ListCronJobs()), []),
      safe(bus.request(new GetMCPStatus()), { servers: [], statusMessage: '' }),
    ]);
    this.sidebar = {
      tools: catalog.length,
      skills: skills.length,
      cron: cron.length,
      mcpStatus: mcp.statusMessage || 'unknown',
    };
    this.refresh();
  }

  // ── pages (bus-backed, mirroring the Python TUI slash commands) ────────

  async showPage(kind: TuiPageKind, notice?: string): Promise<void> {
    if (kind === 'mcp') this.mcpExpanded.clear();
    const titles: Record<TuiPageKind, string> = {
      help: '帮助',
      skills: '技能列表',
      cron: '定时任务',
      mcp: 'MCP 状态',
      sessions: '会话列表',
      reflections: '反思记录',
      context: '上下文窗口',
      settings: '模型设置',
    };
    this.page = { kind, title: titles[kind], body: '加载中…' };
    this.refresh();

    let body: string;
    try {
      body = await this.loadPageBody(kind);
    } catch (error) {
      body = `加载失败：${error instanceof Error ? error.message : String(error)}`;
    }
    if (notice) body = `${notice}\n\n${body}`;
    this.page = { kind, title: titles[kind], body };
    this.refresh();
  }

  /** Expand/collapse one MCP server in the open `/mcp` page. */
  toggleMcpServer(index: number): boolean {
    if (this.page?.kind !== 'mcp') return false;
    if (index < 1 || index > this.mcpServers.length) return false;
    if (this.mcpExpanded.has(index)) this.mcpExpanded.delete(index);
    else this.mcpExpanded.add(index);
    this.page = { ...this.page, body: this.buildMcpBody() };
    this.refresh();
    return true;
  }

  private buildMcpBody(): string {
    if (this.mcpServers.length === 0) return this.mcpStatusMessage;
    const lines = [this.mcpStatusMessage];
    this.mcpServers.forEach((server, index) => {
      const position = index + 1;
      const expanded = this.mcpExpanded.has(position);
      const toolCount = Number(server.tool_count ?? server.toolCount ?? 0);
      lines.push(
        `${position}. ${expanded ? '▼' : '▶'} ${String(server.name ?? '')} [${String(server.status ?? '')}] ` +
          `transport=${String(server.transport ?? '')} tools=${toolCount}` +
          (server.error ? ` error=${String(server.error)}` : ''),
      );
      if (!expanded) return;
      const tools = Array.isArray(server.tools) ? (server.tools as Record<string, unknown>[]) : [];
      for (const tool of tools) {
        lines.push(
          `   🔧 ${String(tool.name ?? '')}${tool.description ? ` — ${String(tool.description).slice(0, 80)}` : ''}`,
        );
      }
      if (tools.length === 0) lines.push('   (no tools registered)');
    });
    lines.push('', '输入数字展开/收起服务器工具');
    return lines.join('\n');
  }

  dismissPage(): void {
    if (!this.page) return;
    this.page = null;
    this.refresh();
  }

  private requireBusForPage(): AsyncEventBus {
    if (!this.bus) throw new Error('TuiModule.start() has not been called');
    return this.bus;
  }

  private async loadPageBody(kind: TuiPageKind): Promise<string> {
    const bus = this.requireBusForPage();
    if (kind === 'help') return HELP_TEXT;

    if (kind === 'settings') {
      try {
        const settings = await bus.request(new GetSettings());
        this.compactRatioForDisplay = settings.compactThresholdRatio;
        return [
          '模型设置（/set <key> <value> 修改 · 重启后生效）',
          '',
          `provider      ${settings.provider}`,
          `model         ${settings.model}`,
          `api_key       ${maskApiKey(settings.apiKey)}`,
          `base_url      ${settings.baseUrl}`,
          `max_context   ${formatTokenCount(settings.maxContextTokens)}（${settings.maxContextTokens.toLocaleString()} tokens）`,
          `max_output    ${formatTokenCount(settings.maxOutputTokens)}（${settings.maxOutputTokens.toLocaleString()} tokens）`,
          `compact_ratio ${settings.compactThresholdRatio}（达到上下文 ${Math.round(settings.compactThresholdRatio * 100)}% 时自动压缩）`,
          `thinking      ${settings.thinkingEnabled ? 'on' : 'off'}`,
          `effort        ${settings.thinkingEnabled ? settings.effort : 'nothinking（thinking 关闭）'}`,
          '',
          '可用键：provider · model · api_key · base_url · max_context · max_output · compact_threshold · thinking · effort',
          '示例：/set max_context 1M   /set max_output 64K   /set api_key sk-…',
          '',
          '单位：K = 1,024，M = 1,048,576（二进制）；也可直接输入精确值，',
          '      如 /set max_context 131072（= 128K）。',
          '说明：max_context 会减去 max_output 作为消息预算。',
        ].join('\n');
      } catch {
        return '设置服务不可用（SettingsModule 未启动）。';
      }
    }

    if (kind === 'skills') {
      const cards = await bus.request(new ListSkills({ includeDeprecated: false }));
      if (cards.length === 0) return '暂无技能。';
      return cards
        .map((card) => {
          const status = String(card.status ?? 'ACTIVE');
          return `- ${card.name} [${status}] ${card.pattern ?? ''}`;
        })
        .join('\n');
    }

    if (kind === 'context') {
      const sessionId = this.sessionId;
      const [messages, status, settings] = await Promise.all([
        bus.request(new GetContext({ sessionId })),
        bus
          .request(new GetMemoryStatus({ sessionId }))
          .catch(() => ({
            messageCount: 0,
            maxTokens: 0,
            estimatedTokens: 0,
            maxContextTokens: 0,
          })),
        bus.request(new GetSettings({ sessionId })).catch(() => null),
      ]);
      if (settings) this.compactRatioForDisplay = settings.compactThresholdRatio;
      const usage = this.viewState.lastUsage;
      const totals = this.viewState.sessionUsage;
      const capacity = status.maxContextTokens || status.maxTokens || 0;
      const latestPrompt = usage?.promptTokens ?? 0;
      const contextRatio = capacity > 0 ? Math.min(1, latestPrompt / capacity) : 0;
      const cacheTotal = totals.cacheHitTokens + totals.cacheMissTokens;
      const hitRate =
        cacheTotal > 0 ? (totals.cacheHitTokens / cacheTotal) * 100 : null;
      const barFor = (ratio: number): string => {
        const filled = Math.round(ratio * 10);
        return `${'█'.repeat(filled)}${'░'.repeat(10 - filled)}`;
      };
      const lines = [
        `会话：${sessionId || '-'}`,
        usage && capacity > 0
          ? `上下文：${latestPrompt.toLocaleString()} / ${capacity.toLocaleString()}（${Math.round(contextRatio * 100)}%）`
          : '上下文：等待 provider 上报',
        usage && capacity > 0 ? `      [${barFor(contextRatio)}]` : '',
        `本次会话：输入 ${totals.inputTokens.toLocaleString()} · 输出 ${totals.outputTokens.toLocaleString()} · 请求 ${totals.requests} 次`,
        cacheTotal > 0
          ? `缓存：命中 ${totals.cacheHitTokens.toLocaleString()} · 未命中 ${totals.cacheMissTokens.toLocaleString()} · 命中率 ${hitRate?.toFixed(1)}%`
          : '缓存：provider 未上报（DeepSeek 缓存字段）',
        usage
          ? `最近请求：prompt ${usage.promptTokens.toLocaleString()} · completion ${usage.completionTokens.toLocaleString()} · total ${usage.totalTokens.toLocaleString()} · cache ${usage.cacheHitTokens.toLocaleString()}/${(usage.cacheHitTokens + usage.cacheMissTokens).toLocaleString()}`
          : '最近请求：暂无 provider 上报',
        `历史消息：${messages.length} 条（无数量上限）`,
        `压缩阈值：${Math.round((this.compactRatioForDisplay ?? 0.8) * 100)}% · /compact 手动压缩`,
        '',
        '最近消息：',
      ];
      if (messages.length === 0) {
        lines.push('  （空上下文）');
      } else {
        messages.slice(-8).forEach((message, index) => {
          lines.push(
            `  ${String(index + 1).padStart(2, ' ')}. ${String(message.role).padEnd(9)} ${contextPreview(message)}`,
          );
        });
      }
      return lines.join('\n');
    }

    if (kind === 'reflections') {
      const records = [...this.viewState.reflectionHistory].reverse();
      if (records.length === 0) return '暂无反思记录。';
      return records
        .slice(0, 20)
        .map((record, index) => {
          const time = formatSessionTime(new Date(record.at).toISOString());
          const lines = [
            `${String(index + 1).padStart(2, ' ')}. ${time}  +${record.created} ~${record.updated} -${record.deprecated}`,
          ];
          if (record.names.length > 0) lines.push(`    新建：${record.names.join(', ')}`);
          if (record.updatedNames.length > 0) {
            lines.push(`    更新：${record.updatedNames.join(', ')}`);
          }
          if (record.deprecated > 0) lines.push(`    废弃：${record.deprecated} 个技能`);
          return lines.join('\n');
        })
        .join('\n\n');
    }

    if (kind === 'cron') {
      const jobs = await bus.request(new ListCronJobs());
      if (jobs.length === 0) return '暂无定时任务。';
      return jobs
        .map((job) => {
          const next = job.nextRunAt ? new Date(job.nextRunAt).toLocaleString() : '-';
          return `- ${job.id} [${job.status}] ${job.name} (${job.cron}) runs=${job.runsDone} next=${next}`;
        })
        .join('\n');
    }

    if (kind === 'mcp') {
      const status = await bus.request(new GetMCPStatus());
      this.mcpStatusMessage = status.statusMessage || 'MCP status unknown';
      this.mcpServers = status.servers;
      return this.buildMcpBody();
    }

    const sessions = await bus.request(new ListSessions());
    const ordered = [...sessions].sort((a, b) => {
      const left = a.updatedAt ?? a.createdAt;
      const right = b.updatedAt ?? b.createdAt;
      return right.localeCompare(left);
    });
    this.sessionChoices = ordered;
    if (ordered.length === 0) return '暂无已保存的会话。';
    return ordered
      .map((session, index) => {
        const when = formatSessionTime(session.updatedAt ?? session.createdAt);
        const message = session.firstMessage.replace(/\s+/g, ' ').trim();
        return [
          `${String(index + 1).padStart(2, ' ')}. ${session.sessionId}`,
          `    ${when} · ${session.messageCount} msgs`,
          message ? `    ${message}` : '',
          '',
        ]
          .filter(Boolean)
          .join('\n');
      })
      .join('\n');
  }

  // ── internals ──────────────────────────────────────────────────────────

  private handle(event: Event): void {
    this.viewState.apply(event);
    this.refresh();
  }

  private buildSnapshot(): TuiSnapshot {
    return {
      revision: this.revision,
      turns: this.viewState.turns,
      streaming: this.viewState.streaming,
      approvals: this.viewState.pendingApprovals,
      cron: this.viewState.cron,
      reflection: this.viewState.lastReflection,
      page: this.page,
      sessionId: this.sessionId,
      sidebar: this.sidebar,
      sidebarTab: this.sidebarTab,
      fileTree: this.fileTree,
      fileRows: this.fileRows,
      fileSelected: this.fileSelected,
      thinkingExpanded: this.thinkingExpanded,
      skillsExpanded: this.skillsExpanded,
      toolOutputExpanded: this.toolOutputExpanded,
      sidebarCollapsed: this.sidebarCollapsed,
      toast: this.toast,
      thinkingEnabled: this.thinkingEnabled,
      effort: this.thinkingEnabled ? this.effort : 'nothinking',
      model: this.model,
      contextPercent:
        this.contextWindowTokens > 0 && this.viewState.lastUsage
          ? Math.min(
              999,
              Math.round(
                (this.viewState.lastUsage.promptTokens / this.contextWindowTokens) * 100,
              ),
            )
          : null,
    };
  }

  private refresh(): void {
    this.revision += 1;
    this.snapshot = this.buildSnapshot();
    for (const listener of this.listeners) listener();
  }

  private publish(event: Event): void {
    if (!this.bus) throw new Error('TuiModule.start() has not been called');
    this.bus.publish(event);
  }
}