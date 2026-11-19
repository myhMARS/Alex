/**
 * SkillModule — retrieval, loading, reflection and skill tools.
 *
 * Mirrors alex/skill/module.py: provides the skill request handlers, reacts to
 * `ReflectSkills` / negative `FeedbackSubmitted`, and registers `load_skill` /
 * `list_skills` into the tool gateway.
 */

import { join } from 'node:path';
import { homedir } from 'node:os';

import type { Module } from '../kernel/runtime';
import type { AsyncEventBus } from '../kernel/bus';
import type { SkillCard } from '../kernel/dto/skill';
import {
  FeedbackSubmitted,
  TurnCompleted,
  TurnFailed,
  UserTurnRequested,
} from '../kernel/contracts/chat';
import {
  DeleteSkill,
  DeprecateSkill,
  GetSkillName,
  ListSkills,
  LoadSkill,
  MergeSkills,
  RecordSkillUsage,
  ReflectSkills,
  RetrieveSkills,
  SkillReflectError,
  SkillsReflected,
} from '../kernel/contracts/skills';
import { GetSessionProjection } from '../kernel/contracts/session';
import type { MessageDTO } from '../kernel/dto/message';
import { RegisterTool } from '../kernel/contracts/tools';
import {
  RegisterPromptSection,
  UnregisterPromptSection,
} from '../kernel/contracts/prompt';
import { skillsSectionPrompt } from '../prompts/skills-section';
import type { MergeSkillsResult } from '../kernel/dto/skill';
import type { ChatModel } from '../llm/client';
import { SkillStore } from './store';
import { retrieveSkills } from './retriever';
import { SkillReflector, type ReflectionResult } from './reflector';
import { requestMerge } from './merge';
import { evolveSkills } from './evolution';
import {
  createSkill,
  recordSkillUse,
  renderSkillPrompt,
  skillToCard,
  type Skill,
} from './models';

export interface SkillModuleOptions {
  store?: SkillStore;
  skillsPath?: string;
  model?: ChatModel;
  reflector?: SkillReflector;
  registerTools?: boolean;
}

export class SkillModule implements Module {
  readonly name = 'skill';
  readonly dependencies: readonly string[] = ['tools'];

  readonly store: SkillStore;
  private readonly reflector: SkillReflector | null;
  private readonly model: ChatModel | null;
  private readonly registerTools: boolean;
  private bus: AsyncEventBus | null = null;

  constructor(options: SkillModuleOptions = {}) {
    this.store =
      options.store ??
      new SkillStore({
        path: options.skillsPath ?? join(homedir(), '.alex', 'skills', 'skills.json'),
      });
    this.reflector =
      options.reflector ?? (options.model ? new SkillReflector(options.model) : null);
    this.model = options.model ?? null;
    this.registerTools = options.registerTools !== false;
  }

  async start(bus: AsyncEventBus): Promise<void> {
    this.bus = bus;
    await this.store.load();

    bus.provide(RetrieveSkills, (req) =>
      retrieveSkills(this.store, req.query, req.topK).map(skillToCard),
    );
    bus.provide(LoadSkill, (req) => {
      const skill = this.store.findByName(req.skillName);
      if (!skill) throw new Error(`Skill not found: ${req.skillName}`);
      return skillToCard(skill);
    });
    bus.provide(ListSkills, (req) => this.listSkills(req.includeDeprecated));
    bus.provide(GetSkillName, (req) => this.store.get(req.skillId)?.name ?? req.skillId);
    bus.provide(DeleteSkill, (req) => this.deleteByTarget(req.target));
    bus.provide(DeprecateSkill, (req) => this.deprecateByTarget(req.target));
    bus.provide(RecordSkillUsage, async (req) => {
      const skill = this.store.get(req.skillId);
      if (!skill) return;
      recordSkillUse(skill, req.positive);
      await this.store.update(skill);
    });
    bus.provide(MergeSkills, () => this.mergeSkills());

    await bus.subscribe(UserTurnRequested, async (cmd) => {
      const skills = retrieveSkills(this.store, cmd.userText, 3).map(skillToCard);
      const id = `skills:${cmd.sessionId}`;
      if (skills.length === 0) {
        bus.publish(new UnregisterPromptSection({ id }));
        return;
      }
      bus.publish(
        new RegisterPromptSection({
          section: {
            id,
            content: skillsSectionPrompt(skills),
            order: 30,
            source: 'skill',
          },
        }),
      );
    });
    await bus.subscribe(TurnCompleted, (cmd) => {
      bus.publish(new UnregisterPromptSection({ id: `skills:${cmd.sessionId}` }));
    });
    await bus.subscribe(TurnFailed, (cmd) => {
      bus.publish(new UnregisterPromptSection({ id: `skills:${cmd.sessionId}` }));
    });
    await bus.subscribe(ReflectSkills, (cmd) => this.reflect(cmd.sessionId));
    await bus.subscribe(FeedbackSubmitted, (cmd) => {
      if (!cmd.positive) return this.reflect(cmd.sessionId);
      return undefined;
    });

    if (this.registerTools) await this.registerSkillTools();
  }

