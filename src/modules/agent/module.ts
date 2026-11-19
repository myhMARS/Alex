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
import type { ModelEffort } from '../../kernel/contracts/settings';
import { assistantMessage, userMessage } from '../../kernel/dto/message';
import type { ToolSpec } from '../../kernel/dto/tool';
import type { SkillCard } from '../../kernel/dto/skill';
import {
  LoadSkill,
  ReflectSkills,
  RetrieveSkills,
  SkillLoaded,
} from '../../kernel/contracts/skills';
import {
  ThinkingUpdated,
  TokenEmitted,
  GetSystemPrompt,
  SetModelEffort,
  TokenUsageReported,
  TurnCompleted,
  TurnFailed,
  TurnStarted,
  UserTurnRequested,
  type TurnKind,
} from '../../kernel/contracts/chat';
import {
  AppendMessages,
  GetContext,
  ReplaceMemory,
} from '../../kernel/contracts/memory';
import { estimateMessageTokens } from '../memory/module';
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
import { skillsSectionPrompt } from '../../prompts/skills-section';

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
  readonly dependencies: readonly string[] = ['memory', 'tools', 'skill'];

  private readonly model: ChatModel | null;
  private readonly responder: Responder;
  private readonly maxIterations: number;
  private readonly systemPrompt: string | undefined;
  private readonly autoReflect: boolean;
  private readonly contextWindowTokens: number;
  private readonly compactThresholdRatio: number;
  private readonly compactRetainRatio: number;
  private lastPromptTokens = 0;
  private thinkingEnabled = true;
  private effort: ModelEffort = 'high';
  private readonly tasks = new Set<Promise<void>>();
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
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    bus.provide(GetSystemPrompt, () => this.systemPrompt ?? '');
    bus.provide(CompactContext, (req) => this.compact(req.sessionId, 'manual'));
    bus.provide(SetModelEffort, (req) => {
      this.thinkingEnabled = req.thinkingEnabled;
      this.effort = req.effort;
      return { thinkingEnabled: this.thinkingEnabled, effort: this.effort };
    });
    await bus.subscribe(UserTurnRequested, (cmd) => {
      this.schedule(this.runCore({
        sessionId: cmd.sessionId,
        userText: cmd.userText,
        kind: 'user',
        streamId: '',
        attachments: cmd.attachments,
      }));
    });
    await bus.subscribe(CronTurnRequested, (cmd) => {
      const trigger = cmd.trigger ?? {};
      this.schedule(this.runCore({
        sessionId: cmd.sessionId || String(trigger.session_id ?? ''),
        userText: String(trigger.prompt ?? ''),
        kind: 'cron',
        streamId: String(trigger.stream_id ?? ''),
        attachments: [],
      }));
    });
  }

  async stop(): Promise<void> {
    await this.whenIdle();
    this.bus = null;
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

  private schedule(task: Promise<void>): void {
    this.tasks.add(task);
    void task.finally(() => this.tasks.delete(task));
  }

  private requireBus(): AsyncEventBus {
    if (!this.bus) throw new Error('AgentModule.start() has not been called');
    return this.bus;
  }

  private async runCore(params: TurnParams): Promise<void> {
    if (this.model) {
      await this.runModelTurn(params);
    } else {
      await this.runResponderTurn(params);
    }
  }

  // ── responder mode (offline / deterministic) ───────────────────────────

  private async runResponderTurn(params: TurnParams): Promise<void> {
    const bus = this.requireBus();
    const turnId = nextTurnId();
    const { sessionId, userText, kind, streamId, attachments } = params;

    bus.publish(
      new TurnStarted({
        sessionId,
        turnId,
        kind,
        userInput: userText,
        attachments,
        source: kind === 'cron' ? 'cron' : 'user',
      }),
    );

    try {
      await this.compactIfNeeded(sessionId, turnId).catch(() => undefined);
      const history = await bus.request(new GetContext({ sessionId }));
      const userMsg = userMessage(buildUserContent(userText, attachments));
      if (attachments.length > 0) userMsg.attachments = attachments;
      if (kind === 'cron') userMsg.alex_turn_kind = 'cron';
      await bus.request(new AppendMessages({ sessionId, messages: [userMsg] }));

      let content = '';
      for await (const delta of this.responder.respond({
        text: userText || `[${attachments.length} attachment(s)]`,
        sessionId,
        turnId,
        history,
      })) {
        if (!delta) continue;
        content += delta;
        bus.publish(new TokenEmitted({ sessionId, turnId, delta, streamId }));
      }

      const assistantMsg = assistantMessage(content);
      await bus.request(new AppendMessages({ sessionId, messages: [assistantMsg] }));

      bus.publish(
        new TurnCompleted({
          sessionId,
          turnId,
          kind,
          content,
          streamId,
          messages: [userMsg, assistantMsg],
          messageBatch: [userMsg, assistantMsg],
        }),
      );
    } catch (error) {
      bus.publish(new TurnFailed({ sessionId, turnId, error: errorText(error), streamId }));
    }
  }

  // ── model mode (real agent loop) ───────────────────────────────────────

  private async runModelTurn(params: TurnParams): Promise<void> {
    const bus = this.requireBus();
    const model = this.model;
    if (!model) return;
    const { sessionId, userText, kind, streamId, attachments } = params;
    const turnId = nextTurnId();

    bus.publish(
      new TurnStarted({
        sessionId,
        turnId,
        kind,
        userInput: userText,
        attachments,
        source: kind === 'cron' ? 'cron' : 'user',
      }),
    );

    try {
      await this.compactIfNeeded(sessionId, turnId).catch(() => undefined);
      const history = await bus.request(new GetContext({ sessionId }));
      const userMsg = userMessage(buildUserContent(userText, attachments));
      if (attachments.length > 0) userMsg.attachments = attachments;
      if (kind === 'cron') userMsg.alex_turn_kind = 'cron';
      await bus.request(new AppendMessages({ sessionId, messages: [userMsg] }));

      const messages: MessageDTO[] = [...history, userMsg];
      const batch: MessageDTO[] = [];
      const catalog = await bus.request(new GetToolCatalog());
      const tools = catalog.map(toToolSchema);
      const skills = await this.retrieveSkills(bus, sessionId, userText);
      const systemPrompt = this.composeSystemPrompt(skills);
      const loadedSkills = new Set<string>();

      let finalContent = '';
      let finalThinking = '';
      let completed = false;

      for (let iteration = 0; iteration < this.maxIterations && !completed; iteration += 1) {
        const outcome = await this.streamOnce(
          model,
          {
            messages,
            tools: tools.length > 0 ? tools : undefined,
            systemPrompt,
            thinking: this.thinkingEnabled,
            effort: this.effort,
          },
          sessionId,
          turnId,
          streamId,
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
            messages,
            tools: undefined,
            systemPrompt,
            thinking: this.thinkingEnabled,
            effort: this.effort,
          },
          sessionId,
          turnId,
          streamId,
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
      }

      await bus.request(new AppendMessages({ sessionId, messages: batch }));
      const fullHistory = await bus.request(new GetContext({ sessionId }));

      bus.publish(
        new TurnCompleted({
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
        bus.publish(new ReflectSkills({ sessionId }));
      }
    } catch (error) {
      bus.publish(new TurnFailed({ sessionId, turnId, error: errorText(error), streamId }));
    }
  }

  private async retrieveSkills(
    bus: AsyncEventBus,
    sessionId: string,
    query: string,
  ): Promise<SkillCard[]> {
    try {
      return await bus.request(new RetrieveSkills({ sessionId, query, topK: 3 }));
    } catch {
      return [];
    }
  }

  private composeSystemPrompt(skills: SkillCard[]): string | undefined {
    if (skills.length === 0) return this.systemPrompt;
    const section = skillsSectionPrompt(skills);
    return this.systemPrompt?.trim()
      ? `${this.systemPrompt.trim()}\n${section}`
      : section.trim();
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
    const messages = await this.requireBus().request(new GetContext({ sessionId }));
    if (messages.length < 4) return null;
    return this.compactMessages(sessionId, messages, 'auto', turnId);
  }

  /** Summarize the oldest history region and keep a recent verbatim tail. */
  async compact(sessionId: string, reason: CompactionReason, turnId = ''): Promise<CompactionResult> {
    const bus = this.requireBus();
    const messages = await bus.request(new GetContext({ sessionId }));
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

    bus.publish(new CompactionStarted({ id, reason, thresholdTokens: threshold, sessionId, turnId }));
    try {
      const summary = await this.summarizeRegion(region, id);
      if (!summary.trim()) throw new Error('summary was empty');
      const summaryMessage = userMessage(`<compacted-summary>\n${summary.trim()}\n</compacted-summary>`);
      await bus.request(
        new ReplaceMemory({ sessionId, messages: [summaryMessage, ...tail] }),
      );
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
      bus.publish(new ContextCompacted({ result, sessionId, turnId }));
      return result;
    } catch (error) {
      bus.publish(
        new CompactionFailed({
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
  ): Promise<{ content: string; thinking: string; toolCalls: ToolCall[] }> {
    const bus = this.requireBus();
    let content = '';
    let thinking = '';
    let toolCalls: ToolCall[] = [];
    let usage: ChatUsage | undefined;

    for await (const event of model.streamChat(request) as AsyncGenerator<ChatStreamEvent>) {
      if (event.type === 'content') {
        content += event.delta;
        bus.publish(new TokenEmitted({ sessionId, turnId, delta: event.delta, streamId }));
      } else if (event.type === 'thinking') {
        thinking += event.delta;
        bus.publish(new ThinkingUpdated({ sessionId, turnId, delta: event.delta, streamId }));
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
        sessionId,
        turnId,
        toolId,
        toolName: name,
        toolInput: args,
        streamId,
      }),
    );

    const result = await bus.request(
      new ExecuteTool({
        sessionId,
        turnId,
        name,
        args,
        ctx: { sessionId, turnId, source: kind === 'cron' ? 'cron' : 'user' },
      }),
    );

    const ok = !result.error;
    const outputText = ok ? (result.output ?? '') : `Error: ${result.error}`;

    bus.publish(
      new ToolFinished({
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

    if (name !== 'load_skill' || !ok) return null;
    const skillName = String((args as Record<string, unknown>).skill_name ?? '');
    try {
      const card = await bus.request(new LoadSkill({ sessionId, skillName }));
      if (!card?.name) return null;
      bus.publish(
        new SkillLoaded({
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