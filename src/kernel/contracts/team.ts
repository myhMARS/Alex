/** Agent-team event contracts — routing and lifecycle primitives. */

import { Command, Event, type MessageContext } from '../message';

export interface TeamMemberInfo {
  agentId: string;
  role: string;
  sessionId?: string;
  parentAgentId?: string;
}

/** Point-to-point or team-broadcast message between agents. */
export class AgentTeamMessage extends Event {
  messageId: string;
  fromAgentId: string;
  toAgentId: string;
  content: string;
  replyTo?: string;
  /** Session that sent the message. */
  fromSessionId?: string;

  constructor(
    init: MessageContext & {
      messageId?: string;
      fromAgentId?: string;
      toAgentId?: string;
      content?: string;
      replyTo?: string;
      fromSessionId?: string;
    } = {},
  ) {
    super({
      ...init,
      agentId: init.fromAgentId ?? init.agentId,
      targetAgentId: init.toAgentId ?? init.targetAgentId,
    });
    this.messageId = init.messageId ?? this.eventId;
    this.fromAgentId = init.fromAgentId ?? this.agentId;
    this.toAgentId = init.toAgentId ?? '';
    this.content = init.content ?? '';
    this.replyTo = init.replyTo;
    this.fromSessionId = init.fromSessionId;
  }
}

export class AgentTeamMessageConsumed extends Event {
  messageId: string;
  ok: boolean;
  error?: string;

  constructor(
    init: MessageContext & { messageId?: string; ok?: boolean; error?: string } = {},
  ) {
    super(init);
    this.messageId = init.messageId ?? '';
    this.ok = init.ok ?? true;
    this.error = init.error;
  }
}

export class AgentTeamMemberJoined extends Event {
  member: TeamMemberInfo;

  constructor(
    init: MessageContext & { member?: TeamMemberInfo; teamId?: string } = {},
  ) {
    super(init);
    this.member =
      init.member ?? { agentId: init.agentId ?? '', role: 'worker' };
  }
}

export class AgentTeamMemberLeft extends Event {
  override agentId: string;

  constructor(init: MessageContext & { agentId?: string } = {}) {
    super(init);
    this.agentId = init.agentId ?? '';
  }
}

/** Assign a task to a team member; the target replies with AgentTeamMessage. */
export class AgentTeamTaskRequested extends Command {
  taskId: string;
  assigneeAgentId: string;
  prompt: string;

  constructor(
    init: MessageContext & {
      taskId?: string;
      assigneeAgentId?: string;
      prompt?: string;
    } = {},
  ) {
    super({
      ...init,
      agentId: init.agentId,
      targetAgentId: init.assigneeAgentId ?? init.targetAgentId,
    });
    this.taskId = init.taskId ?? this.eventId;
    this.assigneeAgentId = init.assigneeAgentId ?? '';
    this.prompt = init.prompt ?? '';
  }
}

export class AgentTeamTaskCompleted extends Event {
  taskId: string;
  override agentId: string;
  result: string;

  constructor(
    init: MessageContext & { taskId?: string; agentId?: string; result?: string } = {},
  ) {
    super(init);
    this.taskId = init.taskId ?? '';
    this.agentId = init.agentId ?? '';
    this.result = init.result ?? '';
  }
}