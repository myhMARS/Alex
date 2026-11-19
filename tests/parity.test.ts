import { afterEach, describe, expect, test } from 'bun:test';
import { readFileSync, readdirSync } from 'node:fs';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { ToolsModule } from '../src/tools/module';
import { CronModule } from '../src/cron/module';
import { SkillModule } from '../src/skill/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { GetToolCatalog } from '../src/kernel/contracts/tools';
import { StoreModule } from '../src/modules/store/module';
import { ListSessions, LoadSession } from '../src/kernel/contracts/session';
import { SkillStore } from '../src/skill/store';
import { CronStore } from '../src/cron/store';

const TS_ROOT = join(import.meta.dir, '..');
const PYTHON_SNAPSHOT = join(import.meta.dir, 'fixtures', 'python-contracts.json');

interface PythonSnapshot {
  contractClasses: string[];
  toolNames: string[];
}

function pythonSnapshot(): PythonSnapshot {
  return JSON.parse(readFileSync(PYTHON_SNAPSHOT, 'utf8')) as PythonSnapshot;
}

const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(prefix: string): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), prefix));
  tempDirs.push(dir);
  return dir;
}

function matchAll(text: string, pattern: RegExp): string[] {
  return [...text.matchAll(pattern)].map((match) => match[1]!).filter(Boolean);
}

function pythonContractClasses(): string[] {
  // Frozen from Python before the legacy implementation was removed.
  // Memory contracts were intentionally removed when MemoryModule was deleted.
  const removed = new Set([
    'AppendMessages',
    'ClearMemory',
    'GetContext',
    'ReplaceMemory',
  ]);
  return pythonSnapshot().contractClasses.filter((name) => !removed.has(name));
}

function typescriptContractClasses(): string[] {
  const dir = join(TS_ROOT, 'src', 'kernel', 'contracts');
  const names = new Set<string>();
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.ts')) continue;
    for (const name of matchAll(readFileSync(join(dir, file), 'utf8'), /^export class (\w+)/gm)) {
      names.add(name);
    }
  }
  return [...names].sort();
}

function pythonToolNames(): string[] {
  return pythonSnapshot().toolNames;
}

async function typescriptCatalog(): Promise<string[]> {
  const dir = await tempDir('alex-ts-parity-');
  const bus = new AsyncEventBus();
  const tools = new ToolsModule({
    roots: [dir],
    policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
    audit: new AuditLogger(join(dir, 'audit.jsonl')),
    approvalTimeoutMs: 200,
    plugins: false,
  });
  const cron = new CronModule({ directory: join(dir, 'cron'), registerTools: true });
  const skill = new SkillModule({
    skillsPath: join(dir, 'skills.json'),
    registerTools: true,
  });
  await tools.start(bus);
  await cron.start(bus);
  await skill.start(bus);
  await bus.start();
  const catalog = await bus.request(new GetToolCatalog());
  await bus.shutdown();
  return catalog.map((spec) => spec.name).sort();
}

describe('frozen Python ↔ TS parity', () => {
  test('every Python kernel contract (frozen snapshot) has a TypeScript counterpart', () => {
    const python = pythonContractClasses();
    const typescript = new Set(typescriptContractClasses());
    const missing = python.filter((name) => !typescript.has(name));
    expect(missing, `missing TS contracts: ${missing.join(', ')}`).toEqual([]);
    expect(python.length).toBeGreaterThanOrEqual(35);
  });

  test('every Python built-in tool (frozen snapshot) is in the TS catalog', async () => {
    const python = pythonToolNames();
    const typescript = new Set(await typescriptCatalog());
    const missing = python.filter((name) => !typescript.has(name));
    expect(missing, `missing TS tools: ${missing.join(', ')}`).toEqual([]);

    const extra = [...typescript].filter((name) => !python.includes(name)).sort();
    expect(extra).toEqual(['list_skills', 'load_skill']);
  });

  test('loads the latest v3 session file', async () => {
    const dir = await tempDir('alex-ts-parity-session-');
    await mkdir(join(dir, 'sessions'), { recursive: true });
    await writeFile(
      join(dir, 'sessions', 'py-session.jsonl'),
      [
        JSON.stringify({
          kind: 'session',
          format_version: 3,
          session_id: 'py-session',
          created_at: '2026-01-01T00:00:00+00:00',
          first_message: 'hello from python',
          cron_history: [],
        }),
        JSON.stringify({
          seq: 1,
          type: 'user_message',
          at: '2026-01-01T00:00:00Z',
          data: { message: { role: 'user', content: 'hello from python' } },
        }),
        JSON.stringify({
          seq: 2,
          type: 'assistant_message',
          at: '2026-01-01T00:00:01Z',
          data: { message: { role: 'assistant', content: 'hi', reasoning_content: '' } },
        }),
      ].join('\n') + '\n',
      'utf8',
    );
    const bus = new AsyncEventBus();
    const store = new StoreModule({ sessionsDir: join(dir, 'sessions') });
    await store.start(bus);
    await bus.start();

    const sessions = await bus.request(new ListSessions());
    expect(sessions[0]?.sessionId).toBe('py-session');
    expect(sessions[0]?.firstMessage).toBe('hello from python');
    expect(sessions[0]?.messageCount).toBe(2);

    const bundle = await bus.request(new LoadSession({ targetSessionId: 'py-session' }));
    expect(bundle?.messages[1]?.reasoning_content).toBe('');
    await bus.shutdown();
  });

  test('loads a Python-shaped skills file', async () => {
    const dir = await tempDir('alex-ts-parity-skills-');
    const path = join(dir, 'skills.json');
    await writeFile(
      path,
      JSON.stringify([
        {
          id: 'pyskill00001',
          name: 'python-skill',
          pattern: 'python import error',
          instruction: '1. read traceback',
          tags: ['python'],
          examples: [],
          status: 'ACTIVE',
          use_count: 3,
          success_count: 2,
          failure_count: 1,
          confidence: 0.6,
          version: 2,
        },
      ]),
      'utf8',
    );
    const store = new SkillStore({ path });
    await store.load();
    const skill = store.get('pyskill00001');
    expect(skill?.name).toBe('python-skill');
    expect(skill?.useCount).toBe(3);
    expect(skill?.status).toBe('ACTIVE');
  });

  test('loads a Python-shaped cron file', async () => {
    const dir = await tempDir('alex-ts-parity-cron-');
    await writeFile(
      join(dir, 'pyjob.json'),
      JSON.stringify({
        id: 'pyjob',
        session_id: 's1',
        name: 'python job',
        cron: '0 9 * * *',
        prompt: 'daily digest',
        recurring: true,
        durable: true,
        status: 'SCHEDULED',
        runs_done: 2,
        last_started_at: 1767225600,
        last_finished_at: 1767225660,
        last_error: '',
        last_result: 'ok',
        next_run_at: 1767312000,
      }),
      'utf8',
    );
    const store = new CronStore({ directory: dir });
    const jobs = await store.restoreAll();
    expect(jobs).toHaveLength(1);
    expect(jobs[0]?.sessionId).toBe('s1');
    expect(jobs[0]?.runsDone).toBe(2);
    expect(jobs[0]?.durable).toBe(true);
  });
});