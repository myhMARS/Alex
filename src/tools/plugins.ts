/**
 * Tool plugin loader — TypeScript counterpart of alex/tools/plugin_loader.py.
 *
 * A plugin is a `.ts` / `.js` / `.mjs` file in a plugin directory exporting
 * `register(registry)` (named or default). Plugins are trusted local code,
 * exactly like the Python `~/.alex/plugins/*.py` convention.
 */

import { readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

import type { ToolRegistry } from './registry';

const PLUGIN_SUFFIXES = ['.ts', '.js', '.mjs', '.cjs'];

export interface PluginLoadError {
  file: string;
  error: string;
}

export interface PluginLoadResult {
  loaded: string[];
  errors: PluginLoadError[];
}

function isPluginFile(name: string): boolean {
  if (name.endsWith('.d.ts')) return false;
  return PLUGIN_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

export async function loadPlugins(
  registry: ToolRegistry,
  directories: string[],
): Promise<PluginLoadResult> {
  const result: PluginLoadResult = { loaded: [], errors: [] };

  for (const directory of directories) {
    let entries: string[];
    try {
      entries = await readdir(directory);
    } catch {
      continue; // missing plugin dir is normal
    }
    for (const entry of entries.sort()) {
      if (!isPluginFile(entry)) continue;
      const file = join(directory, entry);
      try {
        const module = (await import(pathToFileURL(file).href)) as {
          register?: (registry: ToolRegistry) => void | Promise<void>;
          default?: ((registry: ToolRegistry) => void | Promise<void>) | unknown;
        };
        const register =
          typeof module.register === 'function'
            ? module.register
            : typeof module.default === 'function'
              ? (module.default as (registry: ToolRegistry) => void | Promise<void>)
              : undefined;
        if (!register) {
          result.errors.push({ file, error: 'plugin does not export register(registry)' });
          continue;
        }
        await register(registry);
        result.loaded.push(file);
      } catch (error) {
        result.errors.push({
          file,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }

  return result;
}