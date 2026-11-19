/** System-prompt section contracts — modules assemble the prompt over the bus. */

import { Command, Event, Request } from '../message';

export interface PromptSection {
  /** Stable section id; re-registering the same id replaces it. */
  id: string;
  /** Optional heading rendered before the content. */
  title?: string;
  content: string;
  /** Lower numbers render first. */
  order?: number;
  /** Diagnostics: module or file that contributed the section. */
  source?: string;
}

export class RegisterPromptSection extends Command {
  section: PromptSection;

  constructor(init: { section?: PromptSection; sessionId?: string } = {}) {
    super(init);
    this.section =
      init.section ?? { id: '', content: '' };
  }
}

export class UnregisterPromptSection extends Command {
  id: string;

  constructor(init: { id?: string; sessionId?: string } = {}) {
    super(init);
    this.id = init.id ?? '';
  }
}

export class GetPromptSections extends Request<PromptSection[]> {}

export interface PromptInfo {
  userPromptPath: string;
  userPrompt: string;
  agentsPath?: string;
  sections: PromptSection[];
  prompt: string;
}

export class GetPromptInfo extends Request<PromptInfo> {}

export class SetUserPrompt extends Request<{ path: string }> {
  content: string;

  constructor(init: { content?: string; sessionId?: string } = {}) {
    super(init);
    this.content = init.content ?? '';
  }
}

export class ReloadPrompts extends Request<PromptInfo> {}

export class SystemPromptUpdated extends Event {
  prompt: string;
  reason: string;

  constructor(init: { prompt?: string; reason?: string; sessionId?: string } = {}) {
    super(init);
    this.prompt = init.prompt ?? '';
    this.reason = init.reason ?? '';
  }
}

/** Ephemeral runtime context injected into the next model request. */
export class RuntimeContextNotice extends Event {
  text: string;
  override source: string;
  key: string;

  constructor(
    init: { text?: string; source?: string; key?: string; sessionId?: string } = {},
  ) {
    super(init);
    this.text = init.text ?? '';
    this.source = init.source ?? '';
    this.key = init.key ?? this.source;
  }
}
