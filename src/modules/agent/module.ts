/**
 * AgentModule — conversation engine.
 *
 * Two modes:
 *  - **model mode** (`ChatModel` provided): ReAct-style tool loop over the bus,
 *    mirroring Python's TurnProcessor: stream → tool calls → ExecuteTool →
 *    tool messages → repeat until a final answer.
 *  - **responder mode** (default `EchoResponder`): deterministic streaming used
 *    by tests and offline demos.
 *
 * Handles both user turns (`UserTurnRequested`) and cron turns
 * (`CronTurnRequested`); cron turns carry a `streamId` that the cron module
 * uses to correlate the final `TurnCompleted`.
 */

import type { Module } from '../../kernel/runtime';
import type { AsyncEventBus } from '../../kernel/bus';
import type { AttachmentDTO, MessageDTO } from '../../kernel/dto/message';
import { buildUserContent } from '../../attachments/loader';
import { resolveAttachmentRef, ATTACHMENT_URL_PREFIX } from '../../attachments/images';
import type { ModelEffort } from '../../kernel/contracts/settings';
import { RuntimeContextNotice } from '../../kernel/contracts/prompt';
import { SettingsUpdated } from '../../kernel/contracts/settings';
import { assistantMessage, userMessage } from '../../kernel/dto/message';
import type { ToolResult, ToolSpec } from '../../kernel/dto/tool';
import {
  LoadSkill,
  ReflectSkills,
  SkillLoaded,
} from '../../kernel/contracts/skills';
import {
  ThinkingUpdated,
  TokenEmitted,
  GetSystemPrompt,
  SetModelEffort,
  SetPromptPressure,
  SetRequestHeader,
  TokenUsageReported,
  TurnCompleted,
  TurnDequeued,
  TurnFailed,
  TurnQueued,
  TurnStarted,
  UserTurnRequested,
  type TurnKind,
} from '../../kernel/contracts/chat';
import { estimateMessageTokens } from '../../session/token-meter';
import {
  CompactContext,
  CompactionFailed,
  CompactionStarted,
  ContextCompacted,
  type CompactionReason,
  type CompactionResult,
} from '../../kernel/contracts/compaction';
import { CronTurnRequested } from '../../kernel/contracts/cron';
import {
  AgentTeamMessage,
  AgentTeamMessageConsumed,
  AgentTeamTaskCompleted,
  AgentTeamTaskRequested,
} from '../../kernel/contracts/team';
import {
  AppendSessionEvents,
  GetSessionProjection,
} from '../../kernel/contracts/session';
import {
  messageEvent,
  replaceMessagesEvent,
} from '../../session/projector';
import {
  ExecuteTool,
  GetToolCatalog,
  ToolFinished,
  ToolStarted,
} from '../../kernel/contracts/tools';
import type {
  ChatModel,
  ChatRequest,
  ChatStreamEvent,
  ChatUsage,
  ToolCall,
  ToolSchema,
} from '../../llm/client';
import { ensureToolCallResponses } from './tool-history';

export interface RespondInput {
  text: string;
  sessionId: string;
  turnId: string;
  history: MessageDTO[];
}

/** Streaming response source for the offline/deterministic mode. */
export interface Responder {
  respond(input: RespondInput): AsyncIterable<string>;
}

export class EchoResponder implements Responder {
  constructor(private readonly prefix = 'Echo: ') {}

  async *respond(input: RespondInput): AsyncIterable<string> {
    yield this.prefix;
    yield input.text;
  }
}

export interface AgentModuleOptions {
  /** Real streaming LLM (ChatClient). When omitted, the echo responder is used. */
  model?: ChatModel;
  responder?: Responder;
  maxIterations?: number;
  systemPrompt?: string;
  /** Publish ReflectSkills after a user turn that loaded no skill (default true). */
  autoReflect?: boolean;
  /** Model context window used for automatic compaction (0 = manual only). */
  contextWindowTokens?: number;
  /** Compact when provider prompt pressure exceeds this fraction (default 0.8). */
  compactThresholdRatio?: number;
  /** Fraction of the window kept verbatim after compaction (default 0.16). */
  compactRetainRatio?: number;
  thinkingEnabled?: boolean;
  effort?: ModelEffort;
  /** Max long edge for request-time image variants (0 = canonical). */
  imageMaxDimension?: number;
  /** Agent-team identity; scopes published events and serial queues. */
  agentId?: string;
  parentAgentId?: string;
  teamId?: string;
  /** Whether an agent with an id also handles untargeted broadcast turns. */
  acceptBroadcast?: boolean;
}

interface TurnCommandLike {
  agentId: string;
  targetAgentId: string;
}

interface TurnResult {
  content: string;
  ok: boolean;
  error?: string;
}

