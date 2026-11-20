import { afterEach, describe, expect, test } from 'bun:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { ToolsModule, parseGitStatusPorcelainZ } from '../src/tools/module';
import { TuiModule } from '../src/modules/tui/module';
import { ToolRegistry } from '../src/tools/registry';
import { AuditLogger, PermissionPolicy } from '../src/tools/permissions';
import {
  ExecuteTool,
  GetToolCatalog,
  ListDirectory,
  ListFileTree,
  SetPermissionMode,
  ToolApprovalRequested,
  ToolApprovalResolved,
} from '../src/kernel/contracts/tools';
import type { ChatModel, ChatStreamEvent } from '../src/llm/client';
import type { Tool } from '../src/tools/tool';
import { checkShellCommand } from '../src/tools/builtin/shell';
import { parseDuckDuckGoResults } from '../src/tools/builtin/web';

const execFileAsync = promisify(execFile);
const tempDirs: string[] = [];

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

class ReviewerModel implements ChatModel {
  constructor(private readonly payload: string) {}

  async *streamChat(): AsyncIterable<ChatStreamEvent> {
    yield { type: 'content', delta: this.payload };
    yield {
      type: 'done',
      result: {
        content: this.payload,
        thinking: '',
        toolCalls: [],
        finishReason: 'stop',
      },
    };
  }
}

interface Env {
  dir: string;
  auditPath: string;
  bus: AsyncEventBus;
  tools: ToolsModule;
}

async function makeEnv(
  options: {
    allowed?: string[];
    denied?: string[];
    approvalTimeoutMs?: number;
    reviewer?: ChatModel;
  } = {},
): Promise<Env> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-tools-'));
  tempDirs.push(dir);
  const auditPath = join(dir, 'audit.jsonl');
  const bus = new AsyncEventBus();
  const tools = new ToolsModule({
    roots: [dir],
    policy: new PermissionPolicy({
      allowed: options.allowed ?? ['read'],
      denied: options.denied ?? [],
    }),
    audit: new AuditLogger(auditPath),
    approvalTimeoutMs: options.approvalTimeoutMs ?? 2_000,
    plugins: false,
    ...(options.reviewer ? { reviewer: options.reviewer } : {}),
  });
  await tools.start(bus);
  await bus.start();
  return { dir, auditPath, bus, tools };
}

async function autoResolve(bus: AsyncEventBus, granted: boolean, remember = false): Promise<void> {
  await bus.subscribe(ToolApprovalRequested, (event) => {
    bus.publish(
      new ToolApprovalResolved({ reqId: event.reqId, granted, remember }),
    );
  });
}

async function run(bus: AsyncEventBus, name: string, args: Record<string, unknown>) {
  return bus.request(new ExecuteTool({ name, args, sessionId: 'test' }));
}

describe('ToolRegistry', () => {
  test('register / get / unregister', () => {
    const registry = new ToolRegistry();
    const tool: Tool = {
      name: 'demo',
      description: 'demo',
      parameters: { type: 'object', properties: {} },
      async invoke() {
        return 'ok';
      },
    };
    registry.register(tool);
    expect(registry.has('demo')).toBe(true);
    expect(registry.get('demo')).toBe(tool);
    expect(registry.names()).toEqual(['demo']);
    expect(registry.unregister('demo')).toBe(true);
    expect(registry.has('demo')).toBe(false);
  });
});

describe('PermissionPolicy', () => {
  test('defaults allow read/network, others ask', () => {
    const policy = new PermissionPolicy({ env: {} });
    expect(policy.check('read')).toBe('allow');
    expect(policy.check('network')).toBe('allow');
    expect(policy.check('write')).toBe('ask');
    expect(policy.check(undefined)).toBe('allow');
  });

  test('explicit deny wins over allow', () => {
    const policy = new PermissionPolicy({ allowed: ['read', 'write'], denied: ['write'], env: {} });
    expect(policy.check('write')).toBe('deny');
    expect(policy.check('read')).toBe('allow');
  });

  test('remember grants a permission', () => {
    const policy = new PermissionPolicy({ allowed: [], env: {} });
    expect(policy.check('shell')).toBe('ask');
    policy.remember('shell');
    expect(policy.check('shell')).toBe('allow');
  });
});

describe('ToolsModule over the bus', () => {
  test('exposes the builtin catalog', async () => {
    const { bus } = await makeEnv();
    const catalog = await bus.request(new GetToolCatalog());
    const names = catalog.map((spec) => spec.name);
    expect(names).toEqual(
      expect.arrayContaining(['time', 'read', 'write', 'edit', 'glob', 'grep', 'git_inspect']),
    );
    const read = catalog.find((spec) => spec.name === 'read');
    expect(read?.jsonSchema).toHaveProperty('properties');
    expect(read?.provider).toBe('builtin');
    await bus.shutdown();
  });

  test('runs the unrestricted time tool', async () => {
    const { bus } = await makeEnv();
    const result = await run(bus, 'time', { timezone: 'UTC' });
    expect(result.error).toBeUndefined();
    expect(result.output).toContain('Current time');
    expect(result.output).toContain('ISO 8601');
    await bus.shutdown();
  });

  test('reads a file with offset/limit', async () => {
    const { bus, dir } = await makeEnv();
    const path = join(dir, 'notes.txt');
    await writeFile(path, 'line1\nline2\nline3', 'utf8');

    const full = await run(bus, 'read', { path });
    expect(full.error).toBeUndefined();
    expect(full.output).toContain('line1');
    expect(full.output).toContain('line3');

    const sliced = await run(bus, 'read', { path, offset: 2, limit: 1 });
    expect(sliced.output).toContain('line2');
    expect(sliced.output).not.toContain('line1');
    expect(sliced.output).not.toContain('line3');
    await bus.shutdown();
  });

  test('refuses paths outside the allowed roots', async () => {
    const { bus, dir } = await makeEnv();
    const result = await run(bus, 'read', { path: join(dir, '..', 'outside.txt') });
    expect(result.output).toContain('outside the allowed roots');
    await bus.shutdown();
  });

  test('write asks for approval, executes when granted, and audits', async () => {
    const { bus, dir, auditPath } = await makeEnv({ allowed: ['read'] });
    await autoResolve(bus, true);

    const path = join(dir, 'out.txt');
    const result = await run(bus, 'write', { path, content: 'hello\n' });
    expect(result.error).toBeUndefined();
    expect(result.output).toContain('Wrote');
    expect(await readFile(path, 'utf8')).toBe('hello\n');

    const audit = await readFile(auditPath, 'utf8');
    expect(audit).toContain('"decision":"granted"');
    expect(audit).toContain('"tool":"write"');
    await bus.shutdown();
  });

  test('approval preview contains a unified diff for write/edit', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['read'] });
    const path = join(dir, 'diff-me.txt');
    await writeFile(path, 'old line\nkept\n', 'utf8');

    const previews: string[] = [];
    await bus.subscribe(ToolApprovalRequested, (event) => {
      previews.push(event.preview);
      bus.publish(new ToolApprovalResolved({ reqId: event.reqId, granted: true }));
    });

    const written = await run(bus, 'write', { path, content: 'new line\nkept\n' });
    expect(written.error).toBeUndefined();
    expect(previews[0]).toContain('--- a/');
    expect(previews[0]).toContain('+++ b/');
    expect(previews[0]).toContain('-old line');
    expect(previews[0]).toContain('+new line');

    const edited = await run(bus, 'edit', {
      path,
      old_string: 'new line',
      new_string: 'edited line',
    });
    expect(edited.error).toBeUndefined();
    expect(previews[1]).toContain('-new line');
    expect(previews[1]).toContain('+edited line');
    await bus.shutdown();
  });

  test('read-only mode rejects writes and full mode skips approval', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['read'] });
    const path = join(dir, 'mode.txt');
    await bus.request(new SetPermissionMode({ mode: 'read-only' }));
    const readonly = await run(bus, 'write', { path, content: 'x' });
    expect(readonly.error).toContain('read-only');

    await bus.request(new SetPermissionMode({ mode: 'full' }));
    const full = await run(bus, 'write', { path, content: 'x' });
    expect(full.error).toBeUndefined();
    expect(await readFile(path, 'utf8')).toBe('x');
    await bus.shutdown();
  });

  test('model mode lets the reviewer approve a write', async () => {
    const reviewer = new ReviewerModel('{"decision":"allow"}');
    const { bus, dir } = await makeEnv({ allowed: ['read'], reviewer });
    await bus.request(new SetPermissionMode({ mode: 'model' }));
    const path = join(dir, 'reviewed.txt');
    const result = await run(bus, 'write', { path, content: 'reviewed' });
    expect(result.error).toBeUndefined();
    expect(await readFile(path, 'utf8')).toBe('reviewed');
    await bus.shutdown();
  });

  test('model mode lets the reviewer deny a write', async () => {
    const reviewer = new ReviewerModel('{"decision":"deny","reason":"unsafe"}');
    const { bus, dir } = await makeEnv({ allowed: ['read'], reviewer });
    await bus.request(new SetPermissionMode({ mode: 'model' }));
    const path = join(dir, 'denied.txt');
    const result = await run(bus, 'write', { path, content: 'reviewed' });
    expect(result.error).toContain('model reviewer denied');
    await expect(readFile(path, 'utf8')).rejects.toThrow();
    await bus.shutdown();
  });

  test('switching to full mode resolves an already-pending approval', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['read'] });
    let approvalId = '';
    await bus.subscribe(ToolApprovalRequested, (event) => {
      approvalId = event.reqId;
    });
    const resolved: ToolApprovalResolved[] = [];
    await bus.subscribe(ToolApprovalResolved, (event) => resolved.push(event));

    const path = join(dir, 'pending.txt');
    const pending = run(bus, 'write', { path, content: 'pending' });
    for (let index = 0; index < 20 && !approvalId; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    expect(approvalId).not.toBe('');
    await bus.request(new SetPermissionMode({ mode: 'full' }));
    const result = await pending;
    expect(result.error).toBeUndefined();
    expect(await readFile(path, 'utf8')).toBe('pending');
    expect(resolved.some((event) => event.granted)).toBe(true);
    await bus.shutdown();
  });

  test('denied approval blocks the write and audits the denial', async () => {
    const { bus, dir, auditPath } = await makeEnv({ allowed: ['read'] });
    await autoResolve(bus, false);

    const path = join(dir, 'nope.txt');
    const result = await run(bus, 'write', { path, content: 'should not land' });
    expect(result.error).toContain('user denied');
    await expect(readFile(path, 'utf8')).rejects.toThrow();

    const audit = await readFile(auditPath, 'utf8');
    expect(audit).toContain('"decision":"denied"');
    await bus.shutdown();
  });

  test('approval timeout blocks the tool', async () => {
    const { bus } = await makeEnv({ allowed: ['read'], approvalTimeoutMs: 30 });
    const result = await run(bus, 'write', { path: join(tempDirs[0] ?? '.', 'x.txt'), content: 'x' });
    expect(result.error).toContain('timed out');
    await bus.shutdown();
  });

  test('edit replaces a unique string when write is pre-allowed', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['read', 'write'] });
    const path = join(dir, 'edit.txt');
    await writeFile(path, 'alpha beta gamma', 'utf8');

    const result = await run(bus, 'edit', { path, old_string: 'beta', new_string: 'BETA' });
    expect(result.error).toBeUndefined();
    expect(await readFile(path, 'utf8')).toBe('alpha BETA gamma');
    await bus.shutdown();
  });

  test('glob finds files and skips ignored directories', async () => {
    const { bus, dir } = await makeEnv();
    await mkdir(join(dir, 'sub'), { recursive: true });
    await mkdir(join(dir, 'node_modules'), { recursive: true });
    await writeFile(join(dir, 'a.ts'), '', 'utf8');
    await writeFile(join(dir, 'sub', 'b.ts'), '', 'utf8');
    await writeFile(join(dir, 'node_modules', 'c.ts'), '', 'utf8');

    const result = await run(bus, 'glob', { pattern: '**/*.ts', path: dir });
    expect(result.error).toBeUndefined();
    expect(result.output).toContain('a.ts');
    expect(result.output).toContain('b.ts');
    expect(result.output).not.toContain('node_modules');
    await bus.shutdown();
  });

  test('read/write/edit accept Python-compatible encoding and create_dirs options', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['read', 'write'] });
    const nested = join(dir, 'nested', 'file.txt');

    const missingParent = await run(bus, 'write', { path: nested, content: 'x' });
    expect(String(missingParent.error ?? missingParent.output ?? '')).toContain('create_dirs');

    const written = await run(bus, 'write', {
      path: nested,
      content: 'héllo',
      create_dirs: true,
    });
    expect(written.error).toBeUndefined();

    const read = await run(bus, 'read', { path: nested });
    expect(read.output).toContain('héllo');

    const edited = await run(bus, 'edit', {
      file_path: nested,
      old_string: 'héllo',
      new_string: 'bye',
    });
    expect(edited.error).toBeUndefined();
    expect((await run(bus, 'read', { path: nested })).output).toContain('bye');

    const latin1 = join(dir, 'latin1.txt');
    await writeFile(latin1, Buffer.from('héllo latin1', 'latin1'));
    const decoded = await run(bus, 'read', { path: latin1, encoding: 'latin1' });
    expect(decoded.output).toContain('héllo latin1');
    await bus.shutdown();
  });

  test('grep supports Python-style options', async () => {
    const { bus, dir } = await makeEnv();
    await writeFile(join(dir, 'keep.py'), 'Hello\nneedle here\nnext\n', 'utf8');
    await writeFile(join(dir, 'other.txt'), 'needle\n', 'utf8');
    await writeFile(join(dir, 'ignore.py'), 'hello\n', 'utf8');

    const withContext = await run(bus, 'grep', {
      pattern: 'needle',
      path: dir,
      output_mode: 'content',
      context: 1,
    });
    expect(withContext.output).toContain('keep.py-1- Hello');
    expect(withContext.output).toContain('keep.py:2: needle here');

    const counts = await run(bus, 'grep', {
      pattern: 'needle',
      path: dir,
      output_mode: 'count',
    });
    expect(counts.output).toContain('keep.py:1');

    const insensitive = await run(bus, 'grep', {
      pattern: 'HELLO',
      path: dir,
      output_mode: 'files_with_matches',
      ignore_case: true,
    });
    expect(insensitive.output).toContain('keep.py');

    const filtered = await run(bus, 'grep', {
      pattern: 'needle',
      path: dir,
      output_mode: 'files_with_matches',
      glob: '*.py',
    });
    expect(filtered.output).toContain('keep.py');
    expect(filtered.output).not.toContain('other.txt');

    const typed = await run(bus, 'grep', {
      pattern: 'needle',
      path: dir,
      output_mode: 'files_with_matches',
      type: 'py',
    });
    expect(typed.output).toContain('keep.py');
    await bus.shutdown();
  });

  test('parses git porcelain -z output and detects repositories', async () => {
    const statuses = parseGitStatusPorcelainZ(
      ' M src/app.ts\0?? new.txt\0R  renamed.ts\0old.ts\0D  gone.ts\0',
    );
    expect(statuses).toEqual({
      'src/app.ts': 'M',
      'new.txt': '?',
      'renamed.ts': 'R',
      'gone.ts': 'D',
    });

    const { bus, dir } = await makeEnv();
    const plain = await bus.request(new ListFileTree(2, 50));
    expect(plain.isGitRepo).toBe(false);

    await mkdir(join(dir, '.git'), { recursive: true });
    const repo = await bus.request(new ListFileTree(2, 50));
    expect(repo.isGitRepo).toBe(true);
    expect(repo.gitStatus).toEqual({});
    await bus.shutdown();
  });

  test('ListFileTree lists directories first and respects depth', async () => {
    const { bus, dir } = await makeEnv();
    await mkdir(join(dir, 'src'), { recursive: true });
    await writeFile(join(dir, 'src', 'app.ts'), 'const x = 1;', 'utf8');
    await writeFile(join(dir, 'README.md'), '# hi', 'utf8');

    const listing = await bus.request(new ListDirectory('src'));
    expect(listing.path).toBe('src');
    expect(listing.entries.map((entry) => entry.name)).toEqual(['app.ts']);

    const tree = await bus.request(new ListFileTree(2, 50));
    expect(tree.root).toBe(dir);
    expect(tree.entries[0]).toMatchObject({ name: 'src', type: 'dir', depth: 0 });
    expect(tree.entries.some((entry) => entry.path === 'src/app.ts' && entry.depth === 1)).toBe(true);
    expect(tree.entries.some((entry) => entry.path === 'README.md' && entry.type === 'file')).toBe(true);
    expect(tree.truncated).toBe(false);
    await bus.shutdown();
  });

  test('grep searches content and file lists', async () => {
    const { bus, dir } = await makeEnv();
    await writeFile(join(dir, 'one.txt'), 'hello\nneedle here\n', 'utf8');
    await writeFile(join(dir, 'two.txt'), 'no match\n', 'utf8');

    const content = await run(bus, 'grep', {
      pattern: 'needle',
      path: dir,
      output_mode: 'content',
    });
    expect(content.error).toBeUndefined();
    expect(content.output).toContain('one.txt:2: needle here');

    const files = await run(bus, 'grep', {
      pattern: 'needle',
      path: dir,
      output_mode: 'files_with_matches',
    });
    expect(files.output).toContain('one.txt');
    expect(files.output).not.toContain('two.txt');
    await bus.shutdown();
  });

  test('shell safety policy refuses destructive commands', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['shell'] });
    const result = await run(bus, 'bash', { command: 'rm -rf /', working_dir: dir });
    expect(result.output).toContain('refused by safety policy');
    expect(checkShellCommand('mkfs.ext4 /dev/sda1')).toContain('refused');
    await bus.shutdown();
  });

  test.skipIf(!Bun.which('pwsh'))('pwsh runs a command when shell is allowed', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['shell'] });
    const result = await run(bus, 'pwsh', {
      command: 'Write-Output hello-ts',
      working_dir: dir,
    });
    expect(result.error).toBeUndefined();
    expect(result.output).toContain('hello-ts');
    expect(result.output).toContain('exit_code: 0');
    await bus.shutdown();
  }, 30_000);

  test('web_fetch converts HTML to readable text', async () => {
    const server = Bun.serve({
      port: 0,
      fetch() {
        return new Response(
          '<html><head><title>t</title><script>bad()</script></head>' +
            '<body><h1>Hello</h1><p>World</p></body></html>',
          { headers: { 'content-type': 'text/html; charset=utf-8' } },
        );
      },
    });
    try {
      const { bus } = await makeEnv({ allowed: ['network'] });
      const result = await run(bus, 'web_fetch', {
        url: `http://127.0.0.1:${server.port}/`,
      });
      expect(result.error).toBeUndefined();
      expect(result.output).toContain('Hello');
      expect(result.output).toContain('World');
      expect(result.output).not.toContain('bad()');
      await bus.shutdown();
    } finally {
      server.stop(true);
    }
  });

  test('loads tool plugins from pluginDirs', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-plugin-'));
    tempDirs.push(dir);
    const pluginDir = join(dir, 'plugins');
    await mkdir(pluginDir, { recursive: true });
    await writeFile(
      join(pluginDir, 'demo.mjs'),
      `export function register(registry) {
         registry.register({
           name: 'plugin_echo',
           description: 'echo plugin',
           parameters: { type: 'object', properties: {} },
           async invoke(args) { return 'plugin:' + String(args.text ?? ''); },
         });
       }`,
      'utf8',
    );

    const bus = new AsyncEventBus();
    const tools = new ToolsModule({
      roots: [dir],
      policy: new PermissionPolicy({ allowed: ['read'], env: {} }),
      audit: new AuditLogger(join(dir, 'audit.jsonl')),
      plugins: true,
      pluginDirs: [pluginDir],
      approvalTimeoutMs: 1_000,
    });
    await tools.start(bus);
    await bus.start();

    expect(tools.plugins.loaded).toHaveLength(1);
    expect(tools.registry.has('plugin_echo')).toBe(true);
    const result = await run(bus, 'plugin_echo', { text: 'hi' });
    expect(result.output).toBe('plugin:hi');
    await bus.shutdown();
  });

  test('parses DuckDuckGo HTML results', () => {
    const html = `
      <a class="result__a" href="https://example.com/a">Example A</a>
      <a class="result__snippet" href="https://example.com/a">First snippet</a>
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Example B</a>
      <a class="result__snippet" href="https://example.com/b">Second snippet</a>
    `;
    const results = parseDuckDuckGoResults(html);
    expect(results).toHaveLength(2);
    expect(results[0]?.title).toBe('Example A');
    expect(results[0]?.snippet).toBe('First snippet');
    expect(results[1]?.url).toBe('https://example.com/b');
  });

  test.skipIf(!Bun.which('git'))('git_inspect reports status', async () => {
    const { bus, dir } = await makeEnv();
    await execFileAsync('git', ['init', '--quiet'], { cwd: dir });
    const result = await run(bus, 'git_inspect', { action: 'status', path: dir });
    expect(result.error).toBeUndefined();
    expect(result.output).toContain('exit_code: 0');
    await bus.shutdown();
  }, 20_000);
});

describe('permission mode integration', () => {
  test('TUI mode switches are enforced immediately', async () => {
    const { bus, dir } = await makeEnv({ allowed: ['read'] });
    const tui = new TuiModule();
    await tui.start(bus);
    expect(await tui.cyclePermissionMode()).toBe('model');
    expect(await tui.cyclePermissionMode()).toBe('full');
    const path = join(dir, 'immediate.txt');
    const result = await run(bus, 'write', { path, content: 'ok' });
    expect(result.error).toBeUndefined();
    expect(await readFile(path, 'utf8')).toBe('ok');
    await bus.shutdown();
  });
});
