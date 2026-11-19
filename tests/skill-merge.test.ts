import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { SkillStore } from '../src/skill/store';
import { SkillModule } from '../src/skill/module';
import { createSkill } from '../src/skill/models';
import { parseMergeResponse } from '../src/skill/merge';
import { MergeSkills } from '../src/kernel/contracts/skills';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

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

interface SeededStore {
  store: SkillStore;
  keep: ReturnType<typeof createSkill>;
  duplicate: ReturnType<typeof createSkill>;
  obsolete: ReturnType<typeof createSkill>;
}

async function seededStore(): Promise<SeededStore> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-merge-'));
  tempDirs.push(dir);
  const store = new SkillStore({ path: join(dir, 'skills.json') });
  const keep = createSkill({
    name: 'greet',
    pattern: 'greeting responses',
    instruction: 'say hi',
    tags: ['chat'],
  });
  keep.status = 'ACTIVE';
  const duplicate = createSkill({
    name: 'greeting',
    pattern: 'greeting responses',
    instruction: 'say hello',
    tags: ['chat', 'social'],
  });
  duplicate.status = 'ACTIVE';
  const obsolete = createSkill({ name: 'ignore-me', pattern: 'x', instruction: 'y' });
  obsolete.status = 'ACTIVE';
  for (const skill of [keep, duplicate, obsolete]) await store.add(skill);
  return { store, keep, duplicate, obsolete };
}

async function startModule(store: SkillStore, model?: ChatModel) {
  const bus = new AsyncEventBus();
  const skills = new SkillModule({ store, model, registerTools: false });
  await skills.start(bus);
  await bus.start();
  return { bus, skills };
}

describe('skill consolidation', () => {
  test('applies merged groups, updates the keeper and removes duplicates', async () => {
    const { store, keep, duplicate, obsolete } = await seededStore();
    const payload = JSON.stringify({
      merged_groups: [
        {
          keep_id: keep.id,
          merge_ids: [duplicate.id],
          updated_name: 'greet-user',
          updated_pattern: 'greeting or small talk',
          updated_instruction: '1. detect language\n2. greet briefly',
          updated_tags: ['social', 'polite'],
        },
      ],
      deprecate_ids: [obsolete.id],
    });
    const model = new JsonModel(payload);
    const { bus } = await startModule(store, model);

    const result = await bus.request(new MergeSkills());
    expect(result.merged).toBe(1);
    expect(result.deprecated).toBe(1);
    expect(store.get(duplicate.id)).toBeUndefined();
    const keeper = store.get(keep.id);
    expect(keeper?.name).toBe('greet-user');
    expect(keeper?.version).toBe(2);
    expect(keeper?.tags).toEqual(expect.arrayContaining(['chat', 'social', 'polite']));
    expect(store.get(obsolete.id)?.status).toBe('DEPRECATED');
    expect(model.calls).toHaveLength(1);
    await bus.shutdown();
  });

  test('returns an error for unparseable model output', async () => {
    const { store } = await seededStore();
    const model = new JsonModel('I cannot merge these.');
    const { bus } = await startModule(store, model);

    const result = await bus.request(new MergeSkills());
    expect(result.error).toContain('parse');
    expect(result.merged).toBe(0);
    await bus.shutdown();
  });

  test('falls back to deterministic dedupe without a model', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-merge-fallback-'));
    tempDirs.push(dir);
    const store = new SkillStore({ path: join(dir, 'skills.json') });
    const first = createSkill({ name: 'same-skill', pattern: 'p', instruction: 'i' });
    first.status = 'ACTIVE';
    const second = createSkill({ name: 'same-skill', pattern: 'p2', instruction: 'i2' });
    second.status = 'CANDIDATE';
    await store.add(first);
    await store.add(second);
    const { bus } = await startModule(store);

    const result = await bus.request(new MergeSkills());
    expect(result.merged).toBe(1);
    expect(store.listAll().filter((skill) => skill.status !== 'DEPRECATED')).toHaveLength(1);
    await bus.shutdown();
  });
});

describe('merge response parsing', () => {
  test('parses fenced JSON and trailing commas', () => {
    const parsed = parseMergeResponse(
      '```json\n{"merged_groups":[{"keep_id":"a","merge_ids":["b"],"updated_tags":["x"],}],"deprecate_ids":[],}\n```',
    );
    expect(parsed?.groups[0]?.keepId).toBe('a');
    expect(parsed?.groups[0]?.updatedTags).toEqual(['x']);
  });

  test('returns null for invalid output', () => {
    expect(parseMergeResponse('')).toBeNull();
    expect(parseMergeResponse('nothing useful')).toBeNull();
    expect(parseMergeResponse('{"foo": 1}')).toBeNull();
  });
});