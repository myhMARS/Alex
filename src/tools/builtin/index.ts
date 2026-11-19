/** Register the built-in tool set. */

import type { ToolRegistry } from '../registry';
import { createTimeTool } from './time';
import { createEditTool, createReadTool, createWriteTool } from './fs';
import { createGlobTool, createGrepTool } from './search';
import { createGitInspectTool } from './git';
import { createBashTool, createBashWslTool, createPwshTool } from './shell';
import { createWebFetchTool, createWebSearchTool } from './web';

export interface BuiltinToolsOptions {
  /** Allowed filesystem roots for path-scoped tools. */
  roots: string[];
}

export function registerBuiltinTools(
  registry: ToolRegistry,
  options: BuiltinToolsOptions,
): void {
  const { roots } = options;
  for (const tool of [
    createTimeTool(),
    createReadTool(roots),
    createWriteTool(roots),
    createEditTool(roots),
    createGlobTool(roots),
    createGrepTool(roots),
    createGitInspectTool(roots),
    createBashTool(roots),
    createPwshTool(roots),
    createBashWslTool(roots),
    createWebFetchTool(),
    createWebSearchTool(),
  ]) {
    registry.register(tool);
  }
}