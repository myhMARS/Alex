/** AgentRegistryModule — in-process lifecycle and messaging for agent teams. */

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import type { ChatModel } from '../llm/client';
import { RegisterTool, SetAgentPermissions } from '../kernel/contracts/tools';
import { AgentTaskStore } from './store';
import { AgentStore } from './agent-store';
import { AgentMailbox } from './mailbox';
import { TurnCompleted, TurnFailed, UserTurnRequested } from '../kernel/contracts/chat';
import {
  AppendSessionEvents,
  GetSessionProjection,
  LoadSession,
  RegenerateSessionTitle,
  ReplaceSessionEvents,
  SessionTitleUpdated,
} from '../kernel/contracts/session';
import { AgentModule, type AgentModuleOptions } from '../modules/agent/module';
import { generateSessionTitle } from '../session/title';
import {
  AckAgentMessages,
  AgentDisposed,
  AgentSpawned,
  AgentTaskFinished,
  AgentTaskStarted,
  CancelAgentTask,
  DisposeAgent,
  DisposeTeam,
  GetAgent,
  ResumeAgent,
  GetAgentMailboxStatus,
  GetTeamLimits,
  ListAgents,
  ListAgentMessages,
  ListAgentTasks,
  SendAgentMessage,
  SetTeamLimits,
  SpawnAgent,
  type AgentInfo,
  type AgentSpawnSpec,
  type AgentTaskInfo,
  type AgentMailboxEntry,
  type DisposeTeamResult,
  type TeamLimits,
} from '../kernel/contracts/agents';
import {
  AgentTeamMemberJoined,
  AgentTeamMemberLeft,
  AgentTeamMessage,
  AgentTeamMessageConsumed,
} from '../kernel/contracts/team';

export interface AgentRegistryModuleOptions {
  model?: ChatModel;
  createAgent?: (spec: AgentSpawnSpec) => AgentModule;
  /** Register team tools into ToolsModule (spawn/list/message/dispose). */
  registerTools?: boolean;
  /** Default parent agent id written into spawned workers. */
  defaultAgentId?: string;
  defaultTeamId?: string;
  taskTimeoutMs?: number;
  /** Persist task history to JSONL; enabled automatically when tasksPath is set. */
  persistTasks?: boolean;
  tasksPath?: string;
  /** Default permissions applied to worker agents. */
  workerPermissions?: { allowed?: string[]; denied?: string[] };
  /** Persist delivered agent messages to JSONL. */
  persistMailbox?: boolean;
  mailboxPath?: string;
  /** Optional worker-specific model / system prompt. */
  workerModel?: ChatModel;
  workerSystemPrompt?: string;
  /** Maximum tool-call iterations per agent turn. */
  maxIterations?: number;
  /** Maximum number of agents per team (attached agents count). */
  maxAgentsPerTeam?: number;
  /** Maximum concurrently running tasks per team (0/undefined = unlimited). */
  maxConcurrentTasksPerTeam?: number;
  /** Automatically ack a message once the target agent finishes it (default true). */
  autoAckMailbox?: boolean;
  /** Replay attempts before a message becomes dead-letter (default 3). */
  mailboxMaxAttempts?: number;
  /** Persist agent metadata for resume across restarts. */
  persistAgents?: boolean;
  agentsPath?: string;
}

interface RegisteredAgent {
  agent: AgentModule;
  info: AgentInfo;
}

interface TaskRecord {
  info: AgentTaskInfo;
  promise: Promise<string>;
}

function messageText(message: { content?: unknown } | undefined): string {
  if (!message) return '';
  const content = message.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part) =>
        part && typeof part === 'object' && 'text' in part
          ? String((part as { text?: unknown }).text ?? '')
          : '',
      )
      .join(' ');
  }
  return content == null ? '' : String(content);
}

let counter = 0;

export class AgentRegistryModule implements Module {
  readonly name = 'agents';
  readonly dependencies: readonly string[] = ['tools', 'store'];
  private readonly agents = new Map<string, RegisteredAgent>();
  private readonly options: AgentRegistryModuleOptions;
  private readonly taskQueues = new Map<string, Promise<void>>();
  private readonly tasks = new Map<string, TaskRecord>();
  private readonly disposers: Array<() => Promise<void>> = [];
  private readonly teamLimits = new Map<string, TeamLimits>();
  private readonly teamSlots = new Map<
    string,
    {
      active: number;
      waiters: Array<{ priority: number; resolve: () => void }>;
    }
  >();
  private readonly taskStore: AgentTaskStore | null;
  private readonly agentStore: AgentStore | null;
  private readonly resumable = new Map<string, AgentInfo>();
  private readonly disposing = new Map<string, Promise<boolean>>();
  private readonly mailbox: AgentMailbox | null;
  private taskCounter = 0;
  private bus: AsyncEventBus | null = null;

  constructor(options: AgentRegistryModuleOptions = {}) {
    this.options = options;
    this.taskStore =
      options.tasksPath !== undefined
        ? new AgentTaskStore(options.tasksPath)
        : options.persistTasks
          ? new AgentTaskStore()
          : null;
    this.mailbox =
      options.mailboxPath !== undefined
        ? new AgentMailbox(options.mailboxPath)
        : options.persistMailbox
          ? new AgentMailbox()
          : null;
    this.agentStore =
      options.agentsPath !== undefined
        ? new AgentStore(options.agentsPath)
        : options.persistAgents
          ? new AgentStore()
          : null;
  }

  /** Adopt an externally constructed/started agent (e.g. the main agent). */
  attachAgent(agent: AgentModule, spec: AgentSpawnSpec = {}): AgentInfo {
    const agentId = spec.agentId || agent.id;
    if (!agentId) throw new Error('attached agent requires an id');
    if (this.agents.has(agentId)) throw new Error(`agent already exists: ${agentId}`);
    const info: AgentInfo = {
      agentId,
      parentAgentId: spec.parentAgentId ?? agent.parent,
      teamId: spec.teamId ?? agent.team,
      sessionId: spec.sessionId ?? '',
      role: spec.role ?? 'lead',
      status: 'idle',
    };
    this.agents.set(agentId, { agent, info });
    void this.agentStore?.save(info);
    return { ...info };
  }

