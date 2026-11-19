/**
 * Pure view-model folding for the bus-driven TUI.
 *
 * The TUI module subscribes to bus events and feeds them here; a renderer
 * (OpenTUI, tests, future web UI) only reads the resulting snapshots.
 */

import type { Event } from '../../kernel/message';
import type { AttachmentDTO, MessageDTO } from '../../kernel/dto/message';
import {
  ThinkingUpdated,
  TokenEmitted,
  TokenUsageReported,
  TurnCompleted,
  TurnFailed,
  TurnStarted,
  type TurnKind,
} from '../../kernel/contracts/chat';
import { SkillLoaded, SkillsReflected } from '../../kernel/contracts/skills';
import {
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolFinished,
  ToolStarted,
} from '../../kernel/contracts/tools';
import { CronJobEvent } from '../../kernel/contracts/cron';
import {
  CompactionFailed,
  CompactionStarted,
  ContextCompacted,
  type CompactionReason,
} from '../../kernel/contracts/compaction';

export type TurnStatus = 'streaming' | 'done' | 'failed';

export interface ToolCallView {
  toolId: string;
  name: string;
  input: unknown;
  output: string;
  done: boolean;
}

export interface SkillView {
  name: string;
  pattern: string;
}

export type CompactionPart = {
  kind: 'compaction';
  id: string;
  reason: CompactionReason;
  status: 'running' | 'done' | 'failed';
  text: string;
  detail: string;
  summary: string;
};

export type TurnPart =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'skills'; skills: SkillView[] }
  | { kind: 'tool'; tool: ToolCallView }
  | CompactionPart;

export interface TurnView {
  turnId: string;
  sessionId: string;
  kind: TurnKind | 'compaction';
  userInput: string;
  attachments: AttachmentDTO[];
  content: string;
  thinking: string;
  tools: ToolCallView[];
  skills: SkillView[];
  /** Chronological rendering order: text → tool → text → … */
  parts: TurnPart[];
  status: TurnStatus;
  error?: string;
}

export interface ApprovalView {
  reqId: string;
  toolName: string;
  permission: string;
  preview: string;
}

export interface UsageRecord {
  at: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cacheHitTokens: number;
  cacheMissTokens: number;
}

export interface SessionUsage {
  requests: number;
  inputTokens: number;
  outputTokens: number;
  cacheHitTokens: number;
  cacheMissTokens: number;
}

export interface ReflectionRecord {
  at: number;
  created: number;
  updated: number;
  deprecated: number;
  names: string[];
  updatedNames: string[];
}

export interface CronEventView {
  jobId: string;
  name: string;
  status: string;
  error: string;
}

function emptyTurn(event: TurnStarted): TurnView {
  return {
    turnId: event.turnId,
    sessionId: event.sessionId,
    kind: event.kind,
    userInput: event.userInput,
    attachments: event.attachments,
    content: '',
    thinking: '',
    tools: [],
    skills: [],
    parts: [],
    status: 'streaming',
  };
}

function appendTextPart(turn: TurnView, kind: 'text' | 'thinking', delta: string): void {
  const last = turn.parts[turn.parts.length - 1];
  if (last?.kind === kind) {
    last.text += delta;
    return;
  }
  turn.parts.push({ kind, text: delta });
}

function appendSkillPart(turn: TurnView, skill: SkillView): void {
  const last = turn.parts[turn.parts.length - 1];
  if (last?.kind === 'skills') {
    last.skills.push(skill);
    return;
  }
  turn.parts.push({ kind: 'skills', skills: [skill] });
}

export class TuiViewState {
  private readonly history: TurnView[] = [];
  private readonly active = new Map<string, TurnView>();
  private readonly cronEvents: CronEventView[] = [];

  private findCompactionPart(id: string): CompactionPart | null {
    const turns = [...this.history, ...this.active.values()];
    for (const turn of turns) {
      for (const part of turn.parts) {
        if (part.kind === 'compaction' && part.id === id) return part;
      }
    }
    return null;
  }
  private readonly approvals: ApprovalView[] = [];
  lastReflection: SkillsReflected | null = null;
  lastUsage: UsageRecord | null = null;
  private usageTotals: SessionUsage = {
    requests: 0,
    inputTokens: 0,
    outputTokens: 0,
    cacheHitTokens: 0,
    cacheMissTokens: 0,
  };

  get sessionUsage(): SessionUsage {
    return { ...this.usageTotals };
  }
  private readonly reflections: ReflectionRecord[] = [];

  get reflectionHistory(): ReflectionRecord[] {
    return [...this.reflections];
  }

  get turns(): TurnView[] {
    return [...this.history, ...this.active.values()];
  }

  get streaming(): TurnView[] {
    return [...this.active.values()];
  }

  get cron(): CronEventView[] {
    return [...this.cronEvents];
  }

  get pendingApprovals(): ApprovalView[] {
    return [...this.approvals];
  }

