/** JSON-RPC transports for MCP: stdio subprocess and streamable HTTP. */

import { spawn } from 'node:child_process';

import type { MCPServerConfig } from './config';

export interface JsonRpcTransport {
  request<T = unknown>(method: string, params: Record<string, unknown>, timeoutMs: number): Promise<T>;
  notify(method: string, params: Record<string, unknown>): Promise<void>;
  close(): Promise<void>;
}

interface PendingCall {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

interface JsonRpcResponse {
  id?: number | string;
  result?: unknown;
  error?: { code?: number; message?: string };
}

class StdioTransport implements JsonRpcTransport {
  private readonly proc: ReturnType<typeof spawn>;
  private readonly pending = new Map<number, PendingCall>();
  private readonly stderrLines: string[] = [];
  private buffer = '';
  private nextId = 1;
  private closed = false;

  constructor(config: MCPServerConfig) {
    this.proc = spawn(config.command, config.args, {
      env: { ...process.env, ...config.env },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.proc.stdout?.setEncoding('utf8');
    this.proc.stderr?.setEncoding('utf8');
    this.proc.stdout?.on('data', (chunk: string) => this.onData(chunk));
    this.proc.stderr?.on('data', (chunk: string) => {
      this.stderrLines.push(chunk);
      if (this.stderrLines.length > 50) this.stderrLines.shift();
    });
    this.proc.on('exit', (code) => {
      this.rejectAll(new Error(`MCP stdio server exited (code=${String(code)}): ${this.stderrText()}`));
    });
    this.proc.on('error', (error) => {
      this.rejectAll(new Error(`MCP stdio server failed to start: ${error.message}`));
    });
  }

  async request<T = unknown>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<T> {
    if (this.closed) throw new Error('MCP stdio transport is closed');
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timer,
      });
      const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params });
      this.proc.stdin?.write(`${payload}\n`);
    });
  }

  async notify(method: string, params: Record<string, unknown>): Promise<void> {
    if (this.closed) return;
    this.proc.stdin?.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.rejectAll(new Error('MCP stdio transport closed'));
    try {
      this.proc.stdin?.end();
    } catch {
      // ignore
    }
    this.proc.kill();
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    for (;;) {
      const index = this.buffer.indexOf('\n');
      if (index < 0) break;
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (!line) continue;
      this.handleMessage(line);
    }
  }

  private handleMessage(line: string): void {
    let message: JsonRpcResponse;
    try {
      message = JSON.parse(line) as JsonRpcResponse;
    } catch {
      return; // servers may print non-JSON noise on stdout
    }
    const id = typeof message.id === 'number' ? message.id : Number(message.id);
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    clearTimeout(pending.timer);
    if (message.error) {
      pending.reject(new Error(message.error.message ?? `MCP error ${message.error.code ?? ''}`));
    } else {
      pending.resolve(message.result);
    }
  }

  private rejectAll(error: Error): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
      this.pending.delete(id);
    }
  }

  private stderrText(): string {
    return this.stderrLines.join('').trim().split('\n').slice(-3).join(' | ').slice(0, 400);
  }
}

class HttpTransport implements JsonRpcTransport {
  private nextId = 1;
  private sessionId: string | null = null;

  constructor(private readonly config: MCPServerConfig) {}

  async request<T = unknown>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<T> {
    const result = await this.post({ jsonrpc: '2.0', id: this.nextId++, method, params }, timeoutMs);
    return result as T;
  }

  async notify(method: string, params: Record<string, unknown>): Promise<void> {
    await this.post({ jsonrpc: '2.0', method, params }, 15_000);
  }

  async close(): Promise<void> {
    this.sessionId = null;
  }

  private async post(
    payload: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<unknown> {
    const headers: Record<string, string> = {
      ...this.config.headers,
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
    };
    if (this.sessionId) headers['mcp-session-id'] = this.sessionId;

    const response = await fetch(this.config.url, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const sessionId = response.headers.get('mcp-session-id');
    if (sessionId) this.sessionId = sessionId;

    if (!response.ok) {
      throw new Error(`MCP HTTP ${response.status} ${response.statusText}`);
    }

    const contentType = response.headers.get('content-type') ?? '';
    const text = await response.text();
    if (!text.trim()) return undefined; // notifications often return 202/empty

    let message: JsonRpcResponse | undefined;
    if (contentType.includes('text/event-stream')) {
      message = parseSseResponse(text, (payload as { id?: number }).id);
    } else {
      const parsed = JSON.parse(text) as JsonRpcResponse | JsonRpcResponse[];
      message = Array.isArray(parsed)
        ? parsed.find((item) => item.id === payload.id)
        : parsed;
    }
    if (!message) throw new Error('MCP HTTP response did not contain a matching JSON-RPC message');
    if (message.error) throw new Error(message.error.message ?? 'MCP HTTP error');
    return message.result;
  }
}

export function parseSseResponse(
  body: string,
  expectedId: unknown,
): JsonRpcResponse | undefined {
  const messages: JsonRpcResponse[] = [];
  for (const block of body.split(/\n\n/)) {
    const dataLines = block
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim());
    if (dataLines.length === 0) continue;
    try {
      messages.push(JSON.parse(dataLines.join('\n')) as JsonRpcResponse);
    } catch {
      // ignore malformed events
    }
  }
  return (
    messages.find((message) => message.id === expectedId) ??
    messages.find((message) => message.result !== undefined || message.error !== undefined)
  );
}

/**
 * Legacy HTTP+SSE transport (MCP 2024-11-05):
 *   GET <url>        → SSE stream, first event "endpoint" points at the POST URL
 *   POST <endpoint>  → JSON-RPC request; the response arrives on the SSE stream
 */
class SseTransport implements JsonRpcTransport {
  private readonly pending = new Map<number, PendingCall>();
  private readonly controller = new AbortController();
  private readonly endpointPromise: Promise<string>;
  private resolveEndpoint!: (url: string) => void;
  private rejectEndpoint!: (error: Error) => void;
  private buffer = '';
  private nextId = 1;
  private endpoint: string | null = null;
  private closed = false;