  private publishAttached(): void {
    const bus = this.bus;
    if (!bus) return;
    for (const { info } of this.agents.values()) {
      bus.publish(new AgentSpawned({ agentId: info.agentId, teamId: info.teamId, info }));
      bus.publish(
        new AgentTeamMemberJoined({
          teamId: info.teamId,
          agentId: info.agentId,
          member: {
            agentId: info.agentId,
            role: info.role,
            sessionId: info.sessionId,
            parentAgentId: info.parentAgentId,
          },
        }),
      );
    }
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    bus.provide(SpawnAgent, (req) => this.spawn(req.spec));
    bus.provide(ListAgents, (req) =>
      this.list(req.teamId || undefined, req.includeResumable),
    );
    bus.provide(ResumeAgent, (req) =>
      this.resume(req.agentId, req.sessionId || undefined),
    );
    bus.provide(GetAgent, (req) => this.agents.get(req.agentId)?.info ?? null);
    bus.provide(DisposeAgent, (req) => this.dispose(req.agentId));
    bus.provide(DisposeTeam, (req) => this.disposeTeam(req.teamId));
    bus.provide(RegenerateSessionTitle, async (req) => {
      const sessionId = req.targetSessionId || req.sessionId;
      const model = this.options.model;
      if (!sessionId || !model) return null;
      const projection = await bus
        .request(new GetSessionProjection({ sessionId }))
        .catch(() => null);
      let messages = projection?.messages ?? [];
      if (!messages.some((message) => message.role === 'user')) {
        const bundle = await bus
          .request(new LoadSession({ targetSessionId: sessionId }))
          .catch(() => null);
        messages = bundle?.messages ?? messages;
      }
      const user = [...messages].reverse().find((message) => message.role === 'user');
      const assistant = [...messages]
        .reverse()
        .find((message) => message.role === 'assistant');
      const userText = messageText(user);
      if (!userText) return null;
      const title = await generateSessionTitle(model, userText, messageText(assistant));
      if (!title) return null;
      await bus.request(
        new AppendSessionEvents({
          sessionId,
          events: [
            {
              seq: 0,
              type: 'session_title',
              at: new Date().toISOString(),
              data: { title, auto: false },
            },
          ],
        }),
      );
      bus.publish(
        new SessionTitleUpdated({
          sessionId,
          agentId: this.options.defaultAgentId,
          title,
        }),
      );
      return { title };
    });
    bus.provide(CancelAgentTask, (req) => {
      const record = this.tasks.get(req.taskId);
      if (!record || record.info.status !== 'running') return false;
      record.info.status = 'cancelled';
      record.info.failureReason = 'cancelled';
      record.info.finishedAt = Date.now();
      this.persistTask('finished', record.info);
      this.appendTaskEvent(record.info.parentSessionId, 'agent_task_finished', record.info);
      this.agents.get(record.info.agentId)?.agent.cancel(record.info.sessionId);
      this.cancelSubtree(record.info.agentId);
      this.bus?.publish(
        new AgentTaskFinished({
          agentId: record.info.agentId,
          sessionId: record.info.sessionId,
          teamId: this.agents.get(record.info.agentId)?.info.teamId,
          task: { ...record.info },
        }),
      );
      return true;
    });
    bus.provide(ListAgentTasks, () =>
      [...this.tasks.values()].map(({ info }) => ({ ...info })),
    );
    bus.provide(ListAgentMessages, (req) =>
      this.mailbox?.read(
        req.targetAgentId,
        req.limit,
        req.unreadOnly,
        req.sessionId || undefined,
      ) ?? [],
    );
    bus.provide(AckAgentMessages, async (req) => ({
      acked:
        (await this.mailbox?.ack(
          req.targetAgentId,
          req.messageIds,
          req.sessionId || undefined,
        )) ?? 0,
    }));
    bus.provide(
      GetAgentMailboxStatus,
      (req) =>
        this.mailbox?.status(
          req.targetAgentId,
          this.options.mailboxMaxAttempts ?? 3,
          req.sessionId || undefined,
        ) ?? {
          total: 0,
          read: 0,
          unread: 0,
          dead: 0,
        },
    );
    bus.provide(SetTeamLimits, (req) => {
      if (!req.limits.teamId) throw new Error('teamId is required');
      this.teamLimits.set(req.limits.teamId, { ...req.limits });
      return { ...req.limits };
    });
    bus.provide(GetTeamLimits, () =>
      [...this.teamLimits.values()].map((limits) => ({ ...limits })),
    );
    bus.provide(SendAgentMessage, (req) => {
      if (!this.agents.has(req.toAgentId)) {
        throw new Error(`unknown agent: ${req.toAgentId}`);
      }
      return this.sendMessage(
        req.fromAgentId,
        req.toAgentId,
        req.content,
        req.replyTo,
        req.sessionId || undefined,
      );
    });
    if (this.options.registerTools !== false) await this.registerTeamTools();
    await this.loadTasks();
    await this.mailbox?.load();
    await this.loadAgents();
    if (this.options.autoAckMailbox !== false) {
      this.disposers.push(
        await bus.subscribe(AgentTeamMessageConsumed, (event) => {
          if (!event.messageId || !event.agentId) return;
          if (event.ok) {
            void this.mailbox?.ack(event.agentId, [event.messageId]);
          } else {
            // Failed consumption consumes a retry slot; after the limit the
            // message remains unacked and is reported as a dead letter.
            void this.mailbox?.recordAttempt(event.messageId, event.error);
          }
        }),
      );
    }
    this.publishAttached();
  }

  async whenIdle(): Promise<void> {
    await Promise.all([...this.agents.values()].map(({ agent }) => agent.whenIdle()));
  }

  async stop(): Promise<void> {
    for (const agentId of [...this.agents.keys()]) {
      await this.dispose(agentId);
    }
    for (const dispose of this.disposers.splice(0)) {
      await dispose();
    }
    this.bus = null;
  }

