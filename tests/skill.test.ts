import { afterEach, describe, expect, test } from 'bun:test';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { SkillStore } from '../src/skill/store';
import { SkillModule } from '../src/skill/module';
import { retrieveSkills } from '../src/skill/retriever';
import { SkillReflector, parseReflection } from '../src/skill/reflector';
import { createSkill, recordSkillUse } from '../src/skill/models';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';
import { ExecuteTool, GetToolCatalog } from '../src/kernel/contracts/tools';
import { FeedbackSubmitted } from '../src/kernel/contracts/chat';
import {
  LoadSkill,
  ListSkills,
  RecordSkillUsage,
  ReflectSkills,
  RetrieveSkills,
  SkillsReflected,
  SkillReflectError,
  DeleteSkill,
  DeprecateSkill,
} from '../src/kernel/contracts/skills';
import { userMessage, assistantMessage } from '../src/kernel/dto/message';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(prefix = 'alex-ts-skill-'): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), prefix));
  tempDirs.push(dir);
  return dir;
}

class SequenceModel implements ChatModel {
  readonly calls: ChatRequest[] = [];
  constructor(private readonly payloads: string[]) {}

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls.push(request);
    const payload = this.payloads[Math.min(this.calls.length - 1, this.payloads.length - 1)] ?? '';
    yield { type: 'content', delta: payload };
    yield {
      type: 'done',
      result: { content: payload, thinking: '', toolCalls: [], finishReason: 'stop' },
    };
  }
}

class JsonModel implements ChatModel {
  readonly calls: ChatRequest[] = [];
  constructor(private readonly payload: string) {}

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls.push(request);
    yield { type: 'content', delta: this.payload };
    yield {
      type: 'done',
      result: { content: this.payload, thinking: '', toolCalls: [], finishReason: 'stop' },
    };
  }
}

async function setupModule(options: {
  dir: string;
  model?: ChatModel;
  seed?: ReturnType<typeof createSkill>[];
  autoReflect?: boolean;
  reflectionMinTools?: number;
}): Promise<{ bus: AsyncEventBus; skills: SkillModule; store: SkillStore; tools: ToolsModule }> {
  const store = new SkillStore({ path: join(options.dir, 'skills', 'skills.json') });
  for (const skill of options.seed ?? []) await store.add(skill);
  const bus = new AsyncEventBus();
  const tools = new ToolsModule({
    roots: [options.dir],
    policy: new PermissionPolicy({ allowed: ['read', 'network'], env: {} }),
    audit: new AuditLogger(join(options.dir, 'audit.jsonl')),
    approvalTimeoutMs: 500,
    plugins: false,
  });
  const skills = new SkillModule({
    store,
    model: options.model,
    registerTools: true,
    autoReflect: options.autoReflect,
    reflectionMinTools: options.reflectionMinTools,
  });
  await tools.start(bus);
  await skills.start(bus);
  await bus.start();
  return { bus, skills, store, tools };
}

function seedSkill(name: string, pattern: string, tags: string[]): ReturnType<typeof createSkill> {
  const skill = createSkill({ name, pattern, instruction: `How to ${name}`, tags });
  skill.status = 'ACTIVE';
  return skill;
}

describe('SkillStore', () => {
  test('loads Python-format snake_case records and persists the same shape', async () => {
    const dir = await tempDir();
    const path = join(dir, 'skills', 'skills.json');
    await mkdir(join(dir, 'skills'), { recursive: true });
    await writeFile(
      path,
      JSON.stringify([
        {
          id: 'abc123',
          name: 'debug-import',
          pattern: 'python import error',
          instruction: '1. read traceback\n2. fix imports',
          tags: ['python', 'debug'],
          examples: [],
          status: 'ACTIVE',
          use_count: 4,
          success_count: 3,
          failure_count: 1,
          confidence: 0.7,
          version: 2,
        },
      ]),
      'utf8',
    );
    const store = new SkillStore({ path });
    await store.load();
    const skill = store.get('abc123');
    expect(skill?.useCount).toBe(4);
    expect(skill?.successCount).toBe(3);
    expect(skill?.status).toBe('ACTIVE');

    await store.add(createSkill({ name: 'new-skill', pattern: 'x', instruction: 'y' }));
    const raw = JSON.parse(await readFile(path, 'utf8')) as Array<Record<string, unknown>>;
    const persisted = raw.find((item) => item.name === 'new-skill');
    expect(persisted?.use_count).toBe(0);
    expect(persisted?.success_count).toBe(0);
    expect(persisted).not.toHaveProperty('useCount');
  });

  test('recordSkillUse updates confidence with Bayesian smoothing', () => {
    const skill = createSkill({ name: 's' });
    recordSkillUse(skill, true);
    expect(skill.useCount).toBe(1);
    expect(skill.successCount).toBe(1);
    expect(skill.confidence).toBeGreaterThan(0.5);
    recordSkillUse(skill, false);
    expect(skill.confidence).toBeLessThan(1);
  });
});

