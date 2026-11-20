import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { ToolsModule } from '../src/tools/module';
import { PermissionPolicy, AuditLogger } from '../src/tools/permissions';
import { MCPModule, qualifyMcpToolName, type MCPModuleOptions } from '../src/mcp/module';
import { PromptModule } from '../src/prompts/module';
import { GetPromptSections } from '../src/kernel/contracts/prompt';
import { parseMcpConfig, type MCPServerConfig } from '../src/mcp/config';
import { parseSseResponse } from '../src/mcp/transport';
import {
  ExecuteTool,
  GetMCPStatus,
  GetToolCatalog,
} from '../src/kernel/contracts/tools';
import {
  RegisterPromptSection,
  RuntimeContextNotice,
  UnregisterPromptSection,
} from '../src/kernel/contracts/prompt';

const tempDirs: string[] = [];
const fixture = resolve(import.meta.dir, 'fixtures', 'mcp-stdio-server.ts');

afterEach(async () => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

function stdioConfig(): MCPServerConfig {
  return {
    name: 'demo',
    transport: 'stdio',
    command: process.execPath,
    args: [fixture],
    env: {},
    url: '',
    headers: {},
    timeout: 15,
    sseReadTimeout: null,
    enabled: true,
  };
}

async function setup(
  configs: MCPServerConfig[],
  mcpOptions: MCPModuleOptions = {},
): Promise<{
  bus: AsyncEventBus;
  tools: ToolsModule;
  mcp: MCPModule;
  dir: string;
}> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-mcp-'));
  tempDirs.push(dir);
  const bus = new AsyncEventBus();
  const tools = new ToolsModule({
    roots: [dir],
    policy: new PermissionPolicy({ allowed: ['read', 'network'], env: {} }),
    audit: new AuditLogger(join(dir, 'audit.jsonl')),
    approvalTimeoutMs: 1_000,
    plugins: false,
  });
  const mcp = new MCPModule({ configs, connectTimeoutMs: 10_000, requestTimeoutMs: 10_000, ...mcpOptions });
  const prompt = new PromptModule();
  await tools.start(bus);
  await prompt.start(bus);
  await mcp.start(bus);
  await bus.start();
  return { bus, tools, mcp, dir };
}

async function run(bus: AsyncEventBus, name: string, args: Record<string, unknown>) {
  return bus.request(new ExecuteTool({ name, args, sessionId: 'mcp-test' }));
}

describe('MCP config', () => {
  test('parses stdio and streamable-http servers', () => {
    const configs = parseMcpConfig({
      mcpServers: {
        local: {
          command: 'npx',
          args: ['-y', 'server'],
          env: { TOKEN: 123 },
        },
        remote: {
          transport: 'streamable_http',
          url: 'https://example.com/mcp',
          headers: { Authorization: 'Bearer x' },
          timeout: 5,
          disabled: true,
        },
        broken: { args: ['no-command'] },
      },
    });
    expect(configs).toHaveLength(2);
    const local = configs.find((config) => config.name === 'local');
    expect(local?.transport).toBe('stdio');
    expect(local?.args).toEqual(['-y', 'server']);
    expect(local?.env).toEqual({ TOKEN: '123' });
    expect(local?.enabled).toBe(true);
    const remote = configs.find((config) => config.name === 'remote');
    expect(remote?.transport).toBe('streamable-http');
    expect(remote?.url).toBe('https://example.com/mcp');
    expect(remote?.headers).toEqual({ Authorization: 'Bearer x' });
    expect(remote?.enabled).toBe(false);
  });

  test('parses SSE responses', () => {
    const body = [
      'event: message',
      'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}',
      '',
      'event: message',
      'data: {"jsonrpc":"2.0","id":7,"result":{"tools":[]}}',
    ].join('\n');
    const message = parseSseResponse(body, 7);
    expect(message?.id).toBe(7);
    expect(message?.result).toEqual({ tools: [] });
  });
});

