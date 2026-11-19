/** Skill contracts — TypeScript port of alex/kernel/contracts/skills.py. */

import { Command, Event, Request, type MessageContext } from '../message';
import type { MergeSkillsResult, SkillCard } from '../dto/skill';

export class RetrieveSkills extends Request<SkillCard[]> {
  query: string;
  topK: number;

  constructor(init: { query?: string; topK?: number; sessionId?: string } = {}) {
    super(init);
    this.query = init.query ?? '';
    this.topK = init.topK ?? 3;
  }
}

export class LoadSkill extends Request<SkillCard> {
  skillName: string;

  constructor(init: { skillName?: string; sessionId?: string } = {}) {
    super(init);
    this.skillName = init.skillName ?? '';
  }
}

/** Trigger skill reflection (fire-and-forget; results via SkillsReflected). */
export class ReflectSkills extends Command {}

export class SkillsReflected extends Event {
  /** Number of newly created skills (Python field: `new`). */
  created: number;
  updated: number;
  deprecated: number;
  names: string[];
  updatedNames: string[];

  constructor(
    init: MessageContext & {
      created?: number;
      updated?: number;
      deprecated?: number;
      names?: string[];
      updatedNames?: string[];
    } = {},
  ) {
    super(init);
    this.created = init.created ?? 0;
    this.updated = init.updated ?? 0;
    this.deprecated = init.deprecated ?? 0;
    this.names = init.names ?? [];
    this.updatedNames = init.updatedNames ?? [];
  }

  get toast(): string {
    const parts: string[] = [];
    if (this.created) parts.push(`${this.created} new: ${this.names.join(', ')}`);
    if (this.updated) {
      parts.push(
        this.updatedNames.length
          ? `${this.updated} updated: ${this.updatedNames.join(', ')}`
          : `${this.updated} updated`,
      );
    }
    if (this.deprecated) parts.push(`${this.deprecated} deprecated`);
    return parts.length ? `Skills refined — ${parts.join('; ')}` : 'Skills refined';
  }
}

export class SkillLoaded extends Event {
  skillName: string;
  skillPattern: string;
  streamId: string;

  constructor(
    init: MessageContext & {
      skillName?: string;
      skillPattern?: string;
      streamId?: string;
    } = {},
  ) {
    super(init);
    this.skillName = init.skillName ?? '';
    this.skillPattern = init.skillPattern ?? '';
    this.streamId = init.streamId ?? '';
  }
}

export class SkillReflectError extends Event {
  error: string;

  constructor(init: MessageContext & { error?: string } = {}) {
    super(init);
    this.error = init.error ?? '';
  }
}

export class ListSkills extends Request<Record<string, unknown>[]> {
  includeDeprecated: boolean;

  constructor(init: { includeDeprecated?: boolean; sessionId?: string } = {}) {
    super(init);
    this.includeDeprecated = init.includeDeprecated ?? false;
  }
}

export class DeleteSkill extends Request<string | null> {
  target: string;

  constructor(init: { target?: string; sessionId?: string } = {}) {
    super(init);
    this.target = init.target ?? '';
  }
}

export class DeprecateSkill extends Request<string | null> {
  target: string;

  constructor(init: { target?: string; sessionId?: string } = {}) {
    super(init);
    this.target = init.target ?? '';
  }
}

export class GetSkillName extends Request<string> {
  skillId: string;

  constructor(init: { skillId?: string; sessionId?: string } = {}) {
    super(init);
    this.skillId = init.skillId ?? '';
  }
}

export class RecordSkillUsage extends Request<void> {
  skillId: string;
  positive: boolean;

  constructor(init: { skillId?: string; positive?: boolean; sessionId?: string } = {}) {
    super(init);
    this.skillId = init.skillId ?? '';
    this.positive = init.positive ?? true;
  }
}

export class MergeSkills extends Request<MergeSkillsResult> {}