  async stop(): Promise<void> {
    this.bus = null;
  }

  get skillReflector(): SkillReflector | null {
    return this.reflector;
  }

  // ── tools exposed to the LLM ───────────────────────────────────────────

  loadSkillTool(skillName: string): string {
    const skill = this.store.findByName(skillName);
    if (!skill) return `Skill '${skillName}' not found.`;
    return renderSkillPrompt(skill);
  }

  listSkillsTool(): string {
    const active = this.store.listAll().filter((skill) => skill.status !== 'DEPRECATED');
    if (active.length === 0) return 'No skills available.';
    const lines = [`Total: ${active.length} skills\n`];
    for (const skill of active) {
      lines.push(`- ${skill.name} [${skill.status}] pattern: ${skill.pattern}`);
    }
    return lines.join('\n');
  }

  // ── request handlers ───────────────────────────────────────────────────

  private listSkills(includeDeprecated: boolean): Record<string, unknown>[] {
    return this.store
      .listAll()
      .filter((skill) => includeDeprecated || skill.status !== 'DEPRECATED')
      .map((skill) => ({
        id: skill.id,
        name: skill.name,
        status: skill.status,
        use_count: skill.useCount,
        success_count: skill.successCount,
        failure_count: skill.failureCount,
        pattern: skill.pattern,
        instruction: skill.instruction,
        tags: [...skill.tags],
      }));
  }

  private async deleteByTarget(target: string): Promise<string | null> {
    const skill = this.findByTarget(target);
    if (!skill) return null;
    await this.store.remove(skill.id);
    return skill.name;
  }

  private async deprecateByTarget(target: string): Promise<string | null> {
    const skill = this.findByTarget(target);
    if (!skill) return null;
    await this.store.deprecate(skill.id);
    return skill.name;
  }

  private findByTarget(target: string): Skill | undefined {
    const trimmed = target.trim();
    if (!trimmed) return undefined;
    return (
      this.store.findByIdPrefix(trimmed) ??
      this.store.findByName(trimmed, true) ??
      this.store
        .listAll()
        .find((skill) => skill.name.toLowerCase() === trimmed.toLowerCase())
    );
  }