describe('SkillRetriever', () => {
  test('scores tags, name and pattern; penalizes candidates', async () => {
    const dir = await tempDir('alex-ts-retriever-');
    const store = new SkillStore({ path: join(dir, 'skills.json') });
    await store.load();
    const active = seedSkill('deploy-service', 'deploy a microservice to production', ['deploy', 'ops']);
    const candidate = createSkill({
      name: 'deploy-helper',
      pattern: 'deploy helper',
      instruction: 'x',
      tags: ['deploy'],
    });
    await store.add(active);
    await store.add(candidate);

    const results = retrieveSkills(store, 'please deploy a microservice', 2);
    expect(results[0]?.name).toBe('deploy-service');
    expect(results).toHaveLength(2);
    // Confidence adds a small baseline score (Python parity), so unrelated
    // queries still return the top-k by confidence rather than an empty list.
    const noMatch = retrieveSkills(store, 'unrelated cooking question', 2);
    expect(noMatch.length).toBeLessThanOrEqual(2);
  });
});

describe('SkillModule over the bus', () => {
  test('retrieve / load / list / usage / delete / deprecate', async () => {
    const dir = await tempDir();
    const seed = seedSkill('debug-import', 'python import error', ['python', 'debug']);
    const { bus } = await setupModule({ dir, seed: [seed] });

    const retrieved = await bus.request(new RetrieveSkills({ query: 'python import error', topK: 3 }));
    expect(retrieved[0]?.name).toBe('debug-import');

    const loaded = await bus.request(new LoadSkill({ skillName: 'debug-import' }));
    expect(loaded.instruction).toContain('How to debug-import');

    const listed = await bus.request(new ListSkills({ includeDeprecated: false }));
    expect(listed[0]?.name).toBe('debug-import');
    expect(listed[0]).toHaveProperty('use_count');

    await bus.request(new RecordSkillUsage({ skillId: seed.id, positive: true }));
    expect((await bus.request(new LoadSkill({ skillName: 'debug-import' }))).useCount).toBe(1);

    const deprecated = await bus.request(new DeprecateSkill({ target: 'debug-import' }));
    expect(deprecated).toBe('debug-import');
    const afterDeprecate = await bus.request(new ListSkills({ includeDeprecated: false }));
    expect(afterDeprecate).toHaveLength(0);

    const deleted = await bus.request(new DeleteSkill({ target: seed.id.slice(0, 4) }));
    expect(deleted).toBe('debug-import');
    await bus.shutdown();
  });

  test('registers load_skill / list_skills tools', async () => {
    const dir = await tempDir();
    const { bus } = await setupModule({ dir, seed: [seedSkill('debug-import', 'python import error', ['python'])] });

    const catalog = await bus.request(new GetToolCatalog());
    expect(catalog.map((spec) => spec.name)).toEqual(
      expect.arrayContaining(['load_skill', 'list_skills']),
    );

    const loaded = await bus.request(
      new ExecuteTool({ name: 'load_skill', args: { skill_name: 'debug-import' }, sessionId: 's' }),
    );
    expect(loaded.output).toContain('[Skill: debug-import]');
    expect(loaded.output).toContain('How to debug-import');

    const listed = await bus.request(
      new ExecuteTool({ name: 'list_skills', args: {}, sessionId: 's' }),
    );
    expect(listed.output).toContain('debug-import');
    await bus.shutdown();
  });

  test('reflection creates and updates skills, then publishes SkillsReflected', async () => {
    const dir = await tempDir();
    const existing = seedSkill('debug-import', 'python import error', ['python']);
    const payload = JSON.stringify({
      new_skills: [
        {
          name: 'write-tests',
          pattern: 'user asks to add tests',
          instruction: '1. inspect module\n2. write pytest',
          tags: ['testing'],
        },
      ],
      updated_skills: [
        { id: existing.id, pattern: 'python import or module error', instruction: 'refined steps' },
      ],
      deprecated_ids: [],
    });
    const { bus, skills, store } = await setupModule({ dir, model: new JsonModel(payload), seed: [existing] });

    const events: SkillsReflected[] = [];
    await bus.subscribe(SkillsReflected, (event) => { events.push(event); });

    bus.publish(new ReflectSkills({ sessionId: 's1' }));
    await bus.idle();

    expect(events).toHaveLength(1);
    expect(events[0]?.created).toBe(1);
    expect(events[0]?.updated).toBe(1);
    expect(events[0]?.names).toContain('write-tests');
    expect(store.findByName('write-tests')?.status).toBe('CANDIDATE');
    expect(store.get(existing.id)?.version).toBe(2);
    expect(store.get(existing.id)?.pattern).toContain('module error');
    await bus.shutdown();
  });

  test('reflection automatically merges after creating a new skill', async () => {
    const dir = await tempDir();
    const keep = seedSkill('keep-one', 'first workflow', ['a']);
    const deprecate = seedSkill('drop-one', 'second workflow', ['b']);
    const reflection = JSON.stringify({
      new_skills: [
        {
          name: 'new-workflow',
          pattern: 'new workflow',
          instruction: 'do the new workflow',
          tags: ['new'],
        },
      ],
      updated_skills: [],
      deprecated_ids: [],
    });
    const merge = JSON.stringify({
      merged_groups: [],
      deprecate_ids: [deprecate.id],
    });
    const model = new SequenceModel([reflection, merge]);
    const { bus, store } = await setupModule({
      dir,
      model,
      seed: [keep, deprecate],
    });
    const events: SkillsReflected[] = [];
    await bus.subscribe(SkillsReflected, (event) => { events.push(event); });

    bus.publish(new ReflectSkills({ sessionId: 's1' }));
    await bus.idle();

    expect(model.calls).toHaveLength(2);
    expect(events[0]?.created).toBe(1);
    expect(events[0]?.merged).toBe(0);
    expect(events[0]?.remaining).toBe(2);
    expect(store.get(deprecate.id)?.status).toBe('DEPRECATED');
    expect(store.get(keep.id)?.status).not.toBe('DEPRECATED');
    await bus.shutdown();
  });

  test('automatic reflection is gated by task complexity, explicit reflection is not', async () => {
    const dir = await tempDir();
    const payload = JSON.stringify({ new_skills: [], updated_skills: [], deprecated_ids: [] });
    const model = new JsonModel(payload);
    const { bus } = await setupModule({ dir, model });

    bus.publish(new ReflectSkills({ sessionId: 'plain-chat', auto: true }));
    await bus.idle();
    expect(model.calls).toHaveLength(0);

    bus.publish(new ReflectSkills({ sessionId: 'plain-chat' }));
    await bus.idle();
    expect(model.calls).toHaveLength(1);
    await bus.shutdown();
  });

  test('respects the auto_reflect setting', async () => {
    const dir = await tempDir();
    const payload = JSON.stringify({ new_skills: [], updated_skills: [], deprecated_ids: [] });
    const model = new JsonModel(payload);
    const { bus } = await setupModule({ dir, model, autoReflect: false });

    bus.publish(new ReflectSkills({ sessionId: 's1', auto: true }));
    await bus.idle();
    expect(model.calls).toHaveLength(0);

    bus.publish(new ReflectSkills({ sessionId: 's1', force: true }));
    await bus.idle();
    expect(model.calls).toHaveLength(1);
    await bus.shutdown();
  });

  test('reflection reports SkillReflectError without a model', async () => {
    const dir = await tempDir();
    const { bus } = await setupModule({ dir });
    const errors: SkillReflectError[] = [];
    await bus.subscribe(SkillReflectError, (event) => { errors.push(event); });

    bus.publish(new ReflectSkills({ sessionId: 's1' }));
    await bus.idle();
    expect(errors[0]?.error).toContain('not configured');
    await bus.shutdown();
  });

  test('negative feedback triggers reflection, positive does not', async () => {
    const dir = await tempDir();
    const payload = JSON.stringify({ new_skills: [], updated_skills: [], deprecated_ids: [] });
    const model = new JsonModel(payload);
    const { bus } = await setupModule({ dir, model });

    bus.publish(new FeedbackSubmitted({ sessionId: 's1', positive: true }));
    await bus.idle();
    expect(model.calls).toHaveLength(0);

    bus.publish(new FeedbackSubmitted({ sessionId: 's1', positive: false }));
    await bus.idle();
    expect(model.calls.length).toBe(1);
    await bus.shutdown();
  });
});

describe('Reflection parsing', () => {
  test('parses fenced JSON and tolerates trailing commas', () => {
    const parsed = parseReflection(
      '```json\n{"new_skills":[{"name":"a","pattern":"p","instruction":"i","tags":["t"],}],"updated_skills":[],"deprecated_ids":["x"],}\n```',
    );
    expect(parsed.newSkills[0]?.name).toBe('a');
    expect(parsed.deprecatedIds).toEqual(['x']);
    expect(parseReflection('not json')).toEqual({ newSkills: [], updatedSkills: [], deprecatedIds: [] });
  });
});