  private async loadTasks(): Promise<void> {
    if (!this.taskStore) return;
    for (const info of await this.taskStore.load()) {
      if (this.tasks.has(info.taskId)) continue;
      const promise = Promise.resolve(info.result);
      this.tasks.set(info.taskId, { info, promise });
    }
  }

  private persistTask(phase: 'started' | 'finished', info: AgentTaskInfo): void {
    if (!this.taskStore) return;
    void this.taskStore.append(phase, info);
  }

  private sendMessage(
    fromAgentId: string,
    toAgentId: string,
    content: string,
    replyTo?: string,
    fromSessionId?: string,
  ): { messageId: string } {
    const bus = this.bus;
    if (!bus) throw new Error('AgentRegistryModule is not started');
    const target = this.agents.get(toAgentId);
    if (!target) {
      throw new Error(`unknown agent: ${toAgentId}`);
    }
    const sender = this.agents.get(fromAgentId);
    if (sender && sender.info.teamId !== target.info.teamId) {
      throw new Error(
        `cross-team message denied: ${sender.info.teamId} -> ${target.info.teamId}`,
      );
    }
    const toSessionId = target.info.sessionId || fromSessionId || '';
    const message = new AgentTeamMessage({
      teamId: target.info.teamId || this.options.defaultTeamId,
      sessionId: toSessionId,
      fromAgentId: fromAgentId || this.options.defaultAgentId || '',
      toAgentId,
      content,
      replyTo,
      fromSessionId,
    });
    bus.publish(message);
    const entry: AgentMailboxEntry = {
      messageId: message.messageId,
      fromAgentId: message.fromAgentId,
      toAgentId: message.toAgentId,
      teamId: message.teamId ?? '',
      content: message.content,
      replyTo: message.replyTo,
      fromSessionId: message.fromSessionId,
      toSessionId: toSessionId || undefined,
      at: new Date().toISOString(),
    };
    this.mailbox?.record(entry);
    return { messageId: message.messageId };
  }

  /** Spawn (or reuse) an agent and start one task immediately. */
  private async startTask(
    args: Record<string, unknown>,
    parentSessionId?: string,
  ): Promise<TaskRecord> {
    const prompt = String(args.prompt ?? '').trim();
    if (!prompt) throw new Error('prompt is required');
    let agentId = String(args.agent_id ?? '').trim();
    const teamId = String(args.team_id ?? this.options.defaultTeamId ?? '').trim();
    const parentAgentId = String(
      args.parent_agent_id ?? this.options.defaultAgentId ?? '',
    ).trim();
    if (!agentId) {
      const info = await this.spawn({ teamId, parentAgentId, role: 'worker' });
      agentId = info.agentId;
    } else if (!this.agents.has(agentId)) {
      await this.spawn({ agentId, teamId, parentAgentId, role: 'worker' });
    }
    const sessionId =
      String(args.session_id ?? '').trim() ||
      this.agents.get(agentId)?.info.sessionId ||
      `session-${agentId}`;
    const priority = Number(args.priority ?? 0) || 0;
    const retries = Math.max(0, Number(args.retries ?? 0) || 0);
    const retryDelayMs = Math.max(0, Number(args.retry_delay_ms ?? 0) || 0);
    const taskId = `task-${++this.taskCounter}-${Date.now().toString(36)}`;
    const info: AgentTaskInfo = {
      taskId,
      agentId,
      sessionId,
      prompt,
      status: 'running',
      result: '',
      error: '',
      parentSessionId,
      teamId: this.agents.get(agentId)?.info.teamId ?? '',
      priority,
      failureReason: '',
      attempts: 1,
      startedAt: Date.now(),
      finishedAt: null,
    };
    const promise = this.enqueue(agentId, async () => {
      let lastError: unknown = new Error('agent task failed');
      for (let attempt = 1; attempt <= retries + 1; attempt += 1) {
        if ((info.status as string) === 'cancelled') throw lastError;
        info.attempts = attempt;
        const release = await this.acquireTeamSlot(
          this.agents.get(agentId!)?.info.teamId ?? '',
          priority,
        );
        try {
          return await this.executeTurn(agentId!, prompt, sessionId);
        } catch (error) {
          lastError = error;
          if ((info.status as string) === 'cancelled') throw error;
          if (attempt >= retries + 1) throw error;
          if (retryDelayMs > 0) {
            await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
          }
        } finally {
          release();
        }
      }
      throw lastError;
    })
      .then((result) => {
        if (info.status !== 'cancelled') {
          info.status = 'completed';
          info.result = result;
          info.finishedAt = Date.now();
          this.persistTask('finished', info);
          this.appendTaskEvent(parentSessionId, 'agent_task_finished', info);
          this.bus?.publish(
            new AgentTaskFinished({
              agentId,
              sessionId,
              teamId: this.agents.get(agentId)?.info.teamId,
              task: { ...info },
            }),
          );
        }
        return result;
      })
      .catch((error: unknown) => {
        if (info.status !== 'cancelled') {
          info.status = 'failed';
          info.error = error instanceof Error ? error.message : String(error);
          info.failureReason = /timed out/i.test(info.error)
            ? 'timeout'
            : /cancel|abort/i.test(info.error)
              ? 'cancelled'
              : /tool/i.test(info.error)
                ? 'tool_error'
                : 'agent_error';
          info.finishedAt = Date.now();
          this.persistTask('finished', info);
          this.appendTaskEvent(parentSessionId, 'agent_task_finished', info);
          this.bus?.publish(
            new AgentTaskFinished({
              agentId,
              sessionId,
              teamId: this.agents.get(agentId)?.info.teamId,
              task: { ...info },
            }),
          );
        }
        throw error;
      })
      .finally(async () => {
        const disposeAfter =
          args.dispose !== false && args.dispose !== 'false';
        if (disposeAfter) await this.dispose(agentId);
      });
    // Background tasks (wait:false) must not become unhandled rejections.
    void promise.catch(() => undefined);
    const registered = this.agents.get(agentId);
    if (registered && registered.info.sessionId !== sessionId) {
      registered.info.sessionId = sessionId;
      void this.agentStore?.save(registered.info);
    }
    const record: TaskRecord = { info, promise };
    this.tasks.set(taskId, record);
    this.persistTask('started', info);
    this.appendTaskEvent(parentSessionId, 'agent_task_started', info);
    this.bus?.publish(
      new AgentTaskStarted({
        agentId,
        sessionId,
        teamId: this.agents.get(agentId)?.info.teamId,
        task: { ...info },
      }),
    );
    return record;
  }