describe('MCPModule over stdio', () => {
  test('connects, lists tools and executes a provider tool', async () => {
    const config = stdioConfig();
    const { bus, mcp } = await setup([config]);
    await mcp.whenIdle();
    await bus.idle();

    const status = await bus.request(new GetMCPStatus());
    expect(status.servers).toHaveLength(1);
    expect(status.servers[0]?.name).toBe('demo');
    expect(status.servers[0]?.status).toBe('CONNECTED');
    expect(status.servers[0]?.toolCount).toBe(1);

    const qualified = qualifyMcpToolName('demo', 'echo');
    expect(qualified).toBe('mcp__demo__echo');
    const catalog = await bus.request(new GetToolCatalog());
    expect(catalog.map((spec) => spec.name)).toContain(qualified);

    const result = await run(bus, qualified, { text: 'hi' });
    expect(result.error).toBeUndefined();
    expect(result.output).toBe('echo:hi');

    await mcp.stop();
    await bus.shutdown();
  }, 20_000);

  test('reports unreachable servers without blocking startup', async () => {
    const config = stdioConfig();
    config.name = 'missing';
    config.command = join(tmpdir(), 'definitely-not-a-real-command-xyz');
    const { bus, mcp } = await setup([config], { connectTimeoutMs: 2_000 });
    await mcp.whenIdle();
    await bus.idle();

    const status = await bus.request(new GetMCPStatus());
    expect(status.servers[0]?.status).toBe('ERROR');
    expect(status.servers[0]?.error).toBeTruthy();
    await mcp.stop();
    await bus.shutdown();
  }, 20_000);
});

describe('MCP legacy SSE transport', () => {
  test('connects over HTTP+SSE and calls a tool', async () => {
    const encoder = new TextEncoder();
    const streams = new Set<ReadableStreamDefaultController<Uint8Array>>();
    const sendEvent = (event: string, payload: unknown) => {
      const frame = `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
      for (const controller of [...streams]) {
        try {
          controller.enqueue(encoder.encode(frame));
        } catch {
          streams.delete(controller);
        }
      }
    };

    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const url = new URL(request.url);
        if (request.method === 'GET' && url.pathname === '/sse') {
          const stream = new ReadableStream<Uint8Array>({
            start(controller) {
              streams.add(controller);
              controller.enqueue(
                encoder.encode('event: endpoint\ndata: /message?sessionId=sse-1\n\n'),
              );
            },
          });
          return new Response(stream, {
            headers: { 'content-type': 'text/event-stream' },
          });
        }
        if (request.method === 'POST' && url.pathname === '/message') {
          const body = (await request.json()) as any;
          if (body.method === 'initialize') {
            sendEvent('message', {
              jsonrpc: '2.0',
              id: body.id,
              result: { protocolVersion: '2024-11-05', capabilities: { tools: {} } },
            });
          } else if (body.method === 'tools/list') {
            sendEvent('message', {
              jsonrpc: '2.0',
              id: body.id,
              result: {
                tools: [
                  {
                    name: 'echo',
                    description: 'SSE echo',
                    inputSchema: {
                      type: 'object',
                      properties: { text: { type: 'string' } },
                    },
                  },
                ],
              },
            });
          } else if (body.method === 'tools/call') {
            sendEvent('message', {
              jsonrpc: '2.0',
              id: body.id,
              result: {
                content: [
                  { type: 'text', text: `sse:${String(body.params?.arguments?.text ?? '')}` },
                ],
              },
            });
          }
          return new Response(null, { status: 202 });
        }
        return new Response('not found', { status: 404 });
      },
    });

    try {
      const config: MCPServerConfig = {
        name: 'sse-demo',
        transport: 'sse',
        command: '',
        args: [],
        env: {},
        url: `http://127.0.0.1:${server.port}/sse`,
        headers: {},
        timeout: 10,
        sseReadTimeout: null,
        enabled: true,
      };
      const { bus, mcp } = await setup([config], { healthCheck: false });
      await mcp.whenIdle();
      await bus.idle();

      const status = await bus.request(new GetMCPStatus());
      expect(status.servers[0]?.status).toBe('CONNECTED');
      const qualified = qualifyMcpToolName('sse-demo', 'echo');
      const catalog = await bus.request(new GetToolCatalog());
      expect(catalog.map((spec) => spec.name)).toContain(qualified);

      const result = await run(bus, qualified, { text: 'hi' });
      expect(result.error).toBeUndefined();
      expect(result.output).toBe('sse:hi');

      await mcp.stop();
      await bus.shutdown();
    } finally {
      for (const controller of [...streams]) {
        try {
          controller.close();
        } catch {
          // already closed
        }
      }
      streams.clear();
      server.stop(true);
    }
  }, 20_000);
});

