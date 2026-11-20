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
  CancelQueuedTurn,
  FeedbackSubmitted,
  ThinkingUpdated,
  TokenEmitted,
  TokenUsageReported,
  TurnCompleted,
  TurnDequeued,
  TurnFailed,
  TurnQueued,
  TurnQueueCancelled,
  TurnStarted,
  UserTurnRequested,
  SetModelEffort,
  SetPromptPressure,
  SetRequestHeader,
} from '../../kernel/contracts/chat';
import {
  MergeSkills,
  ReflectSkills,
  SkillLoaded,
  SkillsReflected,
  SkillReflectError,
} from '../../kernel/contracts/skills';
import {
  CompactContext,
  CompactionFailed,
  CompactionStarted,
  ContextCompacted,
  type CompactionResult,
} from '../../kernel/contracts/compaction';
import {
  PermissionModeChanged,
  ReloadMCP,
  SetMCPServerEnabled,
  SetPermissionMode,
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolFinished,
  ToolsProvided,
  ToolStarted,
  type PermissionMode,
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
import {
  GetPromptInfo,
  ReloadPrompts,
} from '../../kernel/contracts/prompt';
import {
  AgentDisposed,
  AgentSpawned,
  DisposeAgent,
  DisposeTeam,
  AgentTaskFinished,
  AgentTaskStarted,
  ListAgentMessages,
  ListAgents,
  ListAgentTasks,
  type AgentInfo,
  type AgentTaskInfo,
} from '../../kernel/contracts/agents';
import {
  AppendSessionEvents,
  CheckpointSession,
  ForkSession,
  GetSessionProjection,
  ListSessions,
  LoadSession,
  RepairSession,
  ExportSession,
  RegenerateSessionTitle,
  RemoveSession,
  ReplaceSessionEvents,
  SessionTitleUpdated,
  UpdateSessionMeta,
  type SessionMeta,
} from '../../kernel/contracts/session';
import {
  projectSessionEvents,
  projectSessionSurface,
} from '../../session/projector';
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

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatSessionTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const UI_EVENTS: EventClass[] = [
  TurnStarted,
  TurnQueued,
  TurnDequeued,
  TurnQueueCancelled,
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
  SessionTitleUpdated,
];

export type TuiPageKind =
  | 'help'
  | 'skills'
  | 'cron'
  | 'mcp'
  | 'sessions'
  | 'search'
  | 'reflections'
  | 'context'
  | 'settings'
  | 'log'
  | 'prompt'
  | 'agents'
  | 'mailbox'
  | 'lineage'
  | 'tasks'
  | 'teams'
  | 'task';

export interface TuiSettingField {
  key: string;
  label: string;
  /** Rendered value (secrets are masked). */
  value: string;
  /** Initial text when editing; omitted means use `value`. */
  editValue?: string;
  secret?: boolean;
  /** Click/Enter activates a switch or cycles choices instead of editing text. */
  kind?: 'toggle' | 'choice';
  choices?: string[];
}

export interface TuiQueuedMessage {
  queueId: string;
  sessionId: string;
  agentId: string;
  userInput: string;
  attachments: AttachmentDTO[];
}

export interface TuiPage {
  kind: TuiPageKind;
  title: string;
  body: string;
  /** Structured, directly editable fields (currently settings). */
  fields?: TuiSettingField[];
}

export type TuiSidebarTab = 'overview' | 'files' | 'agents';

/** Sidebar agent row with tree depth for parent/child sessions. */
export interface TuiSidebarAgent extends AgentInfo {
  depth: number;
}

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
  queued: TuiQueuedMessage[];
  cron: CronEventView[];
  reflection: SkillsReflected | null;
  page: TuiPage | null;
  sessionId: string;
  sessionTitle: string;
  sidebarTab: TuiSidebarTab;
  sidebarAgents: TuiSidebarAgent[];
  viewAgentId: string;
  fileTree: FileTreeSnapshot;
  fileRows: TuiFileRow[];
  fileSelected: number;
  thinkingExpanded: boolean;
  skillsExpanded: boolean;
  toolOutputExpanded: boolean;
  /** Turn ids whose tool-call group is manually expanded. */
  toolTurnsExpanded: string[];
  toolTurnsCollapsed: string[];
  sidebarCollapsed: boolean;
  toast: string | null;
  thinkingEnabled: boolean;
  effort: string;
  model: string;
  contextPercent: number | null;
  cacheHitRate: number | null;
  permissionMode: PermissionMode;
}

