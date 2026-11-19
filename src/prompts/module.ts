/**
 * PromptModule — owns the system-prompt section registry.
 *
 * Base prompt, user-edited prompt, AGENTS.md, and module-contributed sections
 * are assembled here. Tools/MCP/skills publish RegisterPromptSection /
 * UnregisterPromptSection events instead of editing the Agent directly.
 */

import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import { GetSystemPrompt } from '../kernel/contracts/chat';
import {
  GetPromptInfo,
  GetPromptSections,
  RegisterPromptSection,
  ReloadPrompts,
  SetUserPrompt,
  SystemPromptUpdated,
  UnregisterPromptSection,
  type PromptInfo,
  type PromptSection,
} from '../kernel/contracts/prompt';

export interface PromptModuleOptions {
  /** User-editable base prompt file (default `~/.alex/system-prompt.md`). */
  userPromptPath?: string;
  /** Explicit AGENTS.md path; when omitted the nearest one is discovered. */
  agentsPath?: string;
  /** Starting directory for AGENTS.md discovery (default cwd). */
  cwd?: string;
}

export function defaultUserPromptPath(): string {
  return process.env.ALEX_SYSTEM_PROMPT_PATH || join(homedir(), '.alex', 'system-prompt.md');
}

export async function findAgentsFile(start = process.cwd()): Promise<string | undefined> {
  let dir = resolve(start);
  for (;;) {
    const candidate = join(dir, 'AGENTS.md');
    try {
      await access(candidate);
      return candidate;
    } catch {
      // keep walking up
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  const global = join(homedir(), '.alex', 'AGENTS.md');
  try {
    await access(global);
    return global;
  } catch {
    return undefined;
  }
}

export class PromptModule implements Module {
  readonly name = 'prompt';
  readonly dependencies: readonly string[] = [];

  private readonly userPromptPath: string;
  private readonly explicitAgentsPath?: string;
  private readonly cwd: string;
  private readonly sections = new Map<string, PromptSection>();
  private baseContent = '';
  private agentsPath?: string;
  private agentsContent = '';
  private prompt = '';
  private bus: AsyncEventBus | null = null;

  constructor(options: PromptModuleOptions = {}) {
    this.userPromptPath = options.userPromptPath ?? defaultUserPromptPath();
    this.explicitAgentsPath = options.agentsPath;
    this.cwd = options.cwd ?? process.cwd();
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    await this.reloadFiles();
    bus.provide(GetSystemPrompt, () => this.prompt);
    bus.provide(GetPromptSections, () => this.listSections());
    bus.provide(GetPromptInfo, () => this.info());
    bus.provide(SetUserPrompt, async (req) => {
      await this.writeUserPrompt(req.content);
      this.baseContent = req.content;
      this.rebuild('user-prompt');
      return { path: this.userPromptPath };
    });
    bus.provide(ReloadPrompts, async () => {
      await this.reloadFiles();
      this.rebuild('reload');
      return this.info();
    });
    await bus.subscribe(RegisterPromptSection, (event) => {
      if (!event.section?.id || !event.section.content.trim()) return;
      this.sections.set(event.section.id, {
        ...event.section,
        order: event.section.order ?? 100,
      });
      this.rebuild('register');
    });
    await bus.subscribe(UnregisterPromptSection, (event) => {
      if (!event.id) return;
      if (this.sections.delete(event.id)) this.rebuild('unregister');
    });
    this.rebuild('startup');
  }

  private async reloadFiles(): Promise<void> {
    this.baseContent = await readOptional(this.userPromptPath);
    this.agentsPath = this.explicitAgentsPath ?? (await findAgentsFile(this.cwd));
    this.agentsContent = this.agentsPath ? await readOptional(this.agentsPath) : '';
    if (this.explicitAgentsPath && !this.agentsContent.trim()) this.agentsPath = undefined;
  }

  private async writeUserPrompt(content: string): Promise<void> {
    await mkdir(dirname(this.userPromptPath), { recursive: true });
    await writeFile(this.userPromptPath, content, 'utf8');
  }

  private listSections(): PromptSection[] {
    const all: PromptSection[] = [];
    if (this.baseContent.trim()) {
      all.push({
        id: 'base',
        content: this.baseContent,
        order: 0,
        source: this.userPromptPath,
      });
    }
    if (this.agentsPath && this.agentsContent.trim()) {
      all.push({
        id: 'agents',
        title: 'AGENTS.md',
        content: this.agentsContent,
        order: 10,
        source: this.agentsPath,
      });
    }
    all.push(...this.sections.values());
    return all.sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
  }

  private rebuild(reason: string): void {
    const sections = this.listSections();
    this.prompt = sections
      .map((section) => {
        const content = section.content.trim();
        if (!content) return '';
        if (!section.title || content.startsWith('#')) return content;
        return `# ${section.title}\n\n${content}`;
      })
      .filter(Boolean)
      .join('\n\n');
    this.bus?.publish(new SystemPromptUpdated({ prompt: this.prompt, reason }));
  }

  private info(): PromptInfo {
    return {
      userPromptPath: this.userPromptPath,
      userPrompt: this.baseContent,
      agentsPath: this.agentsPath,
      sections: this.listSections(),
      prompt: this.prompt,
    };
  }
}

async function readOptional(path: string): Promise<string> {
  try {
    return await readFile(path, 'utf8');
  } catch {
    return '';
  }
}