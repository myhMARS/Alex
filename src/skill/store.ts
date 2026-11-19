/**
 * Skill persistence — file-format compatible with alex/skill/repository.py
 * (`~/.alex/skills/skills.json`, array of snake_case records).
 */

import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

import type { Skill, SkillStatus } from './models';
import { createSkill } from './models';
import { promptsDirFor, readSkillTemplate, removeSkillTemplate, saveSkillTemplate } from './templates';

export interface SkillStoreOptions {
  path?: string;
  /** Override the per-skill template directory (default: <skills.json dir>/prompts). */
  promptsDir?: string;
}

interface SkillFileRecord {
  id?: string;
  name?: string;
  pattern?: string;
  instruction?: string;
  tags?: unknown;
  examples?: unknown;
  status?: string;
  use_count?: number;
  success_count?: number;
  failure_count?: number;
  confidence?: number;
  version?: number;
  // camelCase tolerated as well
  useCount?: number;
  successCount?: number;
  failureCount?: number;
}

export function defaultSkillsPath(): string {
  return join(homedir(), '.alex', 'skills', 'skills.json');
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function asStatus(value: unknown): SkillStatus {
  return value === 'ACTIVE' || value === 'DEPRECATED' || value === 'CANDIDATE'
    ? value
    : 'CANDIDATE';
}

function fromRecord(record: SkillFileRecord): Skill {
  return {
    id: record.id || createSkill().id,
    name: String(record.name ?? ''),
    pattern: String(record.pattern ?? ''),
    instruction: String(record.instruction ?? ''),
    tags: asStringArray(record.tags),
    examples: asStringArray(record.examples),
    status: asStatus(record.status),
    useCount: Number(record.use_count ?? record.useCount ?? 0),
    successCount: Number(record.success_count ?? record.successCount ?? 0),
    failureCount: Number(record.failure_count ?? record.failureCount ?? 0),
    confidence: Number(record.confidence ?? 0.5),
    version: Number(record.version ?? 1),
  };
}

function toRecord(skill: Skill): Record<string, unknown> {
  return {
    id: skill.id,
    name: skill.name,
    pattern: skill.pattern,
    instruction: skill.instruction,
    tags: skill.tags,
    examples: skill.examples,
    status: skill.status,
    use_count: skill.useCount,
    success_count: skill.successCount,
    failure_count: skill.failureCount,
    confidence: skill.confidence,
    version: skill.version,
  };
}

export class SkillStore {
  readonly path: string;
  readonly promptsDir: string;
  private readonly skills = new Map<string, Skill>();
  private loaded = false;

  constructor(options: SkillStoreOptions = {}) {
    this.path = options.path ?? defaultSkillsPath();
    this.promptsDir = options.promptsDir ?? promptsDirFor(this.path);
  }

  /** Load from disk; corrupt data is discarded rather than throwing. */
  async load(): Promise<void> {
    this.loaded = true;
    let raw: string;
    try {
      raw = await readFile(this.path, 'utf8');
    } catch (error) {
      if (isNotFound(error)) return;
      return;
    }
    if (!raw.trim()) return;
    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch {
      return;
    }
    if (!Array.isArray(data)) return;
    for (const item of data) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      try {
        const skill = fromRecord(item as SkillFileRecord);
        if (skill.name) this.skills.set(skill.id, skill);
      } catch {
        // skip corrupt entry
      }
    }
  }

  async save(): Promise<void> {
    await mkdir(dirname(this.path), { recursive: true });
    const payload = JSON.stringify([...this.skills.values()].map(toRecord), null, 2);
    const tmp = `${this.path}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
    try {
      await writeFile(tmp, payload, 'utf8');
      await rename(tmp, this.path);
    } catch (error) {
      await unlink(tmp).catch(() => undefined);
      throw error;
    }
  }

  private async ensureLoaded(): Promise<void> {
    if (!this.loaded) await this.load();
  }

  async add(skill: Skill): Promise<void> {
    await this.ensureLoaded();
    this.skills.set(skill.id, skill);
    await this.save();
    await saveSkillTemplate(this.promptsDir, skill);
  }

  get(id: string): Skill | undefined {
    return this.skills.get(id);
  }

  async update(skill: Skill): Promise<void> {
    await this.ensureLoaded();
    if (!this.skills.has(skill.id)) return;
    this.skills.set(skill.id, skill);
    await this.save();
    await saveSkillTemplate(this.promptsDir, skill);
  }

  async remove(id: string): Promise<void> {
    await this.ensureLoaded();
    if (!this.skills.delete(id)) return;
    await this.save();
    await removeSkillTemplate(this.promptsDir, id);
  }

  async deprecate(id: string): Promise<void> {
    await this.ensureLoaded();
    const skill = this.skills.get(id);
    if (!skill) return;
    skill.status = 'DEPRECATED';
    await this.save();
    await removeSkillTemplate(this.promptsDir, id);
  }

  /** Read the per-skill prompt template, if one exists. */
  async templateFor(skillId: string): Promise<string | null> {
    return readSkillTemplate(this.promptsDir, skillId);
  }

  listAll(): Skill[] {
    return [...this.skills.values()];
  }

  listActive(): Skill[] {
    return this.listAll().filter((skill) => skill.status === 'ACTIVE');
  }

  findByName(name: string, includeDeprecated = false): Skill | undefined {
    const target = name.trim().toLowerCase();
    return this.listAll().find(
      (skill) =>
        skill.name.toLowerCase() === target &&
        (includeDeprecated || skill.status !== 'DEPRECATED'),
    );
  }

  findByIdPrefix(prefix: string): Skill | undefined {
    return this.listAll().find((skill) => skill.id.startsWith(prefix));
  }
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'code' in error &&
    (error as { code?: string }).code === 'ENOENT'
  );
}