interface TurnParams {
  sessionId: string;
  userText: string;
  kind: TurnKind;
  streamId: string;
  attachments: AttachmentDTO[];
}

let turnCounter = 0;

function nextTurnId(): string {
  turnCounter += 1;
  return `turn-${turnCounter.toString(36)}-${Date.now().toString(36)}`;
}

let queueCounter = 0;
function nextQueueId(): string {
  queueCounter += 1;
  return `queue-${queueCounter.toString(36)}-${Date.now().toString(36)}`;
}

function toToolSchema(spec: ToolSpec): ToolSchema {
  return {
    type: 'function',
    function: {
      name: spec.name,
      description: spec.description,
      parameters: spec.jsonSchema,
    },
  };
}

function parseArguments(raw: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(raw || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class AgentModule implements Module {
  readonly name = 'agent';
  readonly dependencies: readonly string[] = ['tools', 'skill'];

  private readonly model: ChatModel | null;
  private readonly responder: Responder;
  private maxIterations: number;
  private readonly systemPrompt: string | undefined;
  private readonly autoReflect: boolean;
  private readonly contextWindowTokens: number;
  private readonly compactThresholdRatio: number;
  private readonly compactRetainRatio: number;
  private lastPromptTokens = 0;
  private restoredHeader: Record<string, unknown> | null = null;
  private readonly pendingRuntimeNotices = new Map<string, string>();
  private thinkingEnabled = true;
  private effort: ModelEffort = 'high';
  private readonly imageMaxDimension: number;
  private readonly agentId: string;
  private readonly parentAgentId: string;
  private readonly teamId: string;
  private readonly acceptBroadcast: boolean;
  private readonly tasks = new Set<Promise<unknown>>();
  private readonly turnAborts = new Map<string, AbortController>();
  private readonly turnQueues = new Map<string, Promise<void>>();
  private readonly disposers: Array<() => Promise<void>> = [];
  private bus: AsyncEventBus | null = null;

  constructor(options: AgentModuleOptions | Responder = {}) {
    if (typeof options === 'object' && 'respond' in options && typeof options.respond === 'function') {
      this.model = null;
      this.responder = options as Responder;
      this.maxIterations = 10;
      this.systemPrompt = undefined;
      this.autoReflect = false;
      this.contextWindowTokens = 0;
      this.compactThresholdRatio = 0.8;
      this.compactRetainRatio = 0.16;
      this.thinkingEnabled = true;
      this.effort = 'high';
      this.imageMaxDimension = 0;
      this.agentId = '';
      this.parentAgentId = '';
      this.teamId = '';
      this.acceptBroadcast = true;
      return;
    }
    const config = options as AgentModuleOptions;
    this.model = config.model ?? null;
    this.responder = config.responder ?? new EchoResponder();
    this.maxIterations = config.maxIterations ?? 10;
    this.systemPrompt = config.systemPrompt;
    this.autoReflect = config.autoReflect !== false;
    this.contextWindowTokens = config.contextWindowTokens ?? 0;
    this.compactThresholdRatio = config.compactThresholdRatio ?? 0.8;
    this.compactRetainRatio = config.compactRetainRatio ?? 0.16;
    this.thinkingEnabled = config.thinkingEnabled ?? true;
    this.effort = config.effort ?? 'high';
    this.imageMaxDimension = config.imageMaxDimension ?? 0;
    this.agentId = config.agentId ?? '';
    this.parentAgentId = config.parentAgentId ?? '';
    this.teamId = config.teamId ?? '';
    this.acceptBroadcast = config.acceptBroadcast !== false;
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    bus.provide(CompactContext, (req) => this.compact(req.sessionId, 'manual'));
    this.disposers.push(await bus.subscribe(SetPromptPressure, (event) => {
      this.lastPromptTokens = Math.max(0, event.promptTokens);
    }));
    this.disposers.push(await bus.subscribe(SetRequestHeader, (event) => {
      this.restoredHeader = event.header && Object.keys(event.header).length > 0
        ? event.header
        : null;
    }));
    this.disposers.push(await bus.subscribe(SettingsUpdated, (event) => {
      const next = Math.max(1, Math.floor(event.settings.maxIterations));
      if (Number.isFinite(next)) this.maxIterations = next;
    }));
    this.disposers.push(await bus.subscribe(RuntimeContextNotice, (event) => {
      const key = `${event.sessionId || '*'}\u0000${event.key || event.source || 'runtime'}`;
      if (!event.text.trim()) {
        this.pendingRuntimeNotices.delete(key);
        return;
      }
      this.pendingRuntimeNotices.set(key, event.text);
    }));
    bus.provide(SetModelEffort, (req) => {
      this.thinkingEnabled = req.thinkingEnabled;
      this.effort = req.effort;
      return { thinkingEnabled: this.thinkingEnabled, effort: this.effort };
    });
    this.disposers.push(await bus.subscribe(UserTurnRequested, (cmd) => {
      if (!this.acceptsCommand(cmd)) return;
      this.enqueueTurn(
        cmd.sessionId,
        () =>
          this.runCore({
            sessionId: cmd.sessionId,
            userText: cmd.userText,
            kind: 'user',
            streamId: '',
            attachments: cmd.attachments,
          }),
        {
          queueId: nextQueueId(),
          userInput: cmd.userText,
          attachments: cmd.attachments,
        },
      );
    }));
    this.disposers.push(await bus.subscribe(AgentTeamMessage, (event) => {
      if (!event.targetAgentId || event.targetAgentId !== this.agentId) return;
      if (!event.content.trim()) return;
      const task = this.enqueueTurn(
        event.sessionId || `${this.agentId}-session`,
        () =>
          this.runCore({
            sessionId: event.sessionId || `${this.agentId}-session`,
            userText: event.content,
            kind: 'user',
            streamId: '',
            attachments: [],
          }),
      )
        .then((outcome) => {
          this.requireBus().publish(
            new AgentTeamMessageConsumed({
              ...this.agentContext(),
              messageId: event.messageId,
              ok: outcome.ok,
              error: outcome.error,
            }),
          );
        })
        .catch(() => {
          this.requireBus().publish(
            new AgentTeamMessageConsumed({
              ...this.agentContext(),
              messageId: event.messageId,
              ok: false,
              error: 'agent turn failed',
            }),
          );
        });
      this.schedule(task);
    }, this.agentId ? { agentId: this.agentId } : {}));
    this.disposers.push(await bus.subscribe(AgentTeamTaskRequested, (command) => {
      if (!this.agentId || command.targetAgentId !== this.agentId) return;
      const task = this.enqueueTurn(
        command.sessionId || `${this.agentId}-session`,
        () =>
          this.runCore({
            sessionId: command.sessionId || `${this.agentId}-session`,
            userText: command.prompt,
            kind: 'user',
            streamId: '',
            attachments: [],
          }),
      )
        .then((outcome) => {
          this.requireBus().publish(
            new AgentTeamTaskCompleted({
              ...this.agentContext(),
              sessionId: command.sessionId,
              taskId: command.taskId,
              agentId: this.agentId,
              result: outcome.content,
            }),
          );
        })
        .catch(() => undefined);
      this.schedule(task);
    }, this.agentId ? { agentId: this.agentId } : {}));
    this.disposers.push(await bus.subscribe(CronTurnRequested, (cmd) => {
      if (!this.acceptsCommand(cmd)) return;
      const trigger = cmd.trigger ?? {};
      const sessionId = cmd.sessionId || String(trigger.session_id ?? '');
      this.enqueueTurn(sessionId, () =>
        this.runCore({
          sessionId,
          userText: String(trigger.prompt ?? ''),
          kind: 'cron',
          streamId: String(trigger.stream_id ?? ''),
          attachments: [],
        }),
      );
    }));
  }

  async stop(): Promise<void> {
    await this.whenIdle();
    for (const dispose of this.disposers.splice(0)) {
      await dispose();
    }
    this.bus = null;
  }

  get id(): string {
    return this.agentId;
  }

  get parent(): string {
    return this.parentAgentId;
  }

  get team(): string {
    return this.teamId;
  }

  get busy(): boolean {
    return this.tasks.size > 0;
  }

  /**
   * Wait until the bus is drained and all in-flight turns (plus the events they
   * emitted) have been dispatched. Useful in tests and graceful shutdown.
   */
  async whenIdle(): Promise<void> {
    const bus = this.bus;
    for (;;) {
      if (bus) await bus.idle();
      if (this.tasks.size === 0) {
        if (bus) await bus.idle();
        if (this.tasks.size === 0) return;
      }
      await Promise.all([...this.tasks]);
    }
  }

  private enqueueTurn(
    sessionId: string,
    run: () => Promise<TurnResult>,
    queued?: { queueId: string; userInput: string; attachments: AttachmentDTO[] },
  ): Promise<TurnResult> {
    const didQueue = this.turnQueues.has(sessionId) && queued !== undefined;
    if (didQueue && queued && this.bus) {
      this.bus.publish(
        new TurnQueued({
          ...this.agentContext(),
          sessionId,
          queueId: queued.queueId,
          userInput: queued.userInput,
          attachments: queued.attachments,
        }),
      );
    }
    const previous = this.turnQueues.get(sessionId) ?? Promise.resolve();
    const task = previous.catch(() => undefined).then(() => {
      if (didQueue && queued && this.bus) {
        this.bus.publish(
          new TurnDequeued({
            ...this.agentContext(),
            sessionId,
            queueId: queued.queueId,
          }),
        );
      }
      return run();
    });
    const tail = task.then(
      () => undefined,
      () => undefined,
    );
    this.turnQueues.set(sessionId, tail);
    this.schedule(
      task.finally(() => {
        if (this.turnQueues.get(sessionId) === tail) {
          this.turnQueues.delete(sessionId);
        }
      }),
    );
    return task;
  }

  private schedule<T>(task: Promise<T>): void {
    this.tasks.add(task);
    void task.finally(() => this.tasks.delete(task));
  }

  private agentContext(): {
    agentId: string;
    parentAgentId: string;
    teamId: string;
  } {
    return {
      agentId: this.agentId,
      parentAgentId: this.parentAgentId,
      teamId: this.teamId,
    };
  }

  /** Commands addressed to another agent are ignored by this runtime. */
  private acceptsCommand(command: TurnCommandLike): boolean {
    const target = command.targetAgentId;
    if (target) return this.agentId === target;
    if (command.agentId) return command.agentId === this.agentId;
    return this.acceptBroadcast;
  }

  private requireBus(): AsyncEventBus {
    if (!this.bus) throw new Error('AgentModule.start() has not been called');
    return this.bus;
  }

  private async runCore(params: TurnParams): Promise<TurnResult> {
    const controller = new AbortController();
    this.turnAborts.set(params.sessionId, controller);
    try {
      if (controller.signal.aborted) throw new Error('agent turn cancelled');
      if (this.model) {
        return await this.runModelTurn(params);
      }
      return await this.runResponderTurn(params);
    } finally {
      if (this.turnAborts.get(params.sessionId) === controller) {
        this.turnAborts.delete(params.sessionId);
      }
    }
  }

  /** Cancel the active turn for one session, or every active turn. */
  cancel(sessionId?: string): void {
    for (const [id, controller] of this.turnAborts) {
      if (sessionId && id !== sessionId) continue;
      controller.abort(new Error('agent turn cancelled'));
    }
  }

  private turnSignal(sessionId: string): AbortSignal | undefined {
    return this.turnAborts.get(sessionId)?.signal;
  }

  private throwIfCancelled(sessionId: string): void {
    const signal = this.turnSignal(sessionId);
    if (signal?.aborted) throw new Error('agent turn cancelled');
  }

  // ── responder mode (offline / deterministic) ───────────────────────────

  private async runResponderTurn(params: TurnParams): Promise<TurnResult> {
    const bus = this.requireBus();
    const turnId = nextTurnId();
    const { sessionId, userText, kind, streamId, attachments } = params;

    bus.publish(
      new TurnStarted({
        ...this.agentContext(),
        sessionId,
        turnId,
        kind,
        userInput: userText,
        attachments,
        source: kind === 'cron' ? 'cron' : 'user',
      }),
    );
    await this.appendSessionEvents(sessionId, [
      {
        seq: 0,
        type: 'turn_started',
        at: new Date().toISOString(),
        data: { turnId, userInput: userText, kind, attachments, ...this.agentContext() },
      },
    ]);

    try {
      await this.compactIfNeeded(sessionId, turnId).catch(() => undefined);
      const history = await this.getHistory(sessionId);
      const userMsg = userMessage(buildUserContent(userText, attachments));
      if (attachments.length > 0) {
        userMsg.attachments = attachments.map(({ dataUrl: _dataUrl, ...rest }) => rest);
      }
      if (kind === 'cron') userMsg.alex_turn_kind = 'cron';
      await this.appendSessionLog(sessionId, [userMsg]);

      let content = '';
      for await (const delta of this.responder.respond({
        text: userText || `[${attachments.length} attachment(s)]`,
        sessionId,
        turnId,
        history,
      })) {
        if (!delta) continue;
        content += delta;
        bus.publish(new TokenEmitted({ ...this.agentContext(), sessionId, turnId, delta, streamId }));
      }

      const assistantMsg = assistantMessage(content);
      await this.appendSessionLog(sessionId, [assistantMsg]);
      const fullHistory = await this.getHistory(sessionId);
      bus.publish(
        new TurnCompleted({
          ...this.agentContext(),
          sessionId,
          turnId,
          kind,
          content,
          streamId,
          messages: fullHistory,
          messageBatch: [userMsg, assistantMsg],
        }),
      );
      return { content, ok: true };
    } catch (error) {
      bus.publish(new TurnFailed({ ...this.agentContext(), sessionId, turnId, error: errorText(error), streamId }));
      return { content: '', ok: false, error: errorText(error) };
    }
  }

  // ── model mode (real agent loop) ───────────────────────────────────────

  private async runModelTurn(params: TurnParams): Promise<TurnResult> {
    const bus = this.requireBus();
    const model = this.model;
    if (!model) return { content: '', ok: false };
    const { sessionId, userText, kind, streamId, attachments } = params;
    const turnId = nextTurnId();

    bus.publish(
      new TurnStarted({
        ...this.agentContext(),
        sessionId,
        turnId,
        kind,
        userInput: userText,
        attachments,
        source: kind === 'cron' ? 'cron' : 'user',
      }),
    );
    await this.appendSessionEvents(sessionId, [
      {
        seq: 0,
        type: 'turn_started',
        at: new Date().toISOString(),
        data: { turnId, userInput: userText, kind, attachments, ...this.agentContext() },
      },
    ]);

    try {
      await this.compactIfNeeded(sessionId, turnId).catch(() => undefined);
      const history = await this.getHistory(sessionId);
      const userMsg = userMessage(buildUserContent(userText, attachments));
      if (attachments.length > 0) {
        userMsg.attachments = attachments.map(({ dataUrl: _dataUrl, ...rest }) => rest);
      }
      if (kind === 'cron') userMsg.alex_turn_kind = 'cron';
      await this.appendSessionLog(sessionId, [userMsg]);

      const messages: MessageDTO[] = [...history, userMsg];
      // Runtime context is request-only user-role context (DSH-style snapshot),
      // not a system-prompt mutation: stable prompt prefixes stay cacheable.
      const sessionPrefix = `${sessionId}\u0000`;
      const runtimeNotices: string[] = [];
      for (const [key, text] of this.pendingRuntimeNotices) {
        if (!key.startsWith(sessionPrefix) && !key.startsWith('*\u0000')) continue;
        runtimeNotices.push(text);
        this.pendingRuntimeNotices.delete(key);
      }
      if (runtimeNotices.length > 0) {
        messages.push({
          role: 'user',
          content:
            '<system-reminder>\nThe following information describes your current runtime context.\n' +
            runtimeNotices.map((notice) => `- ${notice}`).join('\n') +
            '\n</system-reminder>',
          alex_runtime_context: true,
        });
      }
      const batch: MessageDTO[] = [];
      const catalog = await bus.request(new GetToolCatalog({ ...this.agentContext() }));
      const tools = catalog.map(toToolSchema);
      const assembled = await this.assembledSystemPrompt(bus, sessionId);
      let systemPrompt = assembled ?? this.composeSystemPrompt();
      let requestTools = tools;
      let requestThinking = this.thinkingEnabled;
      let requestEffort = this.effort;
      if (this.restoredHeader) {
        if (typeof this.restoredHeader.systemPrompt === 'string') {
          systemPrompt = this.restoredHeader.systemPrompt;
        }
        if (Array.isArray(this.restoredHeader.tools)) {
          requestTools = this.restoredHeader.tools as typeof tools;
        }
        if (typeof this.restoredHeader.thinking === 'boolean') {
          requestThinking = this.restoredHeader.thinking;
        }
        if (
          this.restoredHeader.effort === 'low' ||
          this.restoredHeader.effort === 'high' ||
          this.restoredHeader.effort === 'max'
        ) {
          requestEffort = this.restoredHeader.effort;
        }
        this.restoredHeader = null;
      }
      const loadedSkills = new Set<string>();

      let finalContent = '';
      let finalThinking = '';
      let completed = false;

      for (let iteration = 0; iteration < this.maxIterations && !completed; iteration += 1) {
        this.throwIfCancelled(sessionId);
        await this.appendSessionEvents(sessionId, [
          {
            seq: 0,
            type: 'request_header',
            at: new Date().toISOString(),
            data: {
              ...this.agentContext(),
              turnId,
              iteration,
              systemPrompt: systemPrompt ?? '',
              tools: requestTools,
              thinking: requestThinking,
              effort: requestEffort,
            },
          },
        ]);
        const outcome = await this.streamOnce(
          model,
          {
            messages: await this.hydrateMessages(messages),
            tools: requestTools.length > 0 ? requestTools : undefined,
            systemPrompt,
            thinking: requestThinking,
            effort: requestEffort,
          },
          sessionId,
          turnId,
          streamId,
          this.turnSignal(sessionId),
        );

        finalContent = outcome.content;
        finalThinking = outcome.thinking;

        const assistantMsg: MessageDTO = {
          role: 'assistant',
          content: outcome.content,
          reasoning_content: outcome.thinking,
        };
        if (outcome.toolCalls.length > 0) {
          assistantMsg.tool_calls = outcome.toolCalls.map((call) => ({
            id: call.id,
            type: call.type,
            function: {
              name: call.function.name,
              arguments: call.function.arguments,
            },
          }));
        }
        messages.push(assistantMsg);
        batch.push(assistantMsg);
        await this.appendSessionLog(sessionId, [assistantMsg]);

        if (outcome.toolCalls.length === 0) {
          completed = true;
          break;
        }

        for (const call of outcome.toolCalls) {
          const loaded = await this.executeToolCall(
            bus,
            sessionId,
            turnId,
            streamId,
            kind,
            call,
            messages,
            batch,
          );
          if (loaded) loadedSkills.add(loaded);
        }
      }

      if (!completed) {
        // Max iterations reached — force a final answer without tools.
        const outcome = await this.streamOnce(
          model,
          {
            messages: await this.hydrateMessages(messages),
            tools: undefined,
            systemPrompt,
            thinking: requestThinking,
            effort: requestEffort,
          },
          sessionId,
          turnId,
          streamId,
          this.turnSignal(sessionId),
        );
        finalContent = outcome.content || '（达到最大工具调用次数，未能完成回复）';
        finalThinking = outcome.thinking;
        const assistantMsg: MessageDTO = {
          role: 'assistant',
          content: finalContent,
          reasoning_content: finalThinking,
        };
        messages.push(assistantMsg);
        batch.push(assistantMsg);
        await this.appendSessionLog(sessionId, [assistantMsg]);
      }

      const fullHistory = await this.getHistory(sessionId);

      bus.publish(
        new TurnCompleted({
          ...this.agentContext(),
          sessionId,
          turnId,
          kind,
          content: finalContent,
          thinking: finalThinking,
          streamId,
          messages: fullHistory,
          messageBatch: batch,
        }),
      );

      if (this.autoReflect && kind === 'user' && loadedSkills.size === 0) {
        bus.publish(new ReflectSkills({ ...this.agentContext(), sessionId, auto: true }));
      }
      return { content: finalContent, ok: true };
    } catch (error) {
      bus.publish(new TurnFailed({ ...this.agentContext(), sessionId, turnId, error: errorText(error), streamId }));
      return { content: '', ok: false, error: errorText(error) };
    }
  }

  /** PromptModule owns the assembled prompt; Agent keeps a static fallback for tests. */
  private async assembledSystemPrompt(
    bus: AsyncEventBus,
    sessionId: string,
  ): Promise<string | undefined> {
    try {
      // Let queued prompt-section registrations (e.g. skills for this turn) apply.
      await bus.idle();
      const prompt = await bus.request(new GetSystemPrompt({ ...this.agentContext(), sessionId }));
      return prompt.trim() ? prompt : undefined;
    } catch {
      return undefined;
    }
  }

  private composeSystemPrompt(): string | undefined {
    return this.systemPrompt;
  }

  /** Prefer the event-sourced projection; fall back to memory for legacy tests. */
  private async getHistory(sessionId: string): Promise<MessageDTO[]> {
    const bus = this.requireBus();
    const projection = await bus.request(new GetSessionProjection({ ...this.agentContext(), sessionId }));
    return projection.messages;
  }

  /** Append raw events to the event-sourced session log when available. */
  private async appendSessionEvents(
    sessionId: string,
    events: Array<{ seq: number; type: string; at: string; data: Record<string, unknown> }>,
  ): Promise<boolean> {
    if (!sessionId || events.length === 0) return true;
    await this.requireBus().request(new AppendSessionEvents({ ...this.agentContext(), sessionId, events }));
    return true;
  }

  /** Append messages to the event-sourced session log when available. */
  private async appendSessionLog(
    sessionId: string,
    messages: MessageDTO[],
  ): Promise<boolean> {
    if (!sessionId || messages.length === 0) return true;
    return this.appendSessionEvents(
      sessionId,
      messages.map((message) =>
        messageEvent(
          message.role === 'user'
            ? 'user_message'
            : message.role === 'tool'
              ? 'tool_message'
              : 'assistant_message',
          message,
        ),
      ),
    );
  }

  /** Replace persisted image references with request-time data URLs. */
  private async hydrateMessages(messages: MessageDTO[]): Promise<MessageDTO[]> {
    const hydrated: MessageDTO[] = [];
    for (const message of ensureToolCallResponses(messages)) {
      if (!Array.isArray(message.content)) {
        hydrated.push(message);
        continue;
      }
      const parts = [];
      for (const part of message.content) {
        if (
          part.type === 'image_url' &&
          part.image_url.url.startsWith(ATTACHMENT_URL_PREFIX)
        ) {
          parts.push({
            type: 'image_url' as const,
            image_url: {
              url: await resolveAttachmentRef(
                part.image_url.url,
                this.imageMaxDimension,
              ),
            },
          });
        } else {
          parts.push(part);
        }
      }
      hydrated.push({ ...message, content: parts });
    }
    return hydrated;
  }

  private compactThresholdTokens(): number {
    if (this.contextWindowTokens <= 0) return 0;
    return Math.floor(this.contextWindowTokens * this.compactThresholdRatio);
  }

  private async compactIfNeeded(
    sessionId: string,
    turnId: string,
  ): Promise<CompactionResult | null> {
    const threshold = this.compactThresholdTokens();
    if (threshold <= 0 || this.lastPromptTokens < threshold) return null;
    const messages = await this.getHistory(sessionId);
    if (messages.length < 4) return null;
    return this.compactMessages(sessionId, messages, 'auto', turnId);
  }

  /** Summarize the oldest history region and keep a recent verbatim tail. */
  async compact(sessionId: string, reason: CompactionReason, turnId = ''): Promise<CompactionResult> {
    const bus = this.requireBus();
    const messages = await this.getHistory(sessionId);
    return this.compactMessages(sessionId, messages, reason, turnId);
  }

  private async compactMessages(
    sessionId: string,
    messages: MessageDTO[],
    reason: CompactionReason,
    turnId: string,
  ): Promise<CompactionResult> {
    const bus = this.requireBus();
    const threshold = this.compactThresholdTokens();
    const id = `compact-${Date.now().toString(36)}`;
    const window = this.contextWindowTokens > 0 ? this.contextWindowTokens : 32_768;
    const retainBudget = Math.max(256, Math.floor(window * this.compactRetainRatio));

    let cut = messages.length;
    let keptTokens = 0;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const cost = estimateMessageTokens(messages[index]!);
      if (keptTokens + cost > retainBudget) break;
      keptTokens += cost;
      cut = index;
    }
    // Keep the tail starting at a user turn so tool-call/result pairs stay intact.
    while (cut > 0 && messages[cut]?.role !== 'user') cut -= 1;
    const region = messages.slice(0, cut);
    const tail = messages.slice(cut);
    if (cut <= 0 || region.length < 2 || tail.length === 0) {
      return {
        id,
        reason,
        compactedMessages: 0,
        keptMessages: tail.length,
        freedTokens: 0,
        summary: '',
        thresholdTokens: threshold,
        skipped: true,
      };
    }

    bus.publish(new CompactionStarted({ ...this.agentContext(), id, reason, thresholdTokens: threshold, sessionId, turnId }));
    try {
      const summary = await this.summarizeRegion(region, id);
      if (!summary.trim()) throw new Error('summary was empty');
      const summaryMessage = userMessage(`<compacted-summary>\n${summary.trim()}\n</compacted-summary>`);
      const projected = [summaryMessage, ...tail];
      await this.appendSessionEvents(sessionId, [
        replaceMessagesEvent(projected),
      ]);
      const before = region.reduce((sum, message) => sum + estimateMessageTokens(message), 0);
      const after = estimateMessageTokens(summaryMessage);
      const result: CompactionResult = {
        id,
        reason,
        compactedMessages: region.length,
        keptMessages: tail.length,
        freedTokens: Math.max(0, before - after),
        summary: summary.trim(),
        thresholdTokens: threshold,
        skipped: false,
      };
      this.lastPromptTokens = 0;
      bus.publish(new ContextCompacted({ ...this.agentContext(), result, sessionId, turnId }));
      return result;
    } catch (error) {
      bus.publish(
        new CompactionFailed({
          ...this.agentContext(),
          id,
          reason,
          error: error instanceof Error ? error.message : String(error),
          sessionId,
          turnId,
        }),
      );
      throw error;
    }
  }

  private async summarizeRegion(region: MessageDTO[], id: string): Promise<string> {
    const model = this.model;
    const transcript = region
      .map((message) => {
        const content = Array.isArray(message.content)
          ? message.content
              .map((part) =>
                part.type === 'text' ? part.text : part.type === 'image_url' ? '[image]' : '[attachment]',
              )
              .join('\n')
          : typeof message.content === 'string'
            ? message.content
            : '';
        const calls = message.tool_calls
          ?.map(
            (call) =>
              `${call.function?.name ?? ''}(${call.function?.arguments ?? ''})`,
          )
          .join(', ');
        return `[${message.role}] ${content}${calls ? `\n  tool_calls: ${calls}` : ''}`;
      })
      .join('\n');
    if (model) {
      try {
        let summary = '';
        for await (const event of model.streamChat({
          systemPrompt:
            'You compact earlier agent conversation into a concise, information-dense summary. Keep facts, decisions, file paths, tool outcomes, and open tasks. Do not invent details.',
          messages: [
            userMessage(
              `Summarize the earlier conversation below for future context. Keep it under 400 words.\n\n${transcript}`,
            ),
          ],
          maxTokens: 1_024,
          thinking: false,
        })) {
          if (event.type === 'done') summary = event.result.content || summary;
          else if (event.type === 'content') summary += event.delta;
        }
        if (summary.trim()) return summary;
      } catch {
        // fall through to the deterministic fallback
      }
    }
    const bullets = region.slice(-24).map((message) => {
      const content = Array.isArray(message.content)
        ? message.content.map((part) => (part.type === 'text' ? part.text : '[image]')).join(' ')
        : typeof message.content === 'string'
          ? message.content
          : '';
      return `- ${message.role}: ${content.replace(/\s+/g, ' ').slice(0, 160)}`;
    });
    return `Compacted ${region.length} earlier messages (${id}):\n${bullets.join('\n')}`;
  }

  private async streamOnce(
    model: ChatModel,
    request: ChatRequest,
    sessionId: string,
    turnId: string,
    streamId: string,
    signal?: AbortSignal,
  ): Promise<{ content: string; thinking: string; toolCalls: ToolCall[] }> {
    const bus = this.requireBus();
    let content = '';
    let thinking = '';
    let toolCalls: ToolCall[] = [];
    let usage: ChatUsage | undefined;

    for await (const event of model.streamChat({ ...request, signal }) as AsyncGenerator<ChatStreamEvent>) {
      this.throwIfCancelled(sessionId);
      if (event.type === 'content') {
        content += event.delta;
        bus.publish(new TokenEmitted({ ...this.agentContext(), sessionId, turnId, delta: event.delta, streamId }));
      } else if (event.type === 'thinking') {
        thinking += event.delta;
        bus.publish(new ThinkingUpdated({ ...this.agentContext(), sessionId, turnId, delta: event.delta, streamId }));
      } else if (event.type === 'done') {
        content = event.result.content || content;
        thinking = event.result.thinking || thinking;
        toolCalls = event.result.toolCalls;
        usage = event.result.usage;
      }
    }
    if (usage) {
      this.lastPromptTokens = usage.promptTokens ?? this.lastPromptTokens;
      bus.publish(
        new TokenUsageReported({
          ...this.agentContext(),
          sessionId,
          turnId,
          streamId,
          promptTokens: usage.promptTokens ?? 0,
          completionTokens: usage.completionTokens ?? 0,
          totalTokens:
            usage.totalTokens ??
            (usage.promptTokens ?? 0) + (usage.completionTokens ?? 0),
          cacheHitTokens: usage.cacheHitTokens ?? 0,
          cacheMissTokens: usage.cacheMissTokens ?? 0,
        }),
      );
    }
    return { content, thinking, toolCalls };
  }

  private async executeToolCall(
    bus: AsyncEventBus,
    sessionId: string,
    turnId: string,
    streamId: string,
    kind: TurnKind,
    call: ToolCall,
    messages: MessageDTO[],
    batch: MessageDTO[],
  ): Promise<string | null> {
    const name = call.function?.name ?? '';
    const args = parseArguments(call.function?.arguments ?? '{}');
    const toolId = call.id || `call_${Math.random().toString(36).slice(2, 10)}`;

    bus.publish(
      new ToolStarted({
        ...this.agentContext(),
        sessionId,
        turnId,
        toolId,
        toolName: name,
        toolInput: args,
        streamId,
      }),
    );

    let result: ToolResult;
    const request = new ExecuteTool({
      ...this.agentContext(),
      sessionId,
      turnId,
      name,
      args,
      ctx: { sessionId, turnId, source: kind === 'cron' ? 'cron' : 'user' },
      signal: this.turnSignal(sessionId),
    });
    try {
      // Bus requests default to 30s, which would cut off long shell/MCP tools
      // before their own timeoutSeconds expires.
      result = await bus.request(
        request,
        Math.max(1, request.timeoutSeconds) * 1000 + 10_000,
      );
    } catch (error) {
      result = { name, error: errorText(error) };
    }

    const ok = !result.error;
    const outputText = ok ? (result.output ?? '') : `Error: ${result.error}`;

    bus.publish(
      new ToolFinished({
        ...this.agentContext(),
        sessionId,
        turnId,
        toolId,
        output: outputText,
        streamId,
      }),
    );

    const toolMsg: MessageDTO = {
      role: 'tool',
      tool_call_id: toolId,
      content: outputText,
    };
    messages.push(toolMsg);
    batch.push(toolMsg);
    await this.appendSessionLog(sessionId, [toolMsg]);

    if (name !== 'load_skill' || !ok) return null;
    const skillName = String((args as Record<string, unknown>).skill_name ?? '');
    try {
      const card = await bus.request(new LoadSkill({ ...this.agentContext(), sessionId, skillName }));
      if (!card?.name) return null;
      bus.publish(
        new SkillLoaded({
          ...this.agentContext(),
          sessionId,
          turnId,
          skillName: card.name,
          skillPattern: card.pattern ?? '',
          streamId,
        }),
      );
      return card.name;
    } catch {
      return null;
    }
  }
}