  /** LLM consolidation when a model is available, deterministic dedupe otherwise. */
  private async mergeSkills(): Promise<MergeSkillsResult> {
    const active = this.store.listAll().filter((skill) => skill.status !== 'DEPRECATED');
    if (active.length < 2) {
      return { merged: 0, deprecated: 0, remaining: active.length };
    }
    if (!this.model) return this.mergeSkillsDeterministic(active);

    try {
      const parsed = await requestMerge(this.model, active);
      if (!parsed) {
        return {
          merged: 0,
          deprecated: 0,
          remaining: active.length,
          error: 'Failed to parse LLM response',
        };
      }
      let merged = 0;
      let deprecated = 0;
      for (const group of parsed.groups) {
        const keeper = this.store.get(group.keepId);
        if (!keeper) continue;
        if (group.updatedName) keeper.name = group.updatedName;
        if (group.updatedPattern) keeper.pattern = group.updatedPattern;
        if (group.updatedInstruction) keeper.instruction = group.updatedInstruction;
        if (group.updatedTags) {
          keeper.tags = [...new Set([...keeper.tags, ...group.updatedTags])];
        }
        keeper.version += 1;
        await this.store.update(keeper);
        for (const mergeId of group.mergeIds) {
          if (mergeId === keeper.id) continue;
          if (!this.store.get(mergeId)) continue;
          await this.store.remove(mergeId);
          merged += 1;
        }
      }
      for (const id of parsed.deprecateIds) {
        if (!this.store.get(id)) continue;
        await this.store.deprecate(id);
        deprecated += 1;
      }
      const remaining = this.store
        .listAll()
        .filter((skill) => skill.status !== 'DEPRECATED').length;
      return { merged, deprecated, remaining };
    } catch (error) {
      return {
        merged: 0,
        deprecated: 0,
        remaining: active.length,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  /** Deterministic fallback: collapse same-name skills, keep highest confidence. */
  private async mergeSkillsDeterministic(active: Skill[]): Promise<MergeSkillsResult> {
    const groups = new Map<string, Skill[]>();
    for (const skill of active) {
      const key = skill.name.trim().toLowerCase();
      const group = groups.get(key) ?? [];
      group.push(skill);
      groups.set(key, group);
    }
    let merged = 0;
    const deprecated = 0;
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      group.sort((a, b) => b.confidence - a.confidence);
      const keep = group[0]!;
      for (const duplicate of group.slice(1)) {
        await this.store.remove(duplicate.id);
        merged += 1;
      }
      if (keep.status === 'CANDIDATE') {
        keep.status = 'ACTIVE';
        keep.version += 1;
        await this.store.update(keep);
      }
    }
    const remaining = this.store
      .listAll()
      .filter((skill) => skill.status !== 'DEPRECATED').length;
    return { merged, deprecated, remaining };
  }

  // ── reflection ─────────────────────────────────────────────────────────

  async reflect(sessionId: string): Promise<void> {
    const bus = this.bus;
    if (!bus) return;
    let messages: MessageDTO[] = [];
    if (sessionId) {
      try {
        messages = (await bus.request(new GetSessionProjection({ sessionId }))).messages;
      } catch {
        messages = [];
      }
    }
    if (!this.reflector) {
      bus.publish(new SkillReflectError({ sessionId, error: 'skill reflector is not configured' }));
      return;
    }
    try {
      const result = await this.reflector.reflect({
        messages,
        existing: this.store.listAll().filter((skill) => skill.status !== 'DEPRECATED'),
      });
      const applied = await this.applyReflection(result);
      bus.publish(
        new SkillsReflected({
          sessionId,
          created: applied.created,
          updated: applied.updated,
          deprecated: applied.deprecated,
          names: applied.names,
          updatedNames: applied.updatedNames,
        }),
      );
    } catch (error) {
      bus.publish(
        new SkillReflectError({
          sessionId,
          error: error instanceof Error ? error.message : String(error),
        }),
      );
    }
  }

  private async applyReflection(
    result: ReflectionResult,
  ): Promise<{ created: number; updated: number; deprecated: number; names: string[]; updatedNames: string[] }> {
    const names: string[] = [];
    const updatedNames: string[] = [];
    let created = 0;
    let updated = 0;
    let deprecated = 0;

    for (const draft of result.newSkills) {
      if (this.store.findByName(draft.name, true)) continue;
      const skill = createSkill(draft);
      await this.store.add(skill);
      names.push(skill.name);
      created += 1;
    }

    for (const update of result.updatedSkills) {
      const existing = this.store.get(update.id);
      if (!existing) continue;
      if (update.name !== undefined) existing.name = update.name;
      if (update.pattern !== undefined) existing.pattern = update.pattern;
      if (update.instruction !== undefined) existing.instruction = update.instruction;
      if (update.tags !== undefined) existing.tags = [...update.tags];
      existing.version += 1;
      await this.store.update(existing);
      updatedNames.push(existing.name);
      updated += 1;
    }

    for (const id of result.deprecatedIds) {
      if (!this.store.get(id)) continue;
      await this.store.deprecate(id);
      deprecated += 1;
    }

    await evolveSkills(this.store);
    return { created, updated, deprecated, names, updatedNames };
  }

  // ── skill tools registration ───────────────────────────────────────────

  private async registerSkillTools(): Promise<void> {
    const bus = this.bus;
    if (!bus) return;
    try {
      await bus.request(
        new RegisterTool({
          name: 'load_skill',
          description:
            'Load the full execution methodology of a learned skill by name. ' +
            'Only call when a skill is genuinely relevant.',
          jsonSchema: {
            type: 'object',
            properties: {
              skill_name: { type: 'string', description: 'Skill name to load' },
            },
            required: ['skill_name'],
          },
          callable: async (args: Record<string, unknown>) =>
            this.loadSkillTool(String(args.skill_name ?? '')),
          metadata: { requiredPermission: 'read' },
        }),
      );
      await bus.request(
        new RegisterTool({
          name: 'list_skills',
          description: 'List all learned skills with their trigger patterns.',
          jsonSchema: { type: 'object', properties: {} },
          callable: async () => this.listSkillsTool(),
          metadata: { requiredPermission: 'read' },
        }),
      );
    } catch {
      // ToolsModule may not be started (unit tests) — skip silently.
    }
  }
}

export type { SkillCard };