import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { SessionModule } from '../src/session/module';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { SkillStore } from '../src/skill/store';
import { SkillModule } from '../src/skill/module';
import { createSkill } from '../src/skill/models';
import type { ChatModel, ChatRequest, ChatStreamEvent, ToolCall } from '../src/llm/client';
import { SkillLoaded, SkillsReflected } from '../src/kernel/contracts/skills';
import { TurnCompleted, UserTurnRequested } from '../src/kernel/contracts/chat';
import { GetSessionProjection } from '../src/kernel/contracts/session';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

function toolCall(name: string, args: string): ToolCall {
  return { id: 'call_1', type: 'function', function: { name, arguments: args } };
}

class LoadSkillModel implements ChatModel {
  readonly calls: ChatRequest[] = [];

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls.push({ ...request, messages: [...request.messages] });
    if (this.calls.length === 1) {
      yield {
        type: 'done',
        result: {
          content: '',
          thinking: '',
          toolCalls: [toolCall('load_skill', '{"skill_name":"debug-import"}')],
          finishReason: 'tool_calls',
        },
      };
      return;
    }
    yield { type: 'content', delta: 'fixed' };
    yield {
      type: 'done',
      result: { content: 'fixed', thinking: '', toolCalls: [], finishReason: 'stop' },
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

class PlainModel implements ChatModel {
  async *streamChat(): AsyncIterable<ChatStreamEvent> {
    yield { type: 'content', delta: 'plain answer' };
    yield {
      type: 'done',
      result: { content: 'plain answer', thinking: '', toolCalls: [], finishReason: 'stop' },
    };
  }
}

async function setup(options: { seed: boolean; agentModel: ChatModel; reflector?: ChatModel }) {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-agent-skill-'));
  tempDirs.push(dir);
  const bus = new AsyncEventBus();
  const session = new SessionModule();
  const tools = new ToolsModule({
    roots: [dir],
    policy: new PermissionPolicy({ allowed: ['read', 'network'], env: {} }),
    audit: new AuditLogger(join(dir, 'audit.jsonl')),
    approvalTimeoutMs: 500,
    plugins: false,
  });
  const store = new SkillStore({ path: join(dir, 'skills', 'skills.json') });
  if (options.seed) {
    const skill = createSkill({
      name: 'debug-import',
      pattern: 'python import error',
      instruction: '1. read traceback\n2. fix imports',
      tags: ['python', 'debug'],
    });
    skill.status = 'ACTIVE';
    await store.add(skill);
  }
  const skills = new SkillModule({ store, model: options.reflector });
  const agent = new AgentModule({ model: options.agentModel, systemPrompt: 'SYS' });
  await session.start(bus);
  await tools.start(bus);
  await skills.start(bus);
  await agent.start(bus);
  await bus.start();
  return { bus, agent, skills, store, dir };
}

describe('Agent + Skill integration', () => {
  test('injects retrieved skills into the prompt and emits SkillLoaded', async () => {
    const model = new LoadSkillModel();
    const { bus, agent } = await setup({ seed: true, agentModel: model });

    const loaded: SkillLoaded[] = [];
    const reflected: SkillsReflected[] = [];
    const completed: TurnCompleted[] = [];
    await bus.subscribe(SkillLoaded, (event) => { loaded.push(event); });
    await bus.subscribe(SkillsReflected, (event) => { reflected.push(event); });
    await bus.subscribe(TurnCompleted, (event) => { completed.push(event); });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'python import error again' }));
    await agent.whenIdle();
    await bus.idle();

    const first = model.calls[0]!;
    expect(first.systemPrompt).toContain('SYS');
    expect(first.systemPrompt).toContain('Available Skills');
    expect(first.systemPrompt).toContain('debug-import');

    expect(loaded).toHaveLength(1);
    expect(loaded[0]?.skillName).toBe('debug-import');
    expect(completed[0]?.content).toBe('fixed');

    const toolMessage = model.calls[1]!.messages.find((message) => message.role === 'tool');
    expect(String(toolMessage?.content)).toContain('[Skill: debug-import]');
    expect(String(toolMessage?.content)).toContain('fix imports');

    // A skill was loaded, so auto-reflection must not run.
    expect(reflected).toHaveLength(0);
    const history = (
      await bus.request(new GetSessionProjection({ sessionId: 's1' }))
    ).messages;
    expect(history.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'tool',
      'assistant',
    ]);

    await bus.shutdown();
  }, 20_000);

  test('skips auto-reflection for a simple chat turn', async () => {
    const reflector = new JsonModel(
      JSON.stringify({
        new_skills: [
          { name: 'answer-plainly', pattern: 'plain questions', instruction: 'be brief', tags: ['qa'] },
        ],
        updated_skills: [],
        deprecated_ids: [],
      }),
    );
    const { bus, agent, store } = await setup({
      seed: false,
      agentModel: new PlainModel(),
      reflector,
    });

    const reflected: SkillsReflected[] = [];
    await bus.subscribe(SkillsReflected, (event) => { reflected.push(event); });

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'hello' }));
    await agent.whenIdle();
    await bus.idle();

    expect(reflected).toHaveLength(0);
    expect(store.findByName('answer-plainly')).toBeUndefined();
    await bus.shutdown();
  }, 20_000);
});