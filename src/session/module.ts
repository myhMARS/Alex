/** Session log + projection module (single event/seq source of truth). */

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import {
  AppendSessionEvents,
  GetSessionProjection,
  ReplaceSessionEvents,
  SessionEventAppended,
  type SessionEventRecord,
} from '../kernel/contracts/session';
import { TokenUsageReported } from '../kernel/contracts/chat';
import {
  CompactionFailed,
  CompactionStarted,
  ContextCompacted,
} from '../kernel/contracts/compaction';
import {
  ToolApprovalRequested,
  ToolApprovalResolved,
  ToolFinished,
  ToolStarted,
} from '../kernel/contracts/tools';
import { SkillLoaded, SkillsReflected } from '../kernel/contracts/skills';
import { projectSessionSurface } from './projector';

export class SessionModule implements Module {
  readonly name = 'session';
  readonly dependencies: readonly string[] = [];

  private readonly logs = new Map<string, SessionEventRecord[]>();

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    bus.provide(AppendSessionEvents, (req) => ({
      appended: this.append(req.sessionId, req.events).length,
    }));
    bus.provide(ReplaceSessionEvents, (req) => {
      this.logs.set(req.sessionId, this.renumber(req.events));
      return { replaced: req.events.length };
    });
    bus.provide(GetSessionProjection, (req) =>
      projectSessionSurface([...(this.logs.get(req.sessionId) ?? [])]),
    );

    await bus.subscribe(TokenUsageReported, (event) => {
      this.append(event.sessionId, [
        this.record('usage', {
          promptTokens: event.promptTokens,
          completionTokens: event.completionTokens,
          totalTokens: event.totalTokens,
          cacheHitTokens: event.cacheHitTokens,
          cacheMissTokens: event.cacheMissTokens,
          streamId: event.streamId,
        }),
      ]);
    });
    await bus.subscribe(CompactionStarted, (event) => {
      this.append(event.sessionId, [
        this.record('compaction_started', {
          id: event.id,
          reason: event.reason,
          thresholdTokens: event.thresholdTokens,
        }),
      ]);
    });
    await bus.subscribe(ContextCompacted, (event) => {
      this.append(event.sessionId, [
        this.record('context_compacted', { result: event.result }),
      ]);
    });
    await bus.subscribe(CompactionFailed, (event) => {
      this.append(event.sessionId, [
        this.record('compaction_failed', {
          id: event.id,
          reason: event.reason,
          error: event.error,
        }),
      ]);
    });
    await bus.subscribe(ToolApprovalRequested, (event) => {
      this.append(event.sessionId, [
        this.record('approval_requested', {
          reqId: event.reqId,
          toolName: event.toolName,
          permission: event.permission,
        }),
      ]);
    });
    await bus.subscribe(ToolApprovalResolved, (event) => {
      this.append(event.sessionId, [
        this.record('approval_resolved', {
          reqId: event.reqId,
          granted: event.granted,
          remember: event.remember,
        }),
      ]);
    });
    await bus.subscribe(ToolStarted, (event) => {
      this.append(event.sessionId, [
        this.record('tool_started', {
          turnId: event.turnId,
          toolId: event.toolId,
          toolName: event.toolName,
          input: event.toolInput,
        }),
      ]);
    });
    await bus.subscribe(ToolFinished, (event) => {
      this.append(event.sessionId, [
        this.record('tool_finished', {
          turnId: event.turnId,
          toolId: event.toolId,
          output: event.output,
        }),
      ]);
    });
    await bus.subscribe(SkillLoaded, (event) => {
      this.append(event.sessionId, [
        this.record('skill_loaded', {
          name: event.skillName,
          pattern: event.skillPattern,
        }),
      ]);
    });
    await bus.subscribe(SkillsReflected, (event) => {
      this.append(event.sessionId, [
        this.record('skills_reflected', {
          created: event.created,
          updated: event.updated,
          deprecated: event.deprecated,
          names: event.names,
          updatedNames: event.updatedNames,
        }),
      ]);
    });
  }

  /** Append events with canonical seq + headerSeq enrichment. */
  append(sessionId: string, input: SessionEventRecord[]): SessionEventRecord[] {
    if (!sessionId || input.length === 0) return [];
    const log = this.logs.get(sessionId) ?? [];
    let nextSeq = log.length > 0 ? log[log.length - 1]!.seq + 1 : 1;
    let headerSeq = 0;
    for (const event of log) {
      if (event.type === 'request_header') headerSeq = event.seq;
    }
    const enriched = input.map((event) => {
      const seq = nextSeq++;
      let data = event.data ?? {};
      if (
        (event.type === 'assistant_message' || event.type === 'tool_message') &&
        headerSeq > 0 &&
        typeof data.headerSeq !== 'number'
      ) {
        data = { ...data, headerSeq };
      }
      if (event.type === 'request_header') headerSeq = seq;
      return { ...event, seq, data };
    });
    log.push(...enriched);
    if (log.length > 20_000) log.splice(0, log.length - 20_000);
    this.logs.set(sessionId, log);
    this.publish(sessionId, enriched);
    return enriched;
  }

  /** Renumber a restored log and recompute missing headerSeq links. */
  private renumber(events: SessionEventRecord[]): SessionEventRecord[] {
    let headerSeq = 0;
    return events.map((event, index) => {
      const seq = index + 1;
      let data = event.data ?? {};
      if (
        (event.type === 'assistant_message' || event.type === 'tool_message') &&
        headerSeq > 0 &&
        typeof data.headerSeq !== 'number'
      ) {
        data = { ...data, headerSeq };
      }
      if (event.type === 'request_header') headerSeq = seq;
      return { ...event, seq, data };
    });
  }

  events(sessionId: string): SessionEventRecord[] {
    return [...(this.logs.get(sessionId) ?? [])];
  }

  private record(type: string, data: Record<string, unknown>): SessionEventRecord {
    return { seq: 0, type, at: new Date().toISOString(), data };
  }

  private publish(sessionId: string, events: SessionEventRecord[]): void {
    if (events.length === 0) return;
    // The bus is captured by the closure in start(); this field is optional and
    // replaced by direct publish in append callers.
    this.bus?.publish(new SessionEventAppended({ sessionId, events }));
  }

  private bus: AsyncEventBus | null = null;
}