  private async runTask(
    args: Record<string, unknown>,
    parentSessionId?: string,
  ): Promise<string> {
    const record = await this.startTask(args, parentSessionId);
    return record.promise;
  }

  private async waitTask(taskId: string): Promise<AgentTaskInfo | null> {
    const record = this.tasks.get(taskId);
    if (!record) return null;
    await record.promise.catch(() => undefined);
    return { ...record.info };
  }

  private async waitTasks(ids: string[]): Promise<AgentTaskInfo[]> {
    const infos = await Promise.all(ids.map((id) => this.waitTask(id)));
    return infos.filter((info): info is AgentTaskInfo => info !== null);
  }

  private formatTasks(infos: AgentTaskInfo[]): string {
    if (infos.length === 0) return 'No tasks.';
    return infos
      .map((info) => {
        if (info.status === 'completed') {
          return `- ${info.taskId} [completed] ${info.agentId}: ${info.result}`;
        }
        if (info.status === 'failed') {
          return `- ${info.taskId} [failed] ${info.agentId}: ${info.error}`;
        }
        if ((info.status as string) === 'cancelled') {
          return `- ${info.taskId} [cancelled] ${info.agentId}`;
        }
        return `- ${info.taskId} [running] ${info.agentId}: ${info.prompt}`;
      })
      .join('\n');
  }

