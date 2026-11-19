/** Path safety helpers shared by the built-in tools. */

import { dirname, resolve, sep } from 'node:path';

/**
 * Resolve a user-supplied path and ensure it stays under one of the roots.
 * Mirrors alex/tools/_path.py.
 */
export function resolveInRoots(
  raw: string | undefined,
  roots: string[],
  options: { defaultToCwd?: boolean; label?: string } = {},
): string {
  if (roots.length === 0) throw new Error('roots must not be empty');
  const label = options.label ?? 'path';
  const rootPaths = roots.map((root) => resolve(root));

  let candidate: string;
  if (!raw) {
    if (!options.defaultToCwd) throw new Error(`${label} is required`);
    candidate = rootPaths[0]!;
  } else {
    candidate = resolve(raw);
  }

  for (const root of rootPaths) {
    if (candidate === root || candidate.startsWith(root + sep)) return candidate;
  }
  throw new Error(`${label} '${raw ?? ''}' is outside the allowed roots`);
}

export function toRelative(root: string, target: string): string {
  const absoluteRoot = resolve(root);
  const absoluteTarget = resolve(target);
  if (absoluteTarget === absoluteRoot) return '.';
  if (absoluteTarget.startsWith(absoluteRoot + sep)) {
    return absoluteTarget.slice(absoluteRoot.length + 1);
  }
  return absoluteTarget;
}

export function parentDir(target: string): string {
  return dirname(target);
}