  findTurn(turnId: string): TurnView | undefined {
    return this.active.get(turnId) ?? this.history.find((turn) => turn.turnId === turnId);
  }

  reset(): void {
    this.history.length = 0;
    this.active.clear();
    this.cronEvents.length = 0;
    this.approvals.length = 0;
    this.lastReflection = null;
    this.lastUsage = null;
    this.usageTotals = {
      requests: 0,
      inputTokens: 0,
      outputTokens: 0,
      cacheHitTokens: 0,
      cacheMissTokens: 0,
    };
    this.reflections.length = 0;
  }

  /** Rebuild the view from a persisted message history (session resume). */
  loadHistory(messages: MessageDTO[]): void {
    this.reset();
    let current: TurnView | null = null;
    const flush = () => {
      if (current) this.history.push(current);
      current = null;
    };

    for (const message of messages) {
      if (message.role === 'user') {
        flush();
        current = {
          turnId: `history-${this.history.length + 1}`,
          sessionId: '',
          kind: 'user',
          userInput:
            typeof message.content === 'string'
              ? message.content
              : (message.content ?? [])
                  .filter((part) => part.type === 'text')
                  .map((part) => part.text)
                  .join('\n'),
          attachments: Array.isArray(message.attachments)
            ? (message.attachments as AttachmentDTO[])
            : [],
          content: '',
          thinking: '',
          tools: [],
          skills: [],
          parts: [],
          status: 'done',
        };
      } else if (message.role === 'assistant') {
        if (!current) {
          current = {
            turnId: `history-${this.history.length + 1}`,
            sessionId: '',
            kind: 'user',
            userInput: '',
            attachments: [],
            content: '',
            thinking: '',
            tools: [],
            skills: [],
            parts: [],
            status: 'done',
          };
        }
        if (typeof message.reasoning_content === 'string' && message.reasoning_content) {
          current.thinking += message.reasoning_content;
          appendTextPart(current, 'thinking', message.reasoning_content);
        }
        if (typeof message.content === 'string' && message.content) {
          current.content += message.content;
          appendTextPart(current, 'text', message.content);
        }
        for (const [index, call] of (message.tool_calls ?? []).entries()) {
          const fn = call.function ?? {};
          let input: unknown = fn.arguments ?? '';
          try {
            input = fn.arguments ? JSON.parse(fn.arguments) : {};
          } catch {
            // keep raw arguments
          }
          const tool: ToolCallView = {
            toolId: String(call.id ?? `history-tool-${index}`),
            name: String(fn.name ?? ''),
            input,
            output: '',
            done: false,
          };
          current.tools.push(tool);
          current.parts.push({ kind: 'tool', tool });
        }
      } else if (message.role === 'tool') {
        if (!current) continue;
        const toolCallId = String(message.tool_call_id ?? '');
        const tool =
          current.tools.find((item) => item.toolId === toolCallId) ??
          current.tools[current.tools.length - 1];
        if (tool) {
          tool.output = typeof message.content === 'string' ? message.content : '';
          tool.done = true;
        }
      }
    }
    flush();
  }