  private async executeTurn(agentId: string, prompt: string, sessionId: string): Promise<string> {
    const bus = this.bus;
    if (!bus) throw new Error('AgentRegistryModule is not started');
    const timeoutMs = this.options.taskTimeoutMs ?? 180_000;
    let resolveCompletion: (value: string) => void = () => undefined;
    let rejectCompletion: (error: Error) => void = () => undefined;
    const completion = new Promise<string>((resolve, reject) => {
      resolveCompletion = resolve;
      rejectCompletion = reject;
    });
    let settled = false;
    const finish = (value: string): void => {
      if (settled) return;
      settled = true;
      resolveCompletion(value);
    };
    const fail = (error: Error): void => {
      if (settled) return;
      settled = true;
      rejectCompletion(error);
    };
    const disposers: Array<() => Promise<void>> = [
      await bus.subscribe(
        TurnCompleted,
        (event) => {
          if (event.agentId !== agentId) return;
          finish(event.content);
        },
        { agentId },
      ),
      await bus.subscribe(
        TurnFailed,
        (event) => {
          if (event.agentId !== agentId) return;
          fail(new Error(event.error));
        },
        { agentId },
      ),
    ];
    const timer = setTimeout(() => {
      this.agents.get(agentId)?.agent.cancel(sessionId);
      fail(new Error(`agent task timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    try {
      bus.publish(
        new UserTurnRequested({
          sessionId,
          userText: prompt,
          agentId: this.agents.get(agentId)?.info.parentAgentId ?? this.options.defaultAgentId ?? '',
          targetAgentId: agentId,
        }),
      );
      return await completion;
    } finally {
      clearTimeout(timer);
      for (const dispose of disposers) await dispose();
    }
  }

  private appendTaskEvent(
    sessionId: string | undefined,
    type: 'agent_task_started' | 'agent_task_finished',
    info: AgentTaskInfo,
  ): void {
    if (!sessionId) return;
    const bus = this.bus;
    if (!bus) return;
    const data = {
      taskId: info.taskId,
      agentId: info.agentId,
      workerSessionId: info.sessionId,
      parentSessionId: info.parentSessionId,
      prompt: info.prompt,
      status: info.status,
      result: info.result,
      error: info.error,
    };
    void bus
      .request(
        new AppendSessionEvents({
          sessionId,
          events: [{ seq: 0, type, at: new Date().toISOString(), data }],
        }),
      )
      .catch(() => undefined);
  }

  private async acquireTeamSlot(
    teamId: string,
    priority = 0,
  ): Promise<() => void> {
    const limit = this.maxConcurrentForTeam(teamId) ?? 0;
    if (limit <= 0) return () => undefined;
    let state = this.teamSlots.get(teamId);
    if (!state) {
      state = { active: 0, waiters: [] };
      this.teamSlots.set(teamId, state);
    }
    if (state.active < limit) {
      state.active += 1;
    } else {
      await new Promise<void>((resolve) => {
        state!.waiters.push({ priority, resolve });
        state!.waiters.sort((left, right) => right.priority - left.priority);
      });
      // The releasing task handed its slot to this waiter.
    }
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const next = state!.waiters.shift();
      if (next) next.resolve();
      else state!.active = Math.max(0, state!.active - 1);
    };
  }

  private enqueue<T>(agentId: string, task: () => Promise<T>): Promise<T> {
    const previous = this.taskQueues.get(agentId) ?? Promise.resolve();
    const run = previous.catch(() => undefined).then(task);
    const tail = run.then(
      () => undefined,
      () => undefined,
    );
    this.taskQueues.set(agentId, tail);
    return run.finally(() => {
      if (this.taskQueues.get(agentId) === tail) this.taskQueues.delete(agentId);
    });
  }

  private async registerTeamTools(): Promise<void> {
    const bus = this.bus;
    if (!bus) return;
    try {
      await bus.request(
        new RegisterTool({
          name: 'spawn_agent',
          description:
            'Spawn a worker agent (or reuse agent_id) and run one task. Returns the answer, or a task id with wait:false.',
          jsonSchema: {
            type: 'object',
            properties: {
              prompt: { type: 'string', description: 'Task for the worker agent' },
              agent_id: { type: 'string', description: 'Reuse/spawn this agent id' },
              session_id: { type: 'string', description: 'Worker session id' },
              team_id: { type: 'string', description: 'Team id' },
              dispose: { type: 'boolean', description: 'Dispose the worker after the task (default true)' },
              wait: { type: 'boolean', description: 'Wait for the result (default true); false returns a task id' },
              priority: { type: 'number', description: 'Higher priority tasks run first when a team is saturated' },
              retries: { type: 'number', description: 'Additional attempts after a failed turn (default 0)' },
              retry_delay_ms: { type: 'number', description: 'Delay between retry attempts in ms' },
            },
            required: ['prompt'],
          },
          callable: async (args: Record<string, unknown>, ctx?: { sessionId?: string }) => {
            try {
              const record = await this.startTask(args, ctx?.sessionId);
              if (args.wait === false) return `Task: ${record.info.taskId}`;
              return await record.promise;
            } catch (error) {
              return `Error: ${error instanceof Error ? error.message : String(error)}`;
            }
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'wait_agents',
          description: 'Wait for spawned async tasks and return their results.',
          jsonSchema: {
            type: 'object',
            properties: {
              task_ids: {
                type: 'array',
                items: { type: 'string' },
                description: 'Task ids from spawn_agent wait:false',
              },
              timeout_ms: { type: 'number' },
            },
          },
          callable: async (args: Record<string, unknown>) => {
            const ids = Array.isArray(args.task_ids)
              ? args.task_ids.map((id) => String(id))
              : [...this.tasks.keys()];
            const timeoutMs = Math.max(0, Number(args.timeout_ms ?? 0) || 0);
            try {
              const infos =
                timeoutMs > 0
                  ? await Promise.race([
                      this.waitTasks(ids),
                      new Promise<never>((_resolve, reject) => {
                        setTimeout(
                          () =>
                            reject(
                              new Error(
                                `wait_agents timed out after ${timeoutMs}ms`,
                              ),
                            ),
                          timeoutMs,
                        );
                      }),
                    ])
                  : await this.waitTasks(ids);
              return this.formatTasks(infos);
            } catch (error) {
              return `Error: ${error instanceof Error ? error.message : String(error)}`;
            }
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'cancel_agent_task',
          description: 'Cancel a running agent task by id.',
          jsonSchema: {
            type: 'object',
            properties: { task_id: { type: 'string' } },
            required: ['task_id'],
          },
          callable: async (args: Record<string, unknown>) => {
            const taskId = String(args.task_id ?? '');
            if (!taskId) return 'Error: task_id is required';
            const record = this.tasks.get(taskId);
            if (!record) return `Error: task not found: ${taskId}`;
            if (record.info.status !== 'running') {
              return `Error: task is ${record.info.status}: ${taskId}`;
            }
            record.info.status = 'cancelled';
            record.info.finishedAt = Date.now();
            this.persistTask('finished', record.info);
            this.appendTaskEvent(record.info.parentSessionId, 'agent_task_finished', record.info);
            this.agents.get(record.info.agentId)?.agent.cancel(record.info.sessionId);
            this.cancelSubtree(record.info.agentId);
            this.bus?.publish(
              new AgentTaskFinished({
                agentId: record.info.agentId,
                sessionId: record.info.sessionId,
                teamId: this.agents.get(record.info.agentId)?.info.teamId,
                task: { ...record.info },
              }),
            );
            return `Cancelled: ${taskId}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'read_agent_mailbox',
          description: 'Read persisted messages delivered to one agent.',
          jsonSchema: {
            type: 'object',
            properties: {
              agent_id: { type: 'string' },
              limit: { type: 'number' },
            },
            required: ['agent_id'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            const limit = Number(args.limit ?? 20);
            const unreadOnly = args.unread_only === true;
            const entries =
              this.mailbox?.read(
                agentId,
                limit,
                unreadOnly,
                ctx.sessionId,
              ) ?? [];
            if (entries.length === 0) return 'No messages.';
            if (args.mark_read === true) {
              await this.mailbox?.ack(
                agentId,
                entries.map((entry) => entry.messageId),
                ctx.sessionId,
              );
            }
            return entries
              .map(
                (entry) =>
                  `- ${entry.at}${entry.read ? ' ✓' : ''} from=${entry.fromAgentId} to=${entry.toAgentId}: ${entry.content}`,
              )
              .join('\n');
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'mailbox_status',
          description: 'Show mailbox counts and the last acknowledged message for one agent.',
          jsonSchema: {
            type: 'object',
            properties: { agent_id: { type: 'string' } },
            required: ['agent_id'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            const status = this.mailbox?.status(
              agentId,
              this.options.mailboxMaxAttempts ?? 3,
              ctx.sessionId,
            ) ?? {
              total: 0,
              read: 0,
              unread: 0,
              dead: 0,
            };
            return [
              `agent: ${agentId}`,
              `total: ${status.total}`,
              `read: ${status.read}`,
              `unread: ${status.unread}`,
              `last_read: ${status.lastReadMessageId ?? '-'}`,
              `last_read_at: ${status.lastReadAt ?? '-'}`,
              `dead: ${status.dead ?? 0}`,
            ].join('\n');
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'ack_agent_messages',
          description: 'Acknowledge mailbox messages for one agent.',
          jsonSchema: {
            type: 'object',
            properties: {
              agent_id: { type: 'string' },
              message_ids: { type: 'array', items: { type: 'string' } },
            },
            required: ['agent_id'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            const ids = Array.isArray(args.message_ids)
              ? args.message_ids.map((id) => String(id))
              : undefined;
            const acked =
              (await this.mailbox?.ack(agentId, ids, ctx.sessionId)) ?? 0;
            return `Acked: ${acked}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'set_team_limits',
          description: 'Set dynamic per-team agent and concurrency limits.',
          jsonSchema: {
            type: 'object',
            properties: {
              team_id: { type: 'string' },
              max_agents: { type: 'number' },
              max_concurrent_tasks: { type: 'number' },
            },
            required: ['team_id'],
          },
          callable: async (args: Record<string, unknown>) => {
            const teamId = String(args.team_id ?? '');
            if (!teamId) return 'Error: team_id is required';
            const limits: TeamLimits = { teamId };
            if (args.max_agents !== undefined) limits.maxAgents = Number(args.max_agents);
            if (args.max_concurrent_tasks !== undefined) {
              limits.maxConcurrentTasks = Number(args.max_concurrent_tasks);
            }
            this.teamLimits.set(teamId, limits);
            return `Limits updated: ${teamId}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'task_status',
          description: 'Show one agent task detail by id.',
          jsonSchema: {
            type: 'object',
            properties: { task_id: { type: 'string' } },
            required: ['task_id'],
          },
          callable: async (args: Record<string, unknown>) => {
            const taskId = String(args.task_id ?? '');
            if (!taskId) return 'Error: task_id is required';
            const task = this.tasks.get(taskId)?.info;
            if (!task) return `Error: task not found: ${taskId}`;
            return [
              `task: ${task.taskId}`,
              `status: ${task.status}`,
              `agent: ${task.agentId}`,
              `team: ${task.teamId ?? '-'}`,
              `parent_session: ${task.parentSessionId ?? '-'}`,
              `worker_session: ${task.sessionId}`,
              `priority: ${task.priority ?? 0}`,
              `attempts: ${task.attempts ?? 1}`,
              `failure_reason: ${task.failureReason || '-'}`,
              `result: ${task.result || '-'}`,
              `error: ${task.error || '-'}`,
            ].join('\n');
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'list_dead_letters',
          description: 'List unacknowledged messages that exceeded replay attempts.',
          jsonSchema: {
            type: 'object',
            properties: { agent_id: { type: 'string' } },
            required: ['agent_id'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            const messages =
              this.mailbox?.deadLetters(
                agentId,
                this.options.mailboxMaxAttempts ?? 3,
                ctx.sessionId,
              ) ?? [];
            if (messages.length === 0) return 'No dead letters.';
            return messages
              .map(
                (entry) =>
                  `- ${entry.messageId} attempts=${entry.attempts ?? 0} from=${entry.fromAgentId}${entry.failureReason ? ` reason=${entry.failureReason}` : ''}: ${entry.content}`,
              )
              .join('\n');
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'revive_dead_letters',
          description: 'Reset replay attempts for dead-letter messages so they can be replayed.',
          jsonSchema: {
            type: 'object',
            properties: {
              agent_id: { type: 'string' },
              message_ids: { type: 'array', items: { type: 'string' } },
            },
            required: ['agent_id'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            if (!this.mailbox) return 'Error: mailbox persistence is disabled';
            const ids = Array.isArray(args.message_ids)
              ? args.message_ids.map((id) => String(id))
              : this.mailbox
                  .deadLetters(
                    agentId,
                    this.options.mailboxMaxAttempts ?? 3,
                    ctx.sessionId,
                  )
                  .map((entry) => entry.messageId);
            const revived = await this.mailbox.revive(ids);
            return `Revived: ${revived}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'replay_agent_mailbox',
          description: 'Re-deliver unacknowledged mailbox messages to an agent.',
          jsonSchema: {
            type: 'object',
            properties: {
              agent_id: { type: 'string' },
              limit: { type: 'number' },
            },
            required: ['agent_id'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            const limit = Number(args.limit ?? 10);
            const result = await this.replayMailbox(agentId, limit, ctx.sessionId);
            return `Replayed: ${result.replayed}, Dead: ${result.dead}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'set_agent_permissions',
          description: 'Set allowed/denied tool permissions for one agent.',
          jsonSchema: {
            type: 'object',
            properties: {
              agent_id: { type: 'string' },
              allowed: { type: 'array', items: { type: 'string' } },
              denied: { type: 'array', items: { type: 'string' } },
            },
            required: ['agent_id'],
          },
          callable: async (args: Record<string, unknown>) => {
            const agentId = String(args.agent_id ?? '');
            if (!agentId) return 'Error: agent_id is required';
            if (!this.agents.has(agentId)) return `Error: unknown agent: ${agentId}`;
            const allowed = Array.isArray(args.allowed)
              ? args.allowed.map((value) => String(value))
              : undefined;
            const denied = Array.isArray(args.denied)
              ? args.denied.map((value) => String(value))
              : undefined;
            await this.bus?.request(
              new SetAgentPermissions({ agentId, allowed, denied }),
            );
            return `Permissions updated: ${agentId}`;
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'send_agent_message',
          description: 'Deliver a message to a spawned agent; the agent processes it as a new turn.',
          jsonSchema: {
            type: 'object',
            properties: {
              agent_id: { type: 'string', description: 'Target agent id' },
              content: { type: 'string', description: 'Message content' },
              from_agent_id: { type: 'string', description: 'Sender agent id' },
            },
            required: ['agent_id', 'content'],
          },
          callable: async (
            args: Record<string, unknown>,
            ctx: { sessionId?: string },
          ) => {
            const to = String(args.agent_id ?? '');
            const content = String(args.content ?? '');
            if (!to) return 'Error: agent_id is required';
            if (!content) return 'Error: content is required';
            try {
              const sent = this.sendMessage(
                String(args.from_agent_id ?? this.options.defaultAgentId ?? ''),
                to,
                content,
                undefined,
                ctx.sessionId,
              );
              return `Delivered: ${sent.messageId}`;
            } catch (error) {
              return `Error: ${error instanceof Error ? error.message : String(error)}`;
            }
          },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'list_agents',
          description: 'List registered agents with role, team and status.',
          jsonSchema: { type: 'object', properties: {} },
          callable: async () => this.formatAgents(),
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'dispose_agent',
          description: 'Dispose an agent and its bus subscriptions.',
          jsonSchema: {
            type: 'object',
            properties: { agent_id: { type: 'string' } },
            required: ['agent_id'],
          },
          callable: async (args: Record<string, unknown>) => {
            const id = String(args.agent_id ?? '');
            if (!id) return 'Error: agent_id is required';
            if (await this.dispose(id)) return `Disposed: ${id}`;
            if (this.resumable.has(id)) return `Already disposed: ${id}`;
            return `Error: agent not found: ${id}`;
          },
        }),
      );
    } catch {
      // ToolsModule may not be available (unit tests); skip silently.
    }
  }

  private async replayMailbox(
    agentId: string,
    limit = 10,
    sessionId?: string,
  ): Promise<{ replayed: number; dead: number }> {
    const bus = this.bus;
    if (!bus || !this.mailbox) return { replayed: 0, dead: 0 };
    const target = this.agents.get(agentId);
    if (!target) return { replayed: 0, dead: 0 };
    const maxAttempts = this.options.mailboxMaxAttempts ?? 3;
    const messages = this.mailbox.read(agentId, limit, true, sessionId);
    let replayed = 0;
    let dead = 0;
    for (const entry of messages) {
      const attempts = entry.attempts;
      if ((attempts ?? 0) >= maxAttempts) {
        dead += 1;
        continue;
      }
      await this.mailbox.recordAttempt(entry.messageId);
      bus.publish(
        new AgentTeamMessage({
          messageId: entry.messageId,
          teamId: entry.teamId,
          fromAgentId: entry.fromAgentId,
          toAgentId: entry.toAgentId,
          content: entry.content,
          replyTo: entry.replyTo,
          fromSessionId: entry.fromSessionId,
        }),
      );
      replayed += 1;
    }
    return { replayed, dead };
  }

  private formatAgents(): string {
    const agents = this.list();
    if (agents.length === 0) return 'No agents.';
    return agents
      .map(
        (info) =>
          `- ${info.agentId} role=${info.role} team=${info.teamId || '-'} parent=${info.parentAgentId || '-'} status=${info.status}`,
      )
      .join('\n');
  }

  private maxAgentsForTeam(teamId: string): number | undefined {
    return this.teamLimits.get(teamId)?.maxAgents ?? this.options.maxAgentsPerTeam;
  }

  private maxConcurrentForTeam(teamId: string): number | undefined {
    return (
      this.teamLimits.get(teamId)?.maxConcurrentTasks ??
      this.options.maxConcurrentTasksPerTeam
    );
  }

  private async spawn(spec: AgentSpawnSpec = {}): Promise<AgentInfo> {
    const bus = this.bus;
    if (!bus) throw new Error('AgentRegistryModule is not started');
    const agentId = spec.agentId || `agent-${++counter}-${Date.now().toString(36)}`;
    if (this.agents.has(agentId)) throw new Error(`agent already exists: ${agentId}`);
    const teamId = (spec.teamId ?? this.options.defaultTeamId ?? '').trim();
    const maxAgents = this.maxAgentsForTeam(teamId);
    if (maxAgents !== undefined) {
      const count = [...this.agents.values()].filter(
        ({ info }) => info.teamId === teamId,
      ).length;
      if (count >= maxAgents) {
        throw new Error(
          `team agent limit reached: ${teamId} (${count}/${maxAgents})`,
        );
      }
    }
    const resolvedSpec = { ...spec, agentId };
    const agent =
      this.options.createAgent?.(resolvedSpec) ??
      new AgentModule({
        model: this.options.workerModel ?? this.options.model,
        systemPrompt: this.options.workerSystemPrompt,
        agentId,
        parentAgentId: spec.parentAgentId,
        teamId,
        acceptBroadcast: false,
        maxIterations: this.options.maxIterations,
      });
    await agent.start(bus);
    if (this.options.workerPermissions) {
      await bus
        .request(
          new SetAgentPermissions({
            agentId,
            allowed: this.options.workerPermissions.allowed,
            denied: this.options.workerPermissions.denied,
          }),
        )
        .catch(() => undefined);
    }
    const info: AgentInfo = {
      agentId,
      parentAgentId: spec.parentAgentId ?? '',
      teamId,
      sessionId: spec.sessionId ?? '',
      role: spec.role ?? 'worker',
      status: 'idle',
    };
    this.agents.set(agentId, { agent, info });
    void this.agentStore?.save(info);
    bus.publish(new AgentSpawned({ agentId, teamId: info.teamId, info }));
    bus.publish(
      new AgentTeamMemberJoined({
        teamId: info.teamId,
        agentId,
        member: {
          agentId,
          role: info.role,
          sessionId: info.sessionId,
          parentAgentId: info.parentAgentId,
        },
      }),
    );
    return { ...info };
  }

  /** Cancel running tasks for an agent subtree (used by task cancellation). */
  private cancelSubtree(agentId: string): void {
    const record = this.findRunningTask(agentId);
    if (record) {
      record.info.status = 'cancelled';
      record.info.failureReason = 'cancelled';
      record.info.finishedAt = Date.now();
      this.persistTask('finished', record.info);
      this.appendTaskEvent(record.info.parentSessionId, 'agent_task_finished', record.info);
      this.agents.get(agentId)?.agent.cancel(record.info.sessionId);
      this.bus?.publish(
        new AgentTaskFinished({
          agentId,
          sessionId: record.info.sessionId,
          teamId: this.agents.get(agentId)?.info.teamId,
          task: { ...record.info },
        }),
      );
    }
    for (const [childId, child] of this.agents) {
      if (child.info.parentAgentId === agentId) this.cancelSubtree(childId);
    }
  }

  private findRunningTask(agentId: string): TaskRecord | undefined {
    for (const record of this.tasks.values()) {
      if (record.info.agentId === agentId && record.info.status === 'running') {
        return record;
      }
    }
    return undefined;
  }

  private list(teamId?: string, includeResumable = false): AgentInfo[] {
    const active = [...this.agents.values()]
      .filter(({ info }) => !teamId || info.teamId === teamId)
      .map(({ agent, info }): AgentInfo => ({
        ...info,
        status: agent.busy ? 'running' : 'idle',
      }));
    if (!includeResumable) return active;
    const activeIds = new Set(active.map((info) => info.agentId));
    const dormant: AgentInfo[] = [...this.resumable.values()].filter(
      (info) => !activeIds.has(info.agentId) && (!teamId || info.teamId === teamId),
    );
    return [...active, ...dormant];
  }

  private async loadAgents(): Promise<void> {
    if (!this.agentStore) return;
    for (const info of await this.agentStore.load()) {
      if (this.agents.has(info.agentId)) continue;
      this.resumable.set(info.agentId, info);
    }
  }

  private async resume(agentId: string, sessionId?: string): Promise<AgentInfo> {
    const active = this.agents.get(agentId);
    if (active) return { ...active.info };
    const record = this.resumable.get(agentId);
    if (!record) throw new Error(`agent not found: ${agentId}`);
    if (record.parentAgentId) {
      throw new Error('only the main agent conversation can be resumed');
    }
    const bus = this.bus;
    if (!bus) throw new Error('AgentRegistryModule is not started');
    const resolvedSession = sessionId || record.sessionId;
    if (resolvedSession) {
      const bundle = await bus
        .request(new LoadSession({ targetSessionId: resolvedSession }))
        .catch(() => null);
      if (bundle?.events) {
        await bus
          .request(
            new ReplaceSessionEvents({
              sessionId: resolvedSession,
              events: bundle.events,
            }),
          )
          .catch(() => undefined);
      }
    }
    const spec: AgentSpawnSpec = {
      agentId,
      parentAgentId: record.parentAgentId,
      teamId: record.teamId,
      sessionId: resolvedSession,
      role: record.role,
    };
    const agent =
      this.options.createAgent?.(spec) ??
      new AgentModule({
        model: this.options.workerModel ?? this.options.model,
        systemPrompt: this.options.workerSystemPrompt,
        agentId,
        parentAgentId: record.parentAgentId,
        teamId: record.teamId,
        acceptBroadcast: false,
        maxIterations: this.options.maxIterations,
      });
    await agent.start(bus);
    const info: AgentInfo = {
      ...record,
      sessionId: resolvedSession,
      status: 'idle',
    };
    this.agents.set(agentId, { agent, info });
    this.resumable.delete(agentId);
    await this.agentStore?.save(info);
    bus.publish(new AgentSpawned({ agentId, teamId: info.teamId, info }));
    bus.publish(
      new AgentTeamMemberJoined({
        teamId: info.teamId,
        agentId,
        member: {
          agentId,
          role: info.role,
          sessionId: info.sessionId,
          parentAgentId: info.parentAgentId,
        },
      }),
    );
    return { ...info };
  }

  /** Disband a team: dispose members, cancel tasks and remove durable history. */
  async disposeTeam(teamId: string): Promise<DisposeTeamResult> {
    const id = teamId.trim();
    const empty = (message: string): DisposeTeamResult => ({
      ok: false,
      teamId: id,
      disposedAgents: 0,
      cancelledTasks: 0,
      removedTasks: 0,
      message,
    });
    if (!id || id === '(none)') return empty('无效的 Team ID');

    const members = [...this.agents.entries()].filter(
      ([, registered]) => registered.info.teamId === id,
    );
    if (
      members.some(
        ([agentId, registered]) =>
          agentId === this.options.defaultAgentId || registered.info.role === 'lead',
      )
    ) {
      return empty('不能解散包含主 Agent 的 Team');
    }

    let disposedAgents = 0;
    for (const [agentId] of members) {
      if (await this.dispose(agentId)) disposedAgents += 1;
    }

    let cancelledTasks = 0;
    let removedTasks = 0;
    for (const [taskId, record] of [...this.tasks]) {
      if (record.info.teamId !== id) continue;
      if (record.info.status === 'running') {
        record.info.status = 'cancelled';
        record.info.failureReason = 'team disposed';
        record.info.finishedAt = Date.now();
        this.persistTask('finished', record.info);
        this.bus?.publish(
          new AgentTaskFinished({
            agentId: record.info.agentId,
            sessionId: record.info.sessionId,
            teamId: id,
            task: { ...record.info },
          }),
        );
        cancelledTasks += 1;
      }
      this.tasks.delete(taskId);
      removedTasks += 1;
    }

    await this.taskStore?.removeTeam(id);
    await this.agentStore?.removeTeam(id);
    await this.mailbox?.removeTeam(id);
    this.teamLimits.delete(id);
    this.teamSlots.delete(id);
    for (const [agentId, info] of this.resumable) {
      if (info.teamId === id) this.resumable.delete(agentId);
    }

    return {
      ok: true,
      teamId: id,
      disposedAgents,
      cancelledTasks,
      removedTasks,
      message: `已解散 ${id}：agents=${disposedAgents} tasks=${removedTasks} cancelled=${cancelledTasks}`,
    };
  }

  private async dispose(agentId: string): Promise<boolean> {
    const pending = this.disposing.get(agentId);
    if (pending) return pending;
    const task = this.disposeInternal(agentId).finally(() => {
      if (this.disposing.get(agentId) === task) this.disposing.delete(agentId);
    });
    this.disposing.set(agentId, task);
    return task;
  }

  private async disposeInternal(agentId: string): Promise<boolean> {
    const registered = this.agents.get(agentId);
    if (!registered) return false;
    // Dispose descendants first so a parent cannot leave orphan workers.
    for (const [childId, child] of [...this.agents]) {
      if (child.info.parentAgentId === agentId) {
        await this.dispose(childId);
      }
    }
    this.agents.delete(agentId);
    await registered.agent.stop();
    registered.info.status = 'disposed';
    const dormant: AgentInfo = { ...registered.info, status: 'resumable' };
    this.resumable.set(agentId, dormant);
    void this.agentStore?.save(dormant);
    this.bus?.publish(new AgentDisposed({ agentId }));
    this.bus?.publish(
      new AgentTeamMemberLeft({
        teamId: registered.info.teamId,
        agentId,
      }),
    );
    return true;
  }
}