  constructor(private readonly config: MCPServerConfig) {
    this.endpointPromise = new Promise<string>((resolve, reject) => {
      this.resolveEndpoint = resolve;
      this.rejectEndpoint = reject;
    });
    this.endpointPromise.catch(() => undefined);
    void this.consumeStream();
  }

  async request<T = unknown>(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<T> {
    if (this.closed) throw new Error('MCP SSE transport is closed');
    const endpoint = await Promise.race([
      this.endpointPromise,
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('MCP SSE endpoint timed out')), timeoutMs),
      ),
    ]);
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve: (value) => resolve(value as T), reject, timer });
      void this.post(endpoint, { jsonrpc: '2.0', id, method, params }).catch((error) => {
        const pending = this.pending.get(id);
        if (!pending) return;
        this.pending.delete(id);
        clearTimeout(pending.timer);
        pending.reject(error instanceof Error ? error : new Error(String(error)));
      });
    });
  }

  async notify(method: string, params: Record<string, unknown>): Promise<void> {
    if (this.closed) return;
    const endpoint = await this.endpointPromise;
    await this.post(endpoint, { jsonrpc: '2.0', method, params });
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.controller.abort();
    this.rejectAll(new Error('MCP SSE transport closed'));
  }

  private async consumeStream(): Promise<void> {
    try {
      const response = await fetch(this.config.url, {
        headers: { ...this.config.headers, accept: 'text/event-stream' },
        signal: this.controller.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`MCP SSE HTTP ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        this.buffer += decoder.decode(value, { stream: true });
        this.drainEvents();
      }
      throw new Error('MCP SSE stream closed');
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.rejectEndpoint(new Error(`MCP SSE transport failed: ${message}`));
      this.rejectAll(new Error(`MCP SSE transport closed: ${message}`));
    }
  }

  private drainEvents(): void {
    this.buffer = this.buffer.replace(/\r\n/g, '\n');
    let index = this.buffer.indexOf('\n\n');
    while (index >= 0) {
      const block = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 2);
      this.handleEvent(block);
      index = this.buffer.indexOf('\n\n');
    }
  }

  private handleEvent(block: string): void {
    let event = 'message';
    const data: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) data.push(line.slice(5).trim());
    }
    const payload = data.join('\n');
    if (!payload) return;
    if (event === 'endpoint') {
      try {
        this.endpoint = new URL(payload, this.config.url).toString();
        this.resolveEndpoint(this.endpoint);
      } catch {
        this.rejectEndpoint(new Error(`Invalid MCP SSE endpoint: ${payload}`));
      }
      return;
    }
    let message: JsonRpcResponse;
    try {
      message = JSON.parse(payload) as JsonRpcResponse;
    } catch {
      return;
    }
    if (message.id === undefined || message.id === null) return;
    const pending = this.pending.get(Number(message.id));
    if (!pending) return;
    this.pending.delete(Number(message.id));
    clearTimeout(pending.timer);
    if (message.error) {
      pending.reject(new Error(message.error.message ?? 'MCP SSE error'));
    } else {
      pending.resolve(message.result);
    }
  }

  private async post(endpoint: string, body: Record<string, unknown>): Promise<void> {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        ...this.config.headers,
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
      },
      body: JSON.stringify(body),
      signal: this.controller.signal,
    });
    if (!response.ok) {
      throw new Error(`MCP SSE POST HTTP ${response.status}`);
    }
    await response.text().catch(() => undefined);
  }

  private rejectAll(error: Error): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
      this.pending.delete(id);
    }
  }
}

export function createTransport(config: MCPServerConfig): JsonRpcTransport {
  if (config.transport === 'stdio') return new StdioTransport(config);
  if (config.transport === 'sse') return new SseTransport(config);
  return new HttpTransport(config);
}

export type { StdioTransport, HttpTransport };