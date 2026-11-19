/** Tool model — TypeScript port of alex/tools/models.py. */

import type { ToolExecutionContext } from '../kernel/dto/tool';

export type Permission = 'read' | 'write' | 'shell' | 'network' | 'danger' | (string & {});

export interface ToolMetadata {
  requiredPermission?: Permission;
  /** Human-readable summary shown in the approval modal. */
  summarise?: (args: Record<string, unknown>) => string;
  [key: string]: unknown;
}

export interface Tool {
  name: string;
  description: string;
  /** JSON Schema for the tool input (OpenAI function-calling format). */
  parameters: Record<string, unknown>;
  metadata?: ToolMetadata;
  invoke(args: Record<string, unknown>, ctx: ToolExecutionContext): Promise<string>;
}

export type ToolFactory = () => Tool;

export function defineTool(tool: Tool): Tool {
  return tool;
}