  apply(event: Event): void {
    if (event instanceof TurnStarted) {
      this.active.set(event.turnId, emptyTurn(event));
      return;
    }
    if (event instanceof TokenEmitted) {
      const turn = this.ensureTurn(event.turnId, event.sessionId);
      turn.content += event.delta;
      appendTextPart(turn, 'text', event.delta);
      return;
    }
    if (event instanceof ThinkingUpdated) {
      const turn = this.ensureTurn(event.turnId, event.sessionId);
      turn.thinking += event.delta;
      appendTextPart(turn, 'thinking', event.delta);
      return;
    }
    if (event instanceof SkillLoaded) {
      const turn = this.ensureTurn(event.turnId, event.sessionId);
      const skill = { name: event.skillName, pattern: event.skillPattern };
      turn.skills.push(skill);
      appendSkillPart(turn, skill);
      return;
    }
    if (event instanceof ToolStarted) {
      const turn = this.ensureTurn(event.turnId, event.sessionId);
      const tool: ToolCallView = {
        toolId: event.toolId,
        name: event.toolName,
        input: event.toolInput,
        output: '',
        done: false,
      };
      turn.tools.push(tool);
      turn.parts.push({ kind: 'tool', tool });
      return;
    }
    if (event instanceof ToolFinished) {
      const turn = this.ensureTurn(event.turnId, event.sessionId);
      const tool = turn.tools.find((item) => item.toolId === event.toolId);
      if (tool) {
        tool.output = stringifyOutput(event.output);
        tool.done = true;
      }
      return;
    }
    if (event instanceof TurnCompleted) {
      const turn: TurnView = this.active.get(event.turnId) ?? {
        turnId: event.turnId,
        sessionId: event.sessionId,
        kind: event.kind,
        userInput: '',
        attachments: [],
        content: '',
        thinking: '',
        tools: [],
        skills: [],
        parts: [],
        status: 'streaming',
      };
      if (event.thinking && !turn.thinking) {
        // Aggregated thinking arrives after the stream; show it above the answer.
        turn.thinking = event.thinking;
        turn.parts.unshift({ kind: 'thinking', text: event.thinking });
      }
      if (event.content && !turn.content) {
        turn.content = event.content;
        appendTextPart(turn, 'text', event.content);
      }
      turn.status = 'done';
      this.finalize(turn);
      return;
    }
    if (event instanceof TurnFailed) {
      const turn = this.ensureTurn(event.turnId, event.sessionId);
      turn.status = 'failed';
      turn.error = event.error;
      this.finalize(turn);
      return;
    }
    if (event instanceof ToolApprovalRequested) {
      this.approvals.push({
        reqId: event.reqId,
        toolName: event.toolName,
        permission: event.permission,
        preview: event.preview,
      });
      return;
    }
    if (event instanceof ToolApprovalResolved) {
      const index = this.approvals.findIndex((item) => item.reqId === event.reqId);
      if (index >= 0) this.approvals.splice(index, 1);
      return;
    }
    if (event instanceof CompactionStarted) {
      const part: CompactionPart = {
        kind: 'compaction',
        id: event.id,
        reason: event.reason,
        status: 'running',
        text: `正在压缩上下文（${event.reason === 'auto' ? '自动' : '手动'}）…`,
        detail: '',
        summary: '',
      };
      const activeTurn = [...this.active.values()].find(
        (turn) => turn.sessionId === event.sessionId,
      );
      if (activeTurn) {
        activeTurn.parts.push(part);
      } else {
        this.history.push({
          turnId: event.id,
          sessionId: event.sessionId,
          kind: 'compaction',
          userInput: '',
          attachments: [],
          content: '',
          thinking: '',
          tools: [],
          skills: [],
          parts: [part],
          status: 'done',
        });
      }
      return;
    }
    if (event instanceof ContextCompacted) {
      const part = this.findCompactionPart(event.result.id);
      if (!part) return;
      const { result } = event;
      part.status = 'done';
      part.text = result.skipped
        ? '无需压缩（历史太短或没有可安全压缩的内容）'
        : `上下文已压缩（${result.reason === 'auto' ? '自动' : '手动'}）`;
      part.detail = result.skipped
        ? ''
        : `${result.compactedMessages} 条消息 → 摘要 · 保留 ${result.keptMessages} 条 · 释放 ~${result.freedTokens.toLocaleString()} tokens`;
      part.summary = result.summary;
      return;
    }
    if (event instanceof CompactionFailed) {
      const part = this.findCompactionPart(event.id);
      if (!part) return;
      part.status = 'failed';
      part.text = '上下文压缩失败';
      part.detail = event.error;
      return;
    }
    if (event instanceof TokenUsageReported) {
      this.lastUsage = {
        at: Date.now(),
        promptTokens: event.promptTokens,
        completionTokens: event.completionTokens,
        totalTokens: event.totalTokens,
        cacheHitTokens: event.cacheHitTokens,
        cacheMissTokens: event.cacheMissTokens,
      };
      this.usageTotals.requests += 1;
      this.usageTotals.inputTokens += event.promptTokens;
      this.usageTotals.outputTokens += event.completionTokens;
      this.usageTotals.cacheHitTokens += event.cacheHitTokens;
      this.usageTotals.cacheMissTokens += event.cacheMissTokens;
      return;
    }
    if (event instanceof SkillsReflected) {
      this.lastReflection = event;
      this.reflections.push({
        at: Date.now(),
        created: event.created,
        updated: event.updated,
        deprecated: event.deprecated,
        names: [...event.names],
        updatedNames: [...event.updatedNames],
      });
      if (this.reflections.length > 50) this.reflections.shift();
      return;
    }
    if (event instanceof CronJobEvent) {
      this.cronEvents.push({
        jobId: event.jobId,
        name: event.name,
        status: event.status,
        error: event.error,
      });
      if (this.cronEvents.length > 50) this.cronEvents.shift();
    }
  }

  private ensureTurn(turnId: string, sessionId: string): TurnView {
    let turn = turnId ? this.active.get(turnId) : undefined;
    if (!turn) {
      turn = {
        turnId,
        sessionId,
        kind: 'user',
        userInput: '',
        attachments: [],
        content: '',
        thinking: '',
        tools: [],
        skills: [],
        parts: [],
        status: 'streaming',
      };
      if (turnId) this.active.set(turnId, turn);
    }
    return turn;
  }

  private finalize(turn: TurnView): void {
    if (turn.turnId) this.active.delete(turn.turnId);
    this.history.push(turn);
  }
}

function stringifyOutput(output: unknown): string {
  if (output === undefined || output === null) return '';
  return typeof output === 'string' ? output : JSON.stringify(output);
}

export type { MessageDTO };