const HELP_TEXT = [
  '可用命令：',
  '',
  '  /help            显示本帮助',
  '  /skills          技能列表（name / status / pattern）',
  '  /reflect         立即触发一次技能反思',
  '  /merge           合并语义重复的 skills',
  '  /cron            定时任务列表',
  '  /mcp             MCP 服务器状态（/mcp reload 重新加载）',
  '  /sessions        已持久化会话列表',
  '  /reflections     最近的技能反思记录',
  '  /context         当前对话上下文窗口状态',
  '  /settings        模型设置（API Key / 上下文 / 输出 token）',
  '  /set <key> <val> 修改设置，如 /set max_context 1M',
  '  /compact         手动压缩上下文（较早历史摘要为一条消息）',
  '  /fork            从当前会话复制一个新会话',
  '  /log             查看当前会话的事件日志',
  '  /repair          修复当前会话 JSONL 的损坏尾部',
  '  /checkpoint      将当前会话 JSONL 刷盘',
  '  /prompt          查看 system prompt 组装结果',
  '  /reload-prompts  重新读取用户 prompt / AGENTS.md',
  '  /agents [all]    查看 Agent Team 与任务状态（默认仅当前 session）',
  '  /dispose <id>    dispose 一个 worker agent',
  '  /mailbox         查看 Agent mailbox（未读优先）',
  '  /lineage         查看 Agent / Session 谱系树',
  '  /tasks [all]     查看 Agent task 详情（默认仅当前 session）',
  '  /task <id>       查看单个 task 及关联 session 事件',
  '  /team [id]       过滤 Agent / Task / Mailbox / Lineage 的 team（空=全部）',
  '  /teams           查看所有 Team 汇总',
  '  /image <path>   发送图片（png/jpg/gif/webp）',
  '  /file <path>    发送文本或二进制文件',
  '  @<path>         在消息中内联附件路径（支持 "带空格 路径"）',
  '  /effort <level> 设置模型强度：nothinking / low / high / max',
  '  /new             新建会话（清空视图并切换新 session）',
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
  private sessionTitle = '';
  private sessionChoices: SessionMeta[] = [];
  private sessionFilter = '';
  private mcpStatusMessage = '';
  private mcpServers: Record<string, unknown>[] = [];
  private readonly mcpExpanded = new Set<number>();
  private readonly queuedMessages: TuiQueuedMessage[] = [];
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
  private permissionMode: PermissionMode = 'manual';
  private skillsExpanded = false;
  private toolOutputExpanded = false;
  private readonly expandedToolTurns = new Set<string>();
  private readonly collapsedToolTurns = new Set<string>();
  private sidebarCollapsed = true;
  private toast: string | null = null;
  private toastTimer: ReturnType<typeof setTimeout> | null = null;
  private compactRatioForDisplay = 0.8;
  private thinkingEnabled = true;
  private effort: ModelEffort = 'high';
  private model = '';
  private contextWindowTokens = 0;
  private agentsRefreshing = false;
  private teamFilter = '';
  private taskDetailId = '';
  private taskScope: 'session' | 'all' = 'session';
  private sidebarAgents: TuiSidebarAgent[] = [];
  private viewAgentId = 'main';
  private mainSessionId = '';

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
    await bus.subscribe(PermissionModeChanged, (event) => {
      this.permissionMode = event.mode;
      this.refresh();
    });
    for (const type of [AgentSpawned, AgentDisposed, AgentTaskStarted, AgentTaskFinished]) {
      const handler: EventHandler = () => {
        void this.refreshAgentsPage();
        void this.refreshSidebar();
      };
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
        targetAgentId: this.viewAgentId === 'main' ? '' : this.viewAgentId,
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

  /** Load the persisted title for the current session (startup / resume). */
  async refreshSessionTitle(): Promise<void> {
    const bus = this.bus;
    if (!bus || !this.sessionId) return;
    try {
      const projection = await bus.request(
        new GetSessionProjection({ sessionId: this.sessionId }),
      );
      const title = projection.title?.trim() ?? '';
      if (title !== this.sessionTitle) {
        this.sessionTitle = title;
        this.refresh();
      }
    } catch {
      // A title is optional; the header falls back to the session id.
    }
  }

  get currentSessionId(): string {
    return this.sessionId;
  }

  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
    this.sessionTitle = '';
    if (this.viewAgentId === 'main') this.mainSessionId = sessionId;
    this.refresh();
  }

  /** Select an agent in the sidebar; resume it and load its session if needed. */
  async selectAgent(agentId: string): Promise<boolean> {
    const bus = this.bus;
    if (!bus) return false;
    try {
      if (agentId === 'main') {
        // Switching back must restore the full main conversation, not just
        // the turns that happened to arrive while another agent was selected.
        const target = this.mainSessionId;
        if (target) return await this.loadSession(target);
        this.viewAgentId = 'main';
        this.refresh();
        return true;
      }
      const info = this.sidebarAgents.find((agent) => agent.agentId === agentId);
      if (info?.sessionId) {
        await this.loadAgentSession(agentId, info.sessionId);
      } else {
        this.viewAgentId = agentId;
        this.refresh();
      }
      return true;
    } catch (error) {
      this.setToast(
        `✗ 恢复 Agent 失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    }
  }

  /** Load one agent's persisted session and mark its turns with that agent id. */
  /** Dispose one worker agent; the main agent cannot be disposed from the UI. */
  async disposeAgent(agentId: string): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    const id = agentId.trim();
    if (!bus) return { ok: false, message: 'AgentRegistry 不可用' };
    if (!id) return { ok: false, message: '用法：/dispose <agent_id>' };
    if (id === 'main') return { ok: false, message: '不能 dispose main agent' };
    try {
      const disposed = await bus.request(new DisposeAgent({ agentId: id }));
      await this.refreshSidebar();
      if (this.page?.kind === 'agents') await this.refreshAgentsPage();
      return disposed
        ? { ok: true, message: `已 dispose ${id}` }
        : { ok: false, message: `未找到 agent：${id}` };
    } catch (error) {
      return {
        ok: false,
        message: `dispose 失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  async loadAgentSession(agentId: string, sessionId: string): Promise<boolean> {
    const bus = this.bus;
    if (!bus) return false;
    if (this.viewAgentId === 'main' && this.sessionId) {
      this.mainSessionId = this.sessionId;
    }
    try {
      const bundle = await bus.request(new LoadSession({ targetSessionId: sessionId }));
      if (!bundle) {
        this.setToast(`✗ Agent 会话不存在或无法加载：${sessionId}`);
        this.viewAgentId = agentId;
        this.refresh();
        return false;
      }
      const events = bundle.events ?? [];
      await bus.request(
        new ReplaceSessionEvents({ sessionId: bundle.sessionId, events }),
      );
      const messages = projectSessionEvents(events);
      const surface = projectSessionSurface(events);
      this.viewState.loadHistory(messages, events, surface.turns, agentId);
      this.sessionId = bundle.sessionId;
      this.sessionTitle = bundle.title ?? surface.title ?? '';
      this.viewAgentId = agentId;
      this.page = null;
      this.sessionChoices = [];
      this.refresh();
      return true;
    } catch (error) {
      this.setToast(
        `✗ 加载 Agent 会话失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    }
  }

  /** Load a persisted session into memory and rebuild the chat view. */
  async loadSession(targetSessionId: string): Promise<boolean> {
    const bus = this.bus;
    if (!bus) return false;
    try {
      const bundle = await bus.request(new LoadSession({ targetSessionId }));
      if (!bundle) {
        this.setToast(`✗ 会话不存在或无法加载：${targetSessionId}`);
        return false;
      }
      const sessionId = bundle.sessionId || targetSessionId;
      const agentId = bundle.agentId || 'main';
      if (agentId === 'main') this.mainSessionId = sessionId;
      const events = bundle.events ?? [];
      const messages = projectSessionEvents(events);
      await bus.request(new ReplaceSessionEvents({ sessionId, events }));
      this.sessionId = sessionId;
      this.viewAgentId = agentId;
      const surface = projectSessionSurface(events);
      this.sessionTitle = bundle.title ?? surface.title ?? '';
      this.viewState.loadHistory(messages, events, surface.turns, agentId);
      if (this.viewState.lastUsage) {
        this.publish(
          new SetPromptPressure({
            sessionId,
            promptTokens: this.viewState.lastUsage.promptTokens,
          }),
        );
      }
      if (surface.lastRequestHeader) {
        this.publish(
          new SetRequestHeader({
            sessionId,
            header: surface.lastRequestHeader,
          }),
        );
      }
      this.page = null;
      this.sessionChoices = [];
      this.refresh();
      return true;
    } catch (error) {
      this.setToast(
        `✗ 加载会话失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    }
  }

  /** Show one task detail plus related parent-session events. */
  async showTaskDetail(taskId: string): Promise<boolean> {
    if (!taskId.trim()) {
      this.setToast('用法：/task <taskId>');
      return false;
    }
    this.taskDetailId = taskId.trim();
    await this.showPage('task');
    return true;
  }

  /** Filter team pages; empty string shows all teams. */
  setTeamFilter(teamId: string): void {
    this.teamFilter = teamId.trim();
    if (this.page?.kind === 'agents') void this.refreshAgentsPage();
  }

  getTeamFilter(): string {
    return this.teamFilter;
  }

  /** Restrict task views to the current session (default) or show everything. */
  setTaskScope(scope: 'session' | 'all'): void {
    if (this.taskScope === scope) return;
    this.taskScope = scope;
    if (this.page?.kind === 'agents') void this.refreshAgentsPage();
    else if (this.page?.kind === 'tasks') void this.showPage('tasks');
  }

  getTaskScope(): 'session' | 'all' {
    return this.taskScope;
  }

  private tasksForCurrentSession(tasks: AgentTaskInfo[]): AgentTaskInfo[] {
    if (this.taskScope === 'all' || !this.sessionId) return tasks;
    return tasks.filter(
      (task) =>
        task.parentSessionId === this.sessionId ||
        task.sessionId === this.sessionId,
    );
  }

  /**
   * Sidebar tree: main plus every child session reachable from the current
   * root session. A worker can spawn its own worker, so this walks task
   * parentSessionId → sessionId chains recursively.
   */
  private sidebarAgentsForSession(
    agents: AgentInfo[],
    tasks: AgentTaskInfo[],
  ): TuiSidebarAgent[] {
    const rootSession = this.mainSessionId || this.sessionId;
    if (!rootSession) {
      return agents.map((agent) => ({ ...agent, depth: 0 }));
    }
    const byAgent = new Map(agents.map((agent) => [agent.agentId, agent]));
    const rows: TuiSidebarAgent[] = [];
    const addedAgents = new Set<string>();
    const visitedSessions = new Set<string>([rootSession]);
    const main =
      byAgent.get('main') ??
      agents.find((agent) => agent.sessionId === rootSession);
    if (main) {
      rows.push({ ...main, depth: 0 });
      addedAgents.add(main.agentId);
    }
    const queue: Array<{ sessionId: string; depth: number }> = [
      { sessionId: rootSession, depth: 0 },
    ];
    while (queue.length > 0) {
      const current = queue.shift()!;
      for (const task of tasks) {
        if (task.parentSessionId !== current.sessionId) continue;
        if (!task.sessionId || visitedSessions.has(task.sessionId)) continue;
        const child = byAgent.get(task.agentId) ??
          agents.find((agent) => agent.sessionId === task.sessionId);
        if (child && !addedAgents.has(child.agentId)) {
          rows.push({ ...child, depth: current.depth + 1 });
          addedAgents.add(child.agentId);
        }
        visitedSessions.add(task.sessionId);
        queue.push({ sessionId: task.sessionId, depth: current.depth + 1 });
      }
    }
    return rows;
  }

  /** Fork the current session and load the copy. */
  async forkSession(): Promise<boolean> {
    const bus = this.bus;
    if (!bus) return false;
    try {
      const targetSessionId = `session-fork-${Date.now().toString(36)}`;
      const bundle = await bus.request(
        new ForkSession({
          sourceSessionId: this.sessionId,
          targetSessionId,
        }),
      );
      if (!bundle) return false;
      const events = bundle.events ?? [];
      const messages = projectSessionEvents(events);
      await bus.request(
        new ReplaceSessionEvents({ sessionId: bundle.sessionId, events }),
      );
      const surface = projectSessionSurface(events);
      this.viewState.loadHistory(messages, events, surface.turns);
      this.sessionId = bundle.sessionId;
      this.mainSessionId = bundle.sessionId;
      this.viewAgentId = 'main';
      this.page = null;
      this.sessionChoices = [];
      this.refresh();
      this.setToast(`已 fork 到 ${bundle.sessionId}`);
      return true;
    } catch (error) {
      this.setToast(
        `✗ fork 失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    }
  }

  /** Select the n-th (1-based) session from the `/sessions` page. */
  /** Filter /sessions by title, first message, id, date or time window. */
  setSessionFilter(query: string): void {
    this.sessionFilter = query.trim();
  }

  get currentSessionFilter(): string {
    return this.sessionFilter;
  }

  /** Rename a session by 1-based list index or session id. */
  async renameSession(target: number | string, title: string): Promise<{ ok: boolean; message: string }> {
    const meta = this.resolveSession(target);
    if (!meta) return { ok: false, message: '找不到会话' };
    const clean = title.trim();
    if (!clean) return { ok: false, message: '标题不能为空' };
    const bus = this.bus;
    if (!bus) return { ok: false, message: '会话服务不可用' };
    try {
      const ok = await bus.request(
        new UpdateSessionMeta({ targetSessionId: meta.sessionId, patch: { title: clean } }),
      );
      if (ok && meta.sessionId === this.sessionId) this.sessionTitle = clean;
      if (this.page?.kind === 'sessions') await this.showPage('sessions');
      return { ok, message: ok ? `已重命名：${clean}` : '重命名失败' };
    } catch (error) {
      return { ok: false, message: `重命名失败：${errorMessage(error)}` };
    }
  }

  /** Execute one session-row action. */
  async sessionAction(
    index: number,
    action: 'pin' | 'archive' | 'title' | 'export' | 'delete',
  ): Promise<{ ok: boolean; message: string }> {
    const meta = this.resolveSession(index);
    if (!meta) return { ok: false, message: '找不到会话' };
    const bus = this.bus;
    if (!bus) return { ok: false, message: '会话服务不可用' };
    try {
      if (action === 'pin') {
        await bus.request(
          new UpdateSessionMeta({
            targetSessionId: meta.sessionId,
            patch: { pinned: !meta.pinned },
          }),
        );
        await this.showPage('sessions');
        return { ok: true, message: meta.pinned ? '已取消置顶' : '已置顶' };
      }
      if (action === 'archive') {
        await bus.request(
          new UpdateSessionMeta({
            targetSessionId: meta.sessionId,
            patch: { archived: !meta.archived },
          }),
        );
        await this.showPage('sessions');
        return { ok: true, message: meta.archived ? '已取消归档' : '已归档' };
      }
      if (action === 'title') {
        const result = await bus.request(
          new RegenerateSessionTitle({ targetSessionId: meta.sessionId }),
        );
        if (!result?.title) return { ok: false, message: '标题生成失败' };
        await this.showPage('sessions');
        return { ok: true, message: `标题：${result.title}` };
      }
      if (action === 'export') {
        const result = await bus.request(
          new ExportSession({ targetSessionId: meta.sessionId }),
        );
        if (!result) return { ok: false, message: '导出失败' };
        return { ok: true, message: `已导出：${result.path}` };
      }
      const removed = await bus.request(
        new RemoveSession({ targetSessionId: meta.sessionId }),
      );
      if (!removed) return { ok: false, message: '删除失败' };
      if (meta.sessionId === this.sessionId) await this.newSession();
      else await this.showPage('sessions');
      return { ok: true, message: `已删除会话：${meta.sessionId}` };
    } catch (error) {
      return { ok: false, message: `操作失败：${errorMessage(error)}` };
    }
  }

  /** Export the current session as Markdown. */
  async exportCurrentSession(): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus || !this.sessionId) return { ok: false, message: '当前没有会话' };
    try {
      const result = await bus.request(
        new ExportSession({ targetSessionId: this.sessionId }),
      );
      return result
        ? { ok: true, message: `已导出：${result.path}` }
        : { ok: false, message: '导出失败' };
    } catch (error) {
      return { ok: false, message: `导出失败：${errorMessage(error)}` };
    }
  }

  /** Search user/assistant/tool text inside the current session view. */
  searchCurrentSession(query: string): { ok: boolean; message: string } {
    const needle = query.trim().toLowerCase();
    if (!needle) return { ok: false, message: '用法：/find <关键词>' };
    const matches: string[] = [];
    this.viewState.turns.forEach((turn, turnIndex) => {
      if ((turn.agentId || 'main') !== this.viewAgentId) return;
      const fields: Array<[string, string]> = [
        ['user', turn.userInput],
        ['assistant', turn.content],
        ...turn.tools.map((tool) => [`tool:${tool.name}`, tool.output ?? ''] as [string, string]),
      ];
      for (const [label, text] of fields) {
        const index = text.toLowerCase().indexOf(needle);
        if (index < 0) continue;
        const start = Math.max(0, index - 36);
        const snippet = text
          .slice(start, index + needle.length + 64)
          .replace(/\s+/g, ' ')
          .trim();
        matches.push(
          `${turnIndex + 1}. [${label}] ${start > 0 ? '…' : ''}${snippet}${start + needle.length + 64 < text.length ? '…' : ''}`,
        );
      }
    });
    this.page = {
      kind: 'search',
      title: '会话搜索',
      body:
        `查询：${query}\n匹配：${matches.length} 条\n\n` +
        (matches.length > 0 ? matches.slice(0, 80).join('\n') : '（无匹配）'),
    };
    this.refresh();
    return { ok: true, message: `找到 ${matches.length} 条匹配` };
  }

  private resolveSession(target: number | string): SessionMeta | undefined {
    if (typeof target === 'number') {
      return this.sessionChoices[target - 1];
    }
    const value = target.trim();
    return this.sessionChoices.find(
      (session) => session.sessionId === value || session.title === value,
    );
  }

  async chooseSession(index: number): Promise<boolean> {
    const choice = this.sessionChoices[index - 1];
    if (!choice) {
      this.setToast(`✗ 无效的会话编号：${index}`);
      return false;
    }
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
    if (tab === 'agents') await this.refreshSidebar();
  }

  /** Switch the conversation view to one agent (main or a worker). */
  setViewAgent(agentId: string): void {
    const next = agentId.trim() || 'main';
    if (next === this.viewAgentId) return;
    this.viewAgentId = next;
    this.refresh();
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

  /** Force one reflection pass for the current session. */
  reflectSkills(): { ok: boolean; message: string } {
    const bus = this.bus;
    if (!bus) return { ok: false, message: '技能服务不可用' };
    if (!this.sessionId) return { ok: false, message: '当前没有 session' };
    bus.publish(new ReflectSkills({ sessionId: this.sessionId, force: true }));
    return { ok: true, message: '已触发技能反思，完成后会显示结果' };
  }

  /** Run an explicit skill consolidation pass. */
  async mergeSkills(): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: '技能服务不可用' };
    try {
      const result = await bus.request(new MergeSkills());
      if (this.page?.kind === 'skills') await this.showPage('skills');
      return {
        ok: !result.error,
        message: result.error
          ? `✗ merge 失败：${result.error}`
          : `已合并 ${result.merged} 个 skill · 剩余 ${result.remaining} 个 active`,
      };
    } catch (error) {
      return {
        ok: false,
        message: `✗ merge 失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  /** Expand/collapse the tool-call group of one completed turn. */
  /** Remove one queued user message before it starts executing. */
  cancelQueuedMessage(queueId: string): { ok: boolean; message: string } {
    const bus = this.bus;
    if (!bus) return { ok: false, message: '服务不可用' };
    const index = this.queuedMessages.findIndex((item) => item.queueId === queueId);
    if (index < 0) return { ok: false, message: '排队消息不存在' };
    this.queuedMessages.splice(index, 1);
    bus.publish(
      new CancelQueuedTurn({
        queueId,
        targetAgentId: this.viewAgentId === 'main' ? '' : this.viewAgentId,
        sessionId: this.sessionId,
      }),
    );
    this.refresh();
    return { ok: true, message: '已移除排队消息' };
  }

  /** Re-read mcp.json and reconnect all MCP servers. */
  async reloadMcp(): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: 'MCP 服务不可用' };
    try {
      const result = await bus.request(new ReloadMCP());
      if (this.page?.kind === 'mcp') await this.showPage('mcp');
      await this.refreshSidebar();
      return { ok: result.ok, message: result.message };
    } catch (error) {
      return {
        ok: false,
        message: `MCP reload 失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  /** Cycle permission modes: manual → model → full → read-only. */
  async cyclePermissionMode(): Promise<PermissionMode> {
    const order: PermissionMode[] = ['manual', 'model', 'full', 'read-only'];
    const next = order[(order.indexOf(this.permissionMode) + 1) % order.length] ?? 'manual';
    const bus = this.bus;
    // Update the status bar immediately; the tool gateway applies the mode
    // before the request resolves, so the next tool call already sees it.
    this.permissionMode = next;
    this.refresh();
    if (!bus) return next;
    try {
      this.permissionMode = await bus.request(new SetPermissionMode({ mode: next }));
    } catch {
      this.permissionMode = next;
    }
    this.refresh();
    return this.permissionMode;
  }

  toggleToolTurn(turnId: string, currentlyExpanded?: boolean): boolean {
    if (!turnId) return false;
    const next =
      currentlyExpanded === undefined
        ? !this.expandedToolTurns.has(turnId)
        : !currentlyExpanded;
    if (next) {
      this.expandedToolTurns.add(turnId);
      this.collapsedToolTurns.delete(turnId);
    } else {
      this.expandedToolTurns.delete(turnId);
      this.collapsedToolTurns.add(turnId);
    }
    this.refresh();
    return next;
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

  /** Start a fresh main-agent session and clear the conversation view. */
  async newSession(): Promise<string> {
    const sessionId = `session-${Date.now().toString(36)}`;
    this.sessionId = sessionId;
    this.mainSessionId = sessionId;
    this.sessionTitle = '';
    this.viewAgentId = 'main';
    this.page = null;
    this.sessionChoices = [];
    this.viewState.reset();
    this.queuedMessages.length = 0;
    this.expandedToolTurns.clear();
    this.collapsedToolTurns.clear();
    this.refresh();
    const bus = this.bus;
    if (bus) {
      await bus
        .request(
          new AppendSessionEvents({
            sessionId,
            events: [
              {
                seq: 0,
                type: 'session_created',
                at: new Date().toISOString(),
                data: {},
              },
            ],
          }),
        )
        .catch(() => undefined);
    }
    this.setToast(`已新建会话 ${sessionId}`);
    return sessionId;
  }

  resetView(): void {
    this.viewState.reset();
    this.expandedToolTurns.clear();
    this.collapsedToolTurns.clear();
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

  /** Reload user prompt and AGENTS.md. */
  async reloadPrompts(): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: 'TuiModule 未启动' };
    try {
      const info = await bus.request(new ReloadPrompts());
      return {
        ok: true,
        message: `prompt 已重载（${info.sections.length} 个章节）`,
      };
    } catch (error) {
      return {
        ok: false,
        message: `重载失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  /** Flush the current session JSONL to disk. */
  async checkpointSession(): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: 'TuiModule 未启动' };
    try {
      const result = await bus.request(
        new CheckpointSession({ targetSessionId: this.sessionId }),
      );
      return result.checkpointed
        ? { ok: true, message: `已刷盘（${result.bytes} bytes）` }
        : { ok: false, message: '会话文件不存在' };
    } catch (error) {
      return {
        ok: false,
        message: `刷盘失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  /** Repair a corrupted JSONL session log (truncate at first bad event). */
  async repairSession(): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: 'TuiModule 未启动' };
    try {
      const result = await bus.request(
        new RepairSession({ targetSessionId: this.sessionId }),
      );
      if (!result) return { ok: false, message: '会话不存在或无法修复' };
      return {
        ok: result.repaired,
        message: result.repaired
          ? `已修复：保留 ${result.validEvents} 个事件，丢弃 ${result.droppedEvents} 个损坏事件`
          : `无需修复（${result.validEvents} 个事件均有效）`,
      };
    } catch (error) {
      return {
        ok: false,
        message: `修复失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
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
    } else if (
      normalized === 'compactthreshold' ||
      normalized === 'threshold' ||
      normalized === 'compactratio'
    ) {
      const parsed = Number(value);
      if (!Number.isFinite(parsed) || parsed <= 0 || parsed >= 1) {
        return { ok: false, message: `无效的压缩阈值：${value}（应为 0~1 之间，如 0.8）` };
      }
      patch.compactThresholdRatio = parsed;
    } else if (normalized === 'maxoutput' || normalized === 'output') {
      const parsed = parseTokenCount(value);
      if (parsed === null) return { ok: false, message: `无效的输出 token 数：${value}（支持 64K / 1M）` };
      patch.maxOutputTokens = parsed;
    } else if (normalized === 'imagemax' || normalized === 'image_max') {
      const parsed = Number(value);
      if (!Number.isFinite(parsed) || parsed < 64 || parsed > 8_192) {
        return { ok: false, message: `无效的图片最大边：${value}（64~8192）` };
      }
      patch.imageMaxDimension = Math.floor(parsed);
    } else if (normalized === 'maxiterations') {
      const parsed = Number(value);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 1_000) {
        return { ok: false, message: `无效的 max_iterations：${value}（1~1000 的整数）` };
      }
      patch.maxIterations = parsed;
    } else if (normalized === 'autoreflect') {
      const raw = value.trim().toLowerCase();
      if (!['on', 'off', 'true', 'false', '1', '0'].includes(raw)) {
        return { ok: false, message: `无效的 auto_reflect：${value}（on/off）` };
      }
      patch.autoReflect = ['on', 'true', '1'].includes(raw);
    } else if (
      normalized === 'sessiontitle' ||
      normalized === 'sessiontitlegeneration'
    ) {
      const raw = value.trim().toLowerCase();
      if (!['on', 'off', 'true', 'false', '1', '0'].includes(raw)) {
        return { ok: false, message: `无效的 session_title：${value}（on/off）` };
      }
      patch.sessionTitleGeneration = ['on', 'true', '1'].includes(raw);
    } else if (
      normalized === 'reflectionmintools' ||
      normalized === 'reflectionmintoolcalls'
    ) {
      const parsed = Number(value);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 100) {
        return { ok: false, message: `无效的 reflection_min_tools：${value}（1~100）` };
      }
      patch.reflectionMinTools = parsed;
    } else if (normalized === 'reflectionminwritetools') {
      const parsed = Number(value);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 100) {
        return { ok: false, message: `无效的 reflection_min_write_tools：${value}（1~100）` };
      }
      patch.reflectionMinWriteTools = parsed;
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
    const runtime =
      patch.thinkingEnabled !== undefined ||
      patch.effort !== undefined ||
      patch.maxIterations !== undefined ||
      patch.autoReflect !== undefined ||
      patch.sessionTitleGeneration !== undefined ||
      patch.reflectionMinTools !== undefined ||
      patch.reflectionMinWriteTools !== undefined;
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
    const [catalog, skills, cron, mcp, agents, tasks] = await Promise.all([
      safe(bus.request(new GetToolCatalog()), []),
      safe(bus.request(new ListSkills({ includeDeprecated: false })), []),
      safe(bus.request(new ListCronJobs()), []),
      safe(bus.request(new GetMCPStatus()), { servers: [], statusMessage: '' }),
      safe(bus.request(new ListAgents()), [] as AgentInfo[]),
      safe(bus.request(new ListAgentTasks()), [] as AgentTaskInfo[]),
    ]);
    this.sidebarAgents = this.sidebarAgentsForSession(agents, tasks);
    if (
      this.viewAgentId !== 'main' &&
      !this.sidebarAgents.some((agent) => agent.agentId === this.viewAgentId)
    ) {
      this.viewAgentId = 'main';
    }
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
      search: '会话搜索',
      reflections: '反思记录',
      context: '上下文窗口',
      settings: '模型设置',
      log: '会话事件日志',
      prompt: 'System Prompt',
      agents: 'Agent Team',
      mailbox: 'Agent Mailbox',
      lineage: 'Team / Session Lineage',
      tasks: 'Agent Tasks',
      teams: 'Teams',
      task: 'Task Detail',
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
    let fields: TuiSettingField[] | undefined;
    if (kind === 'settings') {
      try {
        const settings = await this.bus?.request(new GetSettings());
        if (settings) fields = this.buildSettingsFields(settings);
      } catch {
        fields = undefined;
      }
    }
    this.page = { kind, title: titles[kind], body, ...(fields ? { fields } : {}) };
    this.refresh();
  }

  private buildSettingsFields(settings: SettingsSnapshot): TuiSettingField[] {
    const fields: TuiSettingField[] = [
      { key: 'provider', label: 'provider', value: settings.provider },
      { key: 'model', label: 'model', value: settings.model },
      {
        key: 'api_key',
        label: 'api_key',
        value: maskApiKey(settings.apiKey),
        editValue: '',
        secret: true,
      },
      { key: 'base_url', label: 'base_url', value: settings.baseUrl },
      {
        key: 'max_context',
        label: 'max_context',
        value: `${formatTokenCount(settings.maxContextTokens)} (${settings.maxContextTokens.toLocaleString()} tokens)`,
        editValue: formatTokenCount(settings.maxContextTokens),
      },
      {
        key: 'max_output',
        label: 'max_output',
        value: `${formatTokenCount(settings.maxOutputTokens)} (${settings.maxOutputTokens.toLocaleString()} tokens)`,
        editValue: formatTokenCount(settings.maxOutputTokens),
      },
      {
        key: 'compact_ratio',
        label: 'compact_ratio',
        value: String(settings.compactThresholdRatio),
      },
      {
        key: 'thinking',
        label: 'thinking',
        value: settings.thinkingEnabled ? 'on' : 'off',
        kind: 'toggle',
      },
      {
        key: 'effort',
        label: 'effort',
        value: settings.effort,
        kind: 'choice',
        choices: ['low', 'high', 'max'],
      },
      {
        key: 'image_max',
        label: 'image_max',
        value: String(settings.imageMaxDimension),
      },
      {
        key: 'max_iterations',
        label: 'max_iterations',
        value: String(settings.maxIterations),
      },
      {
        key: 'auto_reflect',
        label: 'auto_reflect',
        value: settings.autoReflect ? 'on' : 'off',
        kind: 'toggle',
      },
      {
        key: 'session_title',
        label: 'session_title',
        value: settings.sessionTitleGeneration ? 'on' : 'off',
        kind: 'toggle',
      },
      {
        key: 'reflection_min_tools',
        label: 'reflection_min_tools',
        value: String(settings.reflectionMinTools),
      },
      {
        key: 'reflection_min_write_tools',
        label: 'reflection_min_write_tools',
        value: String(settings.reflectionMinWriteTools),
      },
    ];
    return fields;
  }

  /** Disband one team from the `/teams` page. */
  async disposeTeam(teamId: string): Promise<{ ok: boolean; message: string }> {
    const bus = this.bus;
    if (!bus) return { ok: false, message: 'AgentRegistry 不可用' };
    try {
      const result = await bus.request(new DisposeTeam({ teamId }));
      await this.showPage('teams');
      return { ok: result.ok, message: result.message };
    } catch (error) {
      return {
        ok: false,
        message: `解散失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
  }

  /** Enable or disable one MCP server from the `/mcp` page. */
  async setMcpServerEnabled(
    index: number,
    enabled: boolean,
  ): Promise<{ ok: boolean; message: string }> {
    if (this.page?.kind !== 'mcp') return { ok: false, message: '请先打开 /mcp 页面' };
    if (index < 1 || index > this.mcpServers.length) {
      return { ok: false, message: `无效的服务器编号：${index}` };
    }
    const bus = this.bus;
    if (!bus) return { ok: false, message: 'MCP 服务不可用' };
    const name = String(this.mcpServers[index - 1]?.name ?? '');
    if (!name) return { ok: false, message: '服务器名称为空' };
    try {
      const result = await bus.request(new SetMCPServerEnabled({ name, enabled }));
      await this.showPage('mcp');
      return { ok: result.ok, message: result.message };
    } catch (error) {
      return {
        ok: false,
        message: `操作失败：${error instanceof Error ? error.message : String(error)}`,
      };
    }
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
      const enabled = server.enabled !== false;
      lines.push(
        `${position}. ${expanded ? '▼' : '▶'} ${String(server.name ?? '')} [${String(server.status ?? '')}] ` +
          `${enabled ? '[ ON ]' : '[ OFF ]'} transport=${String(server.transport ?? '')} tools=${toolCount}` +
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
    lines.push('', '点击 [ ON ] / [ OFF ] 切换 · 数字展开/收起 · /mcp reload 重新加载');
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

  private async refreshAgentsPage(): Promise<void> {
    if (this.agentsRefreshing || this.page?.kind !== 'agents') return;
    this.agentsRefreshing = true;
    try {
      const body = await this.loadPageBody('agents');
      if (this.page?.kind === 'agents') {
        this.page = { kind: 'agents', title: 'Agent Team', body };
        this.refresh();
      }
    } catch {
      // Keep the last page if a transient registry lookup fails.
    } finally {
      this.agentsRefreshing = false;
    }
  }

  private async loadPageBody(kind: TuiPageKind): Promise<string> {
    const bus = this.requireBusForPage();
    if (kind === 'help') return HELP_TEXT;

    if (kind === 'log') {
      const bundle = await bus
        .request(new LoadSession({ targetSessionId: this.sessionId }))
        .catch(() => null);
      const events = bundle?.events ?? [];
      if (events.length === 0) return '当前会话还没有事件。';
      const lines = [
        `会话：${this.sessionId || '-'} · 共 ${events.length} 个事件`,
        '',
      ];
      for (const event of events.slice(-200)) {
        const data = JSON.stringify(event.data ?? {}, (_key, value) =>
          typeof value === 'string' && value.startsWith('data:')
            ? '[inline-image]'
            : value,
        );
        lines.push(
          `#${String(event.seq).padStart(4, ' ')} ${formatSessionTime(event.at)} ${event.type}`,
        );
        lines.push(
          `      ${(data ?? '').slice(0, 180)}${(data ?? '').length > 180 ? '…' : ''}`,
        );
      }
      return lines.join('\n');
    }

    if (kind === 'agents') {
      const [agents, tasks] = await Promise.all([
        bus.request(new ListAgents({ teamId: this.teamFilter })).catch(
          () => [] as AgentInfo[],
        ),
        bus.request(new ListAgentTasks()).catch(() => [] as AgentTaskInfo[]),
      ]);
      const scopedTasks = this.tasksForCurrentSession(tasks);
      if (agents.length === 0 && scopedTasks.length === 0) {
        return this.taskScope === 'all'
          ? '当前没有注册的 Agent。'
          : '当前 session 没有相关 Agent/task（/agents all 查看全部）。';
      }
      const lines = ['Agents:'];
      for (const agent of agents) {
        lines.push(
          `- ${agent.agentId} [${agent.status}] role=${agent.role} team=${agent.teamId || '-'} parent=${agent.parentAgentId || '-'}`,
        );
      }
      lines.push(
        '',
        this.taskScope === 'all' ? 'Tasks (all):' : 'Tasks (current session):',
      );
      if (scopedTasks.length === 0) {
        lines.push(
          this.taskScope === 'all'
            ? '  （无任务）'
            : '  （当前 session 无任务；/agents all 查看全部）',
        );
      } else {
        for (const task of scopedTasks.slice(-20)) {
          lines.push(
            `- ${task.taskId} [${task.status}] ${task.agentId}: ${task.result || task.error || task.prompt}`,
          );
        }
      }
      return lines.join('\n');
    }

    if (kind === 'task') {
      const tasks = await bus.request(new ListAgentTasks()).catch(() => [] as AgentTaskInfo[]);
      const task = tasks.find((item) => item.taskId === this.taskDetailId);
      if (!task) return `未找到 task：${this.taskDetailId}`;
      const lines = [
        `task: ${task.taskId}`,
        `status: ${task.status}`,
        `agent: ${task.agentId}`,
        `team: ${task.teamId ?? '-'}`,
        `parent_session: ${task.parentSessionId ?? '-'}`,
        `worker_session: ${task.sessionId}`,
        `priority: ${task.priority ?? 0}`,
        `failure_reason: ${task.failureReason || '-'}`,
        `started_at: ${task.startedAt}`,
        `finished_at: ${task.finishedAt ?? '-'}`,
        `prompt: ${task.prompt}`,
        `result: ${task.result || '-'}`,
        `error: ${task.error || '-'}`,
      ];
      if (task.parentSessionId) {
        try {
          const projection = await bus.request(
            new GetSessionProjection({ sessionId: task.parentSessionId }),
          );
          const related = projection.events.filter(
            (event) => event.data?.taskId === task.taskId,
          );
          lines.push('', 'Session events:');
          if (related.length === 0) lines.push('  （无）');
          for (const event of related) {
            lines.push(`- seq=${event.seq} ${event.type} at=${event.at}`);
          }
        } catch {
          lines.push('', 'Session events: （不可用）');
        }
      }
      return lines.join('\n');
    }

    if (kind === 'teams') {
      const [agents, tasks] = await Promise.all([
        bus.request(new ListAgents()).catch(() => [] as AgentInfo[]),
        bus.request(new ListAgentTasks()).catch(() => [] as AgentTaskInfo[]),
      ]);
      const teams = new Map<
        string,
        { agents: number; running: number; tasks: number; taskRunning: number; completed: number }
      >();
      for (const agent of agents) {
        const key = agent.teamId || '(none)';
        const entry = teams.get(key) ?? {
          agents: 0,
          running: 0,
          tasks: 0,
          taskRunning: 0,
          completed: 0,
        };
        entry.agents += 1;
        if (agent.status === 'running') entry.running += 1;
        teams.set(key, entry);
      }
      for (const task of tasks) {
        const key = task.teamId || '(none)';
        const entry = teams.get(key) ?? {
          agents: 0,
          running: 0,
          tasks: 0,
          taskRunning: 0,
          completed: 0,
        };
        entry.tasks += 1;
        if (task.status === 'running') entry.taskRunning += 1;
        if (task.status === 'completed') entry.completed += 1;
        teams.set(key, entry);
      }
      if (teams.size === 0) return '当前没有 Team。';
      return [
        ...[...teams.entries()].map(
          ([teamId, entry]) =>
            `- ${teamId}: agents=${entry.agents} running=${entry.running} tasks=${entry.tasks} task_running=${entry.taskRunning} completed=${entry.completed} [解散]`,
        ),
        '',
        '点击 [解散] 两次确认 · 包含主 Agent 的 Team 不能解散',
      ].join('\n');
    }

    if (kind === 'tasks') {
      let tasks: AgentTaskInfo[] = [];
      try {
        tasks = await bus.request(new ListAgentTasks());
      } catch {
        return 'AgentRegistry 不可用。';
      }
      if (this.teamFilter) {
        tasks = tasks.filter((task) => task.teamId === this.teamFilter);
      }
      tasks = this.tasksForCurrentSession(tasks);
      if (tasks.length === 0) {
        return this.taskScope === 'all'
          ? '当前没有 task。'
          : '当前 session 没有 task（/tasks all 查看全部）。';
      }
      return tasks
        .slice(-30)
        .map((task) => {
          const timestamps =
            task.finishedAt !== null
              ? `${task.startedAt} → ${task.finishedAt}`
              : `${task.startedAt} → running`;
          return [
            `- ${task.taskId} [${task.status}] agent=${task.agentId}`,
            `    parent=${task.parentSessionId ?? '-'} worker=${task.sessionId}`,
            `    ${timestamps}`,
            `    prompt: ${task.prompt}`,
            task.result ? `    result: ${task.result}` : '',
            task.failureReason ? `    reason: ${task.failureReason}` : '',
            task.error ? `    error: ${task.error}` : '',
          ]
            .filter(Boolean)
            .join('\n');
        })
        .join('\n\n');
    }

    if (kind === 'lineage') {
      const [agents, sessions] = await Promise.all([
        bus.request(new ListAgents({ teamId: this.teamFilter })).catch(
          () => [] as AgentInfo[],
        ),
        bus.request(new ListSessions()).catch(() => [] as SessionMeta[]),
      ]);
      const lines: string[] = ['Agents:'];
      const agentIds = new Set(agents.map((agent) => agent.agentId));
      const visibleSessions = this.teamFilter
        ? sessions.filter(
            (session) => session.agentId !== undefined && agentIds.has(session.agentId),
          )
        : sessions;
      const agentChildren = new Map<string, AgentInfo[]>();
      for (const agent of agents) {
        const parent = agent.parentAgentId || '';
        const list = agentChildren.get(parent) ?? [];
        list.push(agent);
        agentChildren.set(parent, list);
      }
      const agentRoots = agents.filter(
        (agent) => !agent.parentAgentId || !agentIds.has(agent.parentAgentId),
      );
      const walkAgent = (agent: AgentInfo, depth: number): void => {
        lines.push(
          `${'  '.repeat(depth)}- ${agent.agentId} [${agent.role}] team=${agent.teamId || '-'} session=${agent.sessionId || '-'}`,
        );
        for (const child of agentChildren.get(agent.agentId) ?? []) {
          walkAgent(child, depth + 1);
        }
      };
      for (const root of agentRoots) walkAgent(root, 0);
      if (agents.length === 0) lines.push('  （无）');

      lines.push('', 'Sessions:');
      const sessionIds = new Set(visibleSessions.map((session) => session.sessionId));
      const sessionChildren = new Map<string, SessionMeta[]>();
      for (const session of visibleSessions) {
        const parent = session.parentSessionId || '';
        const list = sessionChildren.get(parent) ?? [];
        list.push(session);
        sessionChildren.set(parent, list);
      }
      const sessionRoots = visibleSessions.filter(
        (session) =>
          !session.parentSessionId || !sessionIds.has(session.parentSessionId),
      );
      const walkSession = (session: SessionMeta, depth: number): void => {
        const agent = session.agentId ? ` agent=${session.agentId}` : '';
        const forked =
          session.parentSessionId && session.forkBoundarySeq !== undefined
            ? ` @${session.forkBoundarySeq}`
            : '';
        lines.push(
          `${'  '.repeat(depth)}- ${session.sessionId}${forked}${agent} (${session.messageCount} msgs)`,
        );
        for (const child of sessionChildren.get(session.sessionId) ?? []) {
          walkSession(child, depth + 1);
        }
      };
      for (const root of sessionRoots) walkSession(root, 0);
      if (visibleSessions.length === 0) lines.push('  （无）');
      return lines.join('\n');
    }

    if (kind === 'mailbox') {
      let agents: AgentInfo[];
      try {
        agents = await bus.request(new ListAgents({ teamId: this.teamFilter }));
      } catch {
        return 'AgentRegistry 不可用。';
      }
      if (agents.length === 0) return '当前没有注册的 Agent。';
      const lines: string[] = [];
      for (const agent of agents) {
        let messages: Array<{ read?: boolean; fromAgentId: string; content: string }> = [];
        try {
          messages = await bus.request(
            new ListAgentMessages({
              agentId: agent.agentId,
              limit: 20,
              sessionId: this.sessionId,
            }),
          );
        } catch {
          continue;
        }
        const unread = messages.filter((message) => !message.read).length;
        lines.push(`- ${agent.agentId} (${unread} unread / ${messages.length} msgs)`);
        for (const message of messages.slice(-5)) {
          lines.push(
            `    ${message.read ? '✓' : '•'} ${message.fromAgentId}: ${message.content}`,
          );
        }
      }
      return lines.length > 0 ? lines.join('\n') : 'Mailbox 为空。';
    }

    if (kind === 'prompt') {
      const info = await bus.request(new GetPromptInfo());
      const lines = [
        `用户 prompt：${info.userPromptPath}`,
        `AGENTS.md：${info.agentsPath ?? '未找到'}`,
        '',
        '章节：',
        ...info.sections.map(
          (section) =>
            `- ${section.id}${section.title ? ` (${section.title})` : ''} order=${section.order ?? 100} source=${section.source ?? '-'}`,
        ),
        '',
        '组装结果（前 2000 字符）：',
        info.prompt.slice(0, 2_000),
      ];
      return lines.join('\n');
    }

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
          `image_max     ${settings.imageMaxDimension} px（请求时图片最大边）`,
          `max_iterations ${settings.maxIterations}（单轮最多工具循环次数）`,
          `auto_reflect   ${settings.autoReflect ? 'on' : 'off'}（turn 完成后自动反思）`,
          `session_title  ${settings.sessionTitleGeneration ? 'on' : 'off'}（首轮对话后自动生成会话标题）`,
          `reflection_min_tools ${settings.reflectionMinTools}（自动反思所需工具调用数）`,
          `reflection_min_write_tools ${settings.reflectionMinWriteTools}（含 write/edit 时的工具调用数）`,
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
      const [projection, settings] = await Promise.all([
        bus.request(new GetSessionProjection({ sessionId })),
        bus.request(new GetSettings({ sessionId })).catch(() => null),
      ]);
      const messages = projection.messages;
      if (settings) this.compactRatioForDisplay = settings.compactThresholdRatio;
      const usage = this.viewState.lastUsage;
      const totals = this.viewState.sessionUsage;
      const capacity = settings?.maxContextTokens ?? 0;
      const latestPrompt = usage?.promptTokens ?? 0;
      const contextRatio = capacity > 0 ? Math.min(1, latestPrompt / capacity) : 0;
      const cacheTotal = totals.cacheHitTokens + totals.cacheMissTokens;
      const hitRate =
        cacheTotal > 0 ? (totals.cacheHitTokens / cacheTotal) * 100 : null;
      const barFor = (ratio: number, width = 24): string => {
        const filled = Math.max(0, Math.min(width, Math.round(ratio * width)));
        return `${'█'.repeat(filled)}${'░'.repeat(width - filled)}`;
      };
      const lines = [
        '◆ 用量概览',
        `  会话        ${sessionId || '-'}`,
        '',
        usage && capacity > 0
          ? `  ${barFor(contextRatio)}  ${latestPrompt.toLocaleString()} / ${capacity.toLocaleString()} tokens  ${Math.round(contextRatio * 100)}%`
          : '  ░░░░░░░░░░░░░░░░░░░░░░░░  waiting for provider usage',
        '',
        '◆ 本次会话',
        `  输入        ${totals.inputTokens.toLocaleString()}`,
        `  输出        ${totals.outputTokens.toLocaleString()}`,
        `  请求        ${totals.requests}`,
        '',
        '◆ 缓存',
        cacheTotal > 0
          ? `  命中        ${totals.cacheHitTokens.toLocaleString()}`
          : '  命中        -',
        cacheTotal > 0
          ? `  未命中      ${totals.cacheMissTokens.toLocaleString()}`
          : '  未命中      -',
        cacheTotal > 0
          ? `  命中率      ${hitRate?.toFixed(1)}%`
          : '  命中率      -',
        cacheTotal > 0 && hitRate !== null
          ? `  ${barFor(hitRate / 100)}`
          : '  provider 未上报（DeepSeek 缓存字段）',
        '',
        '◆ 最近请求',
        usage
          ? `  prompt ${usage.promptTokens.toLocaleString()} · completion ${usage.completionTokens.toLocaleString()} · total ${usage.totalTokens.toLocaleString()} · cache ${usage.cacheHitTokens.toLocaleString()}/${(usage.cacheHitTokens + usage.cacheMissTokens).toLocaleString()}`
          : '  暂无 provider 上报',
        '',
        '◆ 历史',
        `  ${messages.length} 条消息（无数量上限）`,
        `  压缩阈值 ${Math.round((this.compactRatioForDisplay ?? 0.8) * 100)}% · /compact 手动压缩`,
      ];
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

    let sessions = await bus.request(new ListSessions());
    sessions = sessions.filter(
      (session) => !session.agentId || session.agentId === 'main',
    );
    if (this.teamFilter) {
      const agents = await bus
        .request(new ListAgents({ teamId: this.teamFilter }))
        .catch(() => [] as AgentInfo[]);
      const ids = new Set(agents.map((agent) => agent.agentId));
      sessions = sessions.filter(
        (session) => !session.agentId || ids.has(session.agentId),
      );
    }

    const rawFilter = this.sessionFilter.trim();
    const lower = rawFilter.toLowerCase();
    const showArchived = lower === 'archived';
    const showAll = lower === 'all';
    const textQuery = showArchived || showAll ? '' : lower;
    const now = Date.now();
    const matchesQuery = (session: SessionMeta): boolean => {
      if (!textQuery) return true;
      const when = session.updatedAt ?? session.createdAt;
      const date = new Date(when);
      if (textQuery === 'today') {
        return date.toDateString() === new Date().toDateString();
      }
      const days = textQuery.match(/^(\d+)d$/);
      if (days) {
        return now - date.getTime() <= Number(days[1]) * 86_400_000;
      }
      const text = [
        session.sessionId,
        session.title ?? '',
        session.firstMessage,
        formatSessionTime(when),
        when,
      ]
        .join(' ')
        .toLowerCase();
      return text.includes(textQuery);
    };
    let filtered = sessions.filter((session) => {
      if (showArchived) return Boolean(session.archived) && matchesQuery(session);
      if (showAll) return matchesQuery(session);
      return !session.archived && matchesQuery(session);
    });
    filtered = [...filtered].sort((a, b) => {
      const pinDiff = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
      if (pinDiff !== 0) return pinDiff;
      const archiveDiff = Number(Boolean(a.archived)) - Number(Boolean(b.archived));
      if (archiveDiff !== 0) return archiveDiff;
      return (b.updatedAt ?? b.createdAt).localeCompare(a.updatedAt ?? a.createdAt);
    });
    this.sessionChoices = filtered;
    const scope = rawFilter
      ? `筛选：${rawFilter} · ${filtered.length} 个会话`
      : `共 ${filtered.length} 个会话（置顶优先 · 已归档默认隐藏）`;
    if (filtered.length === 0) {
      return `${scope}\n\n${rawFilter ? '没有匹配的会话。' : '暂无已保存的会话。'}`;
    }
    return [
      scope,
      '',
      ...filtered.map((session, index) => {
        const when = formatSessionTime(session.updatedAt ?? session.createdAt);
        const message = session.firstMessage.replace(/\s+/g, ' ').trim();
        const title = session.title?.trim() || session.sessionId;
        const flags = `${session.pinned ? ' · pinned' : ''}${session.archived ? ' · archived' : ''}`;
        const actions =
          `${session.pinned ? '[取消置顶]' : '[置顶]'} ` +
          `${session.archived ? '[恢复]' : '[归档]'} [标题] [导出] [删除]`;
        return [
          `${String(index + 1).padStart(2, ' ')}. ${title}${flags}\t${actions}`,
          `    ${title === session.sessionId ? '' : `${session.sessionId} · `}${when} · ${session.messageCount} msgs`,
          session.parentSessionId
            ? `    ↳ fork of ${session.parentSessionId}${session.forkBoundarySeq !== undefined ? ` @ seq ${session.forkBoundarySeq}` : ''}`
            : '',
          message ? `    ${message}` : '',
          '',
        ]
          .filter(Boolean)
          .join('\n');
      }),
      '点击按钮操作 · 数字加载 · /rename <n> <标题> · /find <关键词> 会话内搜索',
    ].join('\n');
  }

  // ── internals ──────────────────────────────────────────────────────────

  private handle(event: Event): void {
    if (event instanceof SessionTitleUpdated) {
      if (!event.sessionId || event.sessionId === this.sessionId) {
        this.sessionTitle = event.title;
      }
      if (this.page?.kind === 'sessions') void this.showPage('sessions');
      this.refresh();
      return;
    }
    if (event instanceof TurnQueued) {
      if (!this.queuedMessages.some((item) => item.queueId === event.queueId)) {
        this.queuedMessages.push({
          queueId: event.queueId,
          sessionId: event.sessionId,
          agentId: event.agentId || 'main',
          userInput: event.userInput,
          attachments: event.attachments,
        });
      }
    } else if (event instanceof TurnDequeued || event instanceof TurnQueueCancelled) {
      const index = this.queuedMessages.findIndex((item) => item.queueId === event.queueId);
      if (index >= 0) this.queuedMessages.splice(index, 1);
    } else {
      this.viewState.apply(event);
    }
    this.refresh();
  }

  private buildSnapshot(): TuiSnapshot {
    return {
      revision: this.revision,
      turns: this.viewState.turns.filter(
        (turn) => (turn.agentId || 'main') === this.viewAgentId,
      ),
      streaming: this.viewState.streaming.filter(
        (turn) => (turn.agentId || 'main') === this.viewAgentId,
      ),
      approvals: this.viewState.pendingApprovals,
      queued: this.queuedMessages.filter(
        (item) =>
          (!item.sessionId || item.sessionId === this.sessionId) &&
          item.agentId === this.viewAgentId,
      ),
      cron: this.viewState.cron,
      reflection: this.viewState.lastReflection,
      page: this.page,
      sessionId: this.sessionId,
      sessionTitle: this.sessionTitle,
      sidebar: this.sidebar,
      sidebarTab: this.sidebarTab,
      sidebarAgents: this.sidebarAgents,
      viewAgentId: this.viewAgentId,
      fileTree: this.fileTree,
      fileRows: this.fileRows,
      fileSelected: this.fileSelected,
      thinkingExpanded: this.thinkingExpanded,
      skillsExpanded: this.skillsExpanded,
      toolOutputExpanded: this.toolOutputExpanded,
      toolTurnsExpanded: [...this.expandedToolTurns],
      toolTurnsCollapsed: [...this.collapsedToolTurns],
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
      cacheHitRate: (() => {
        const totals = this.viewState.sessionUsage;
        const total = totals.cacheHitTokens + totals.cacheMissTokens;
        return total > 0 ? (totals.cacheHitTokens / total) * 100 : null;
      })(),
      permissionMode: this.permissionMode,
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