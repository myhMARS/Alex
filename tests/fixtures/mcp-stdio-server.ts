/**
 * Minimal MCP stdio server used by ts/tests/mcp.test.ts.
 *
 * Speaks newline-delimited JSON-RPC on stdin/stdout and exposes one `echo`
 * tool. Not part of the app; test fixture only.
 */

let buffer = '';

function send(message: unknown): void {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function handle(message: any): void {
  if (!message || typeof message !== 'object') return;
  const { id, method } = message;
  if (method === 'initialize') {
    send({
      jsonrpc: '2.0',
      id,
      result: {
        protocolVersion: '2025-03-26',
        capabilities: { tools: {} },
        serverInfo: { name: 'alex-ts-fixture', version: '1.0.0' },
      },
    });
    return;
  }
  if (method === 'notifications/initialized') return;
  if (method === 'tools/list') {
    send({
      jsonrpc: '2.0',
      id,
      result: {
        tools: [
          {
            name: 'echo',
            description: 'Echo the provided text back',
            inputSchema: {
              type: 'object',
              properties: { text: { type: 'string' } },
              required: ['text'],
            },
          },
        ],
      },
    });
    return;
  }
  if (method === 'tools/call') {
    const text = String(message.params?.arguments?.text ?? '');
    send({
      jsonrpc: '2.0',
      id,
      result: { content: [{ type: 'text', text: `echo:${text}` }], isError: false },
    });
    return;
  }
  if (id !== undefined) {
    send({ jsonrpc: '2.0', id, error: { code: -32601, message: 'method not found' } });
  }
}

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk: string) => {
  buffer += chunk;
  for (;;) {
    const index = buffer.indexOf('\n');
    if (index < 0) break;
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (!line) continue;
    try {
      handle(JSON.parse(line));
    } catch {
      // ignore malformed input
    }
  }
});