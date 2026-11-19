import { afterEach, describe, expect, test } from 'bun:test';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { PromptModule } from '../src/prompts/module';
import { SessionModule } from '../src/session/module';
import { AgentModule } from '../src/modules/agent/module';
import { ToolsModule } from '../src/tools/module';
import { SkillModule } from '../src/skill/module';
import { SkillStore } from '../src/skill/store';
import { createSkill } from '../src/skill/models';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { GetSystemPrompt, TokenUsageReported, TurnCompleted, UserTurnRequested } from '../src/kernel/contracts/chat';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';
import {
  GetPromptInfo,
  GetPromptSections,
  RegisterPromptSection,
  ReloadPrompts,
  SetUserPrompt,
  UnregisterPromptSection,
} from '../src/kernel/contracts/prompt';

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-prompt-'));
  tempDirs.push(dir);
  return dir;
}

describe('PromptModule', () => {
  test('assembles base prompt, user prompt, AGENTS.md and module sections', async () => {
    const dir = await tempDir();
    const userPath = join(dir, 'system-prompt.md');
    const agentsPath = join(dir, 'AGENTS.md');
    await writeFile(agentsPath, 'Always use tabs for code.', 'utf8');
    const bus = new AsyncEventBus();
    const prompt = new PromptModule({
      userPromptPath: userPath,
      agentsPath,
      cwd: dir,
    });
    await prompt.start(bus);
    await bus.start();

    const base = await bus.request(new GetSystemPrompt());
    expect(base).not.toContain('You are Alex');
    expect(base).toContain('AGENTS.md');
    expect(base).toContain('Always use tabs for code.');

    await bus.request(
      new SetUserPrompt({
        content: 'CUSTOM BASE PROMPT\n\nFollow the project rules.',
      }),
    );
    let current = await bus.request(new GetSystemPrompt());
    expect(current).toContain('CUSTOM BASE PROMPT');
    expect(current).toContain('Follow the project rules.');
    expect(current).toContain('Always use tabs for code.');

    bus.publish(
      new RegisterPromptSection({
        section: {
          id: 'demo',
          title: 'Demo',
          content: 'section body',
          order: 50,
          source: 'test',
        },
      }),
    );
    await bus.idle();
    current = await bus.request(new GetSystemPrompt());
    expect(current).toContain('# Demo');
    expect(current).toContain('section body');
    expect((await bus.request(new GetPromptSections())).some((s) => s.id === 'demo')).toBe(
      true,
    );

    bus.publish(new UnregisterPromptSection({ id: 'demo' }));
    await bus.idle();
    expect(await bus.request(new GetSystemPrompt())).not.toContain('section body');
  });

  test('discovers AGENTS.md by walking up from cwd', async () => {
    const dir = await tempDir();
    const nested = join(dir, 'packages', 'app');
    await mkdir(nested, { recursive: true });
    await writeFile(join(dir, 'AGENTS.md'), 'WORKSPACE RULE', 'utf8');
    const bus = new AsyncEventBus();
    const prompt = new PromptModule({
      userPromptPath: join(dir, 'prompt.md'),
      cwd: nested,
    });
    await prompt.start(bus);
    await bus.start();
    const info = await bus.request(new GetPromptInfo());
    expect(info.agentsPath).toBe(join(dir, 'AGENTS.md'));
    expect(info.prompt).toContain('WORKSPACE RULE');
  });

  test('reloads user prompt and AGENTS.md from disk', async () => {
    const dir = await tempDir();
    const userPath = join(dir, 'system-prompt.md');
    const agentsPath = join(dir, 'AGENTS.md');
    await writeFile(agentsPath, 'old instruction', 'utf8');
    const bus = new AsyncEventBus();
    const prompt = new PromptModule({ userPromptPath: userPath, agentsPath, cwd: dir });
    await prompt.start(bus);
    await bus.start();

    await writeFile(userPath, 'NEW USER PROMPT', 'utf8');
    await writeFile(agentsPath, 'new instruction', 'utf8');
    const info = await bus.request(new ReloadPrompts());
    expect(info.userPrompt).toBe('NEW USER PROMPT');
    expect(info.prompt).toContain('NEW USER PROMPT');
    expect(info.prompt).toContain('new instruction');
    expect(info.prompt).not.toContain('old instruction');
    expect((await bus.request(new GetPromptInfo())).agentsPath).toBe(agentsPath);
  });

  test('ToolsModule contributes a Tool Runtime section over the bus', async () => {
    const dir = await tempDir();
    const bus = new AsyncEventBus();
    const prompt = new PromptModule({
      userPromptPath: join(dir, 'prompt.md'),
      agentsPath: join(dir, 'AGENTS.md'),
      cwd: dir,
    });
    const tools = new ToolsModule({
      roots: [dir],
      plugins: false,
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
    });
    await prompt.start(bus);
    await tools.start(bus);
    await bus.start();
    await bus.idle();

    const systemPrompt = await bus.request(new GetSystemPrompt());
    expect(systemPrompt).toContain('Tool Runtime');
    expect(systemPrompt).toContain('Available tools');
    const sections = await bus.request(new GetPromptSections());
    expect(sections.some((section) => section.id === 'tools')).toBe(true);
  });

  test('Agent uses the assembled prompt instead of its static fallback', async () => {
    const dir = await tempDir();
    const bus = new AsyncEventBus();
    const prompt = new PromptModule({
      userPromptPath: join(dir, 'prompt.md'),
      agentsPath: join(dir, 'AGENTS.md'),
      cwd: dir,
    });
    const session = new SessionModule();
    const tools = new ToolsModule({
      roots: [dir],
      plugins: false,
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
    });
    const calls: ChatRequest[] = [];
    const model: ChatModel = {
      async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
        calls.push(request);
        yield {
          type: 'done',
          result: {
            content: 'ok',
            thinking: '',
            toolCalls: [],
            finishReason: 'stop',
            usage: { promptTokens: 10, completionTokens: 1, totalTokens: 11 },
          },
        };
      },
    };
    const agent = new AgentModule({ model, systemPrompt: 'STATIC FALLBACK' });
    await prompt.start(bus);
    await session.start(bus);
    await tools.start(bus);
    await agent.start(bus);
    await bus.start();
    await bus.request(
      new SetUserPrompt({ content: 'USER ASSEMBLED PROMPT' }),
    );

    bus.publish(new UserTurnRequested({ sessionId: 's1', userText: 'hi' }));
    await agent.whenIdle();
    await bus.idle();

    expect(calls[0]?.systemPrompt).toContain('USER ASSEMBLED PROMPT');
    expect(calls[0]?.systemPrompt).toContain('Tool Runtime');
    expect(calls[0]?.systemPrompt).not.toContain('STATIC FALLBACK');
  });

  test('SkillModule registers and unregisters the per-turn skills section', async () => {
    const dir = await tempDir();
    const bus = new AsyncEventBus();
    const prompt = new PromptModule({
      userPromptPath: join(dir, 'prompt.md'),
      agentsPath: join(dir, 'AGENTS.md'),
      cwd: dir,
    });
    const session = new SessionModule();
    const tools = new ToolsModule({
      roots: [dir],
      plugins: false,
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
    });
    const store = new SkillStore({ path: join(dir, 'skills.json') });
    const skill = createSkill({
      name: 'debug-import',
      pattern: 'python import error',
      instruction: 'read traceback',
      tags: ['python'],
    });
    skill.status = 'ACTIVE';
    await store.add(skill);
    const skills = new SkillModule({ store, registerTools: false });
    await prompt.start(bus);
    await session.start(bus);
    await tools.start(bus);
    await skills.start(bus);
    await bus.start();

    bus.publish(
      new UserTurnRequested({ sessionId: 's1', userText: 'python import error' }),
    );
    await bus.idle();
    let systemPrompt = await bus.request(new GetSystemPrompt({ sessionId: 's1' }));
    expect(systemPrompt).toContain('Available Skills');
    expect(systemPrompt).toContain('debug-import');

    bus.publish(
      new TurnCompleted({
        sessionId: 's1',
        turnId: 't1',
        messages: [],
      }),
    );
    await bus.idle();
    systemPrompt = await bus.request(new GetSystemPrompt({ sessionId: 's1' }));
    expect(systemPrompt).not.toContain('debug-import');
  });
});