describe('MCP health check', () => {
  test('refreshes tools and marks unreachable servers as ERROR', async () => {
    let toolName = 'echo';
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        const body = (await request.json()) as any;
        const headers = { 'mcp-session-id': 'sess-h' };
        if (body.method === 'initialize') {
          return Response.json(
            {
              jsonrpc: '2.0',
              id: body.id,
              result: {
                protocolVersion: '2025-03-26',
                capabilities: { tools: {} },
                instructions: 'Health server instructions.',
              },
            },
            { headers },
          );
        }
        if (body.method === 'notifications/initialized') {
          return new Response(null, { status: 202, headers });
        }
        if (body.method === 'tools/list') {
          return Response.json(
            {
              jsonrpc: '2.0',
              id: body.id,
              result: {
                tools: [
                  {
                    name: toolName,
                    description: 'echo tool',
                    inputSchema: { type: 'object', properties: {} },
                  },
                ],
              },
            },
            { headers },
          );
        }
        return Response.json({ jsonrpc: '2.0', id: body.id, result: {} }, { headers });
      },
    });

    try {
      const config: MCPServerConfig = {
        name: 'health-demo',
        transport: 'streamable-http',
        command: '',
        args: [],
        env: {},
        url: `http://127.0.0.1:${server.port}/mcp`,
        headers: {},
        timeout: 5,
        sseReadTimeout: null,
        enabled: true,
      };
      const { bus, mcp } = await setup([config], { healthCheck: false });
      await mcp.whenIdle();
      await bus.idle();
      const unregistered: string[] = [];
      const notices: string[] = [];
      await bus.subscribe(RegisterPromptSection, () => undefined);
      await bus.subscribe(UnregisterPromptSection, (event) => {
        if (event.id === 'mcp') unregistered.push(event.id);
      });
      await bus.subscribe(RuntimeContextNotice, (event) => {
        notices.push(event.text);
      });
      expect(qualifyMcpToolName('health-demo', 'echo')).toBe('mcp__health-demo__echo');
      let catalog = await bus.request(new GetToolCatalog());
      expect(catalog.map((spec) => spec.name)).toContain('mcp__health-demo__echo');

      toolName = 'echo2';
      await mcp.refreshHealth();
      await bus.idle();
      catalog = await bus.request(new GetToolCatalog());
      expect(catalog.map((spec) => spec.name)).toContain('mcp__health-demo__echo2');
      expect(catalog.map((spec) => spec.name)).not.toContain('mcp__health-demo__echo');
      let status = await bus.request(new GetMCPStatus());
      expect(status.servers[0]?.status).toBe('CONNECTED');
      expect(status.servers[0]?.toolCount).toBe(1);
      const sections = await bus.request(new GetPromptSections());
      const mcpSection = sections.find((section) => section.id === 'mcp');
      expect(mcpSection?.content).toContain('health-demo');
      expect(mcpSection?.content).not.toContain('ERROR');
      const toolsSection = sections.find((section) => section.id === 'tools');
      expect(toolsSection?.content).toContain('Use the provided tool schemas');
      expect(toolsSection?.content).not.toContain('mcp__health-demo__echo');

      // Reload closes the old fleet, reconnects, and republishes the catalog.
      const reloaded = await mcp.reload();
      expect(reloaded.ok).toBe(true);
      expect(reloaded.connected).toBe(1);
      expect(reloaded.tools).toBe(1);
      await bus.idle();
      catalog = await bus.request(new GetToolCatalog());
      expect(catalog.map((spec) => spec.name)).toContain('mcp__health-demo__echo2');
      expect(notices.some((text) => text.includes('MCP reloaded'))).toBe(true);

      server.stop(true);
      await mcp.refreshHealth();
      await bus.idle();
      status = await bus.request(new GetMCPStatus());
      expect(status.servers[0]?.status).toBe('ERROR');
      catalog = await bus.request(new GetToolCatalog());
      expect(catalog.map((spec) => spec.name)).not.toContain('mcp__health-demo__echo2');
      expect(unregistered.length).toBeGreaterThan(0);
      expect(notices.some((text) => text.includes('unavailable'))).toBe(true);
      const afterStop = await bus.request(new GetPromptSections());
      expect(afterStop.some((section) => section.id === 'mcp')).toBe(false);

      await mcp.stop();
      await bus.shutdown();
    } finally {
      server.stop(true);
    }
  }, 20_000);
});

describe('MCPModule over streamable-http', () => {
  test('initializes, lists and calls tools with a session id', async () => {
    let seenSessionId = '';
    const server = Bun.serve({
      port: 0,
      async fetch(request) {
        if (request.method !== 'POST') return new Response('', { status: 405 });
        const body = (await request.json()) as any;
        const sessionId = request.headers.get('mcp-session-id') ?? '';
        if (sessionId) seenSessionId = sessionId;
        const headers = { 'mcp-session-id': 'sess-1' };
        if (body.method === 'initialize') {
          return Response.json(
            {
              jsonrpc: '2.0',
              id: body.id,
              result: {
                protocolVersion: '2025-03-26',
                capabilities: { tools: {} },
                serverInfo: { name: 'http-demo', version: '1.0.0' },
              },
            },
            { headers },
          );
        }
        if (body.method === 'notifications/initialized') {
          return new Response(null, { status: 202, headers });
        }
        if (body.method === 'tools/list') {
          return Response.json(
            {
              jsonrpc: '2.0',
              id: body.id,
              result: {
                tools: [
                  {
                    name: 'echo',
                    description: 'Echo',
                    inputSchema: {
                      type: 'object',
                      properties: { text: { type: 'string' } },
                    },
                  },
                ],
              },
            },
            { headers },
          );
        }
        if (body.method === 'tools/call') {
          return Response.json(
            {
              jsonrpc: '2.0',
              id: body.id,
              result: { content: [{ type: 'text', text: `http:${String(body.params?.arguments?.text ?? '')}` }] },
            },
            { headers },
          );
        }
        return Response.json(
          { jsonrpc: '2.0', id: body.id, error: { code: -32601, message: 'method not found' } },
          { headers },
        );
      },
    });

    try {
      const config: MCPServerConfig = {
        name: 'http-demo',
        transport: 'streamable-http',
        command: '',
        args: [],
        env: {},
        url: `http://127.0.0.1:${server.port}/mcp`,
        headers: {},
        timeout: 10,
        sseReadTimeout: null,
        enabled: true,
      };
      const { bus, mcp } = await setup([config]);
      await mcp.whenIdle();
      await bus.idle();

      const qualified = qualifyMcpToolName('http-demo', 'echo');
      const result = await run(bus, qualified, { text: 'yo' });
      expect(result.error).toBeUndefined();
      expect(result.output).toBe('http:yo');
      expect(seenSessionId).toBe('sess-1');

      await mcp.stop();
      await bus.shutdown();
    } finally {
      server.stop(true);
    }
  }, 20_000);
});