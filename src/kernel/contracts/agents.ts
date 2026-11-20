/** Agent registry contracts — spawn, inspect, message and dispose team members. */

import { Event, Request, type MessageContext } from '../message';

export interface AgentSpawnSpec {
  agentId?: string;
  parentAgentId?: string;
  teamId?: string;
  sessionId?: string;
  role?: string;
}

export interface AgentInfo {
  agentId: string;
  parentAgentId: string;
  teamId: string;
  sessionId: string;
  role: string;
  status: 'idle' | 'running' | 'disposed' | 'resumable';
}

export interface AgentMailboxEntry {
  messageId: string;
  fromAgentId: string;
  toAgentId: string;
  teamId: string;
  content: string;
  replyTo?: string;
  /** Session that sent the message. */
  fromSessionId?: string;
  /** Session that should consume the message. */
  toSessionId?: string;
  at: string;
  /** Whether this message has been acknowledged. */
  read?: boolean;
  /** Replay attempts so far. */
  attempts?: number;
  /** Last failure reason, if any. */
  failureReason?: string;
}

export class ListAgentMessages extends Request<AgentMailboxEntry[]> {
  targetAgentId: string;
  limit: number;
  unreadOnly: boolean;

  constructor(
    init: {
      agentId?: string;
      limit?: number;
      unreadOnly?: boolean;
      sessionId?: string;
    } = {},
  ) {
    super(init);
    this.targetAgentId = init.agentId ?? '';
    this.limit = init.limit ?? 20;
    this.unreadOnly = init.unreadOnly ?? false;
  }
}

export interface AgentMailboxStatus {
  total: number;
  read: number;
  unread: number;
  lastReadMessageId?: string;
  lastReadAt?: string;
  dead?: number;
}

export class GetAgentMailboxStatus extends Request<AgentMailboxStatus> {
  targetAgentId: string;

  constructor(init: { agentId?: string; sessionId?: string } = {}) {
    super(init);
    this.targetAgentId = init.agentId ?? '';
  }
}

export class AckAgentMessages extends Request<{ acked: number }> {
  targetAgentId: string;
  messageIds?: string[];

  constructor(
    init: {
      agentId?: string;
      messageIds?: string[];
      sessionId?: string;
    } = {},
  ) {
    super(init);
    this.targetAgentId = init.agentId ?? '';
    this.messageIds = init.messageIds;
  }
}

export class SpawnAgent extends Request<AgentInfo> {
  spec: AgentSpawnSpec;

  constructor(
    init: { spec?: AgentSpawnSpec; agentId?: string; sessionId?: string } = {},
  ) {
    super(init);
    this.spec = { ...init.spec, agentId: init.spec?.agentId ?? init.agentId };
  }
}

export interface TeamLimits {
  teamId: string;
  maxAgents?: number;
  maxConcurrentTasks?: number;
}

export class SetTeamLimits extends Request<TeamLimits> {
  limits: TeamLimits;

  constructor(init: { limits?: TeamLimits; sessionId?: string } = {}) {
    super(init);
    this.limits = init.limits ?? { teamId: '' };
  }
}

export class GetTeamLimits extends Request<TeamLimits[]> {}

export class ResumeAgent extends Request<AgentInfo> {
  override agentId: string;
  override sessionId: string;

  constructor(init: { agentId?: string; sessionId?: string } = {}) {
    super(init);
    this.agentId = init.agentId ?? '';
    this.sessionId = init.sessionId ?? '';
  }
}

export class ListAgents extends Request<AgentInfo[]> {
  override teamId: string;
  includeResumable: boolean;

  constructor(
    init: { teamId?: string; includeResumable?: boolean; sessionId?: string } = {},
  ) {
    super(init);
    this.teamId = init.teamId ?? '';
    this.includeResumable = init.includeResumable ?? false;
  }
}

export class GetAgent extends Request<AgentInfo | null> {
  override agentId: string;

  constructor(init: { agentId?: string; sessionId?: string } = {}) {
    super(init);
    this.agentId = init.agentId ?? '';
  }
}

export class DisposeAgent extends Request<boolean> {
  override agentId: string;

  constructor(init: { agentId?: string; sessionId?: string } = {}) {
    super(init);
    this.agentId = init.agentId ?? '';
  }
}

export class SendAgentMessage extends Request<{ messageId: string }> {
  fromAgentId: string;
  toAgentId: string;
  content: string;
  replyTo?: string;

  constructor(
    init: MessageContext & {
      fromAgentId?: string;
      toAgentId?: string;
      content?: string;
      replyTo?: string;
    } = {},
  ) {
    super(init);
    this.fromAgentId = init.fromAgentId ?? init.agentId ?? '';
    this.toAgentId = init.toAgentId ?? init.targetAgentId ?? '';
    this.content = init.content ?? '';
    this.replyTo = init.replyTo;
  }
}

export interface AgentTaskInfo {
  taskId: string;
  agentId: string;
  sessionId: string;
  prompt: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  result: string;
  error: string;
  /** Session whose tool call spawned this task. */
  parentSessionId?: string;
  /** Team that owns the worker. */
  teamId?: string;
  /** Scheduling priority (higher runs first when a team is saturated). */
  priority?: number;
  /** Coarse failure classification: timeout / cancelled / agent_error / tool_error. */
  failureReason?: string;
  /** Execution attempt count (1 for a non-retried task). */
  attempts?: number;
  startedAt: number;
  finishedAt: number | null;
}

export class ListAgentTasks extends Request<AgentTaskInfo[]> {}

export class AgentTaskStarted extends Event {
  task: AgentTaskInfo;

  constructor(init: MessageContext & { task?: AgentTaskInfo } = {}) {
    super(init);
    this.task =
      init.task ?? {
        taskId: '',
        agentId: init.agentId ?? '',
        sessionId: init.sessionId ?? '',
        prompt: '',
        status: 'running',
        result: '',
        error: '',
        startedAt: 0,
        finishedAt: null,
      };
  }
}

export class AgentTaskFinished extends Event {
  task: AgentTaskInfo;

  constructor(init: MessageContext & { task?: AgentTaskInfo } = {}) {
    super(init);
    this.task =
      init.task ?? {
        taskId: '',
        agentId: init.agentId ?? '',
        sessionId: init.sessionId ?? '',
        prompt: '',
        status: 'completed',
        result: '',
        error: '',
        startedAt: 0,
        finishedAt: 0,
      };
  }
}

export class CancelAgentTask extends Request<boolean> {
  taskId: string;

  constructor(init: { taskId?: string; sessionId?: string } = {}) {
    super(init);
    this.taskId = init.taskId ?? '';
  }
}

export class AgentSpawned extends Event {
  info: AgentInfo;

  constructor(init: MessageContext & { info?: AgentInfo } = {}) {
    super(init);
    this.info =
      init.info ?? {
        agentId: '',
        parentAgentId: '',
        teamId: '',
        sessionId: '',
        role: 'worker',
        status: 'idle',
      };
  }
}

export class AgentDisposed extends Event {
  override agentId: string;

  constructor(init: MessageContext & { agentId?: string } = {}) {
    super(init);
    this.agentId = init.agentId ?? '';
  }
}

export interface DisposeTeamResult {
  ok: boolean;
  teamId: string;
  disposedAgents: number;
  cancelledTasks: number;
  removedTasks: number;
  message: string;
}

export class DisposeTeam extends Request<DisposeTeamResult> {
  override teamId: string;

  constructor(init: { teamId?: string; sessionId?: string } = {}) {
    super(init);
    this.teamId = init.teamId ?? '';
  }
}
