/** Tool DTOs — mirror alex/kernel/dto/tool.py. */

export interface ToolExecutionContext {
  sessionId: string;
  turnId?: string;
  source?: 'user' | 'cron' | 'system';
  metadata?: Record<string, unknown>;
}

export interface ToolSpec {
  name: string;
  description: string;
  jsonSchema: Record<string, unknown>;
  provider?: 'builtin' | 'mcp' | 'plugin' | string;
  metadata?: Record<string, unknown>;
}

export interface ToolResult {
  name: string;
  output?: string;
  error?: string;
  runId?: string;
}

export function toolError(name: string, error: string, runId = ''): ToolResult {
  return { name, error, runId };
}