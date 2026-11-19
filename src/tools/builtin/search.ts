/** Built-in search tools: glob / grep. */

import { readdir, readFile, stat } from 'node:fs/promises';
import { join, relative, resolve, sep } from 'node:path';

import type { Tool } from '../tool';
import { PERMISSION_READ } from '../permissions';
import { resolveInRoots } from '../path';

const IGNORED_DIRS = new Set([
  'node_modules',
  '.git',
  '.venv',
  '__pycache__',
  '.pytest_cache',
  'dist',
  'build',
]);
const MAX_RESULTS = 200;
const MAX_GREP_HEAD_LIMIT = 10_000;
const DEFAULT_GREP_HEAD_LIMIT = 250;
const MAX_FILE_BYTES = 1024 * 1024;

const TYPE_TO_EXTS: Record<string, string[]> = {
  py: ['.py'],
  js: ['.js', '.mjs', '.cjs', '.jsx'],
  ts: ['.ts', '.tsx'],
  json: ['.json'],
  md: ['.md', '.markdown'],
  yaml: ['.yaml', '.yml'],
  toml: ['.toml'],
  rust: ['.rs'],
  go: ['.go'],
  java: ['.java'],
  c: ['.c', '.h'],
  cpp: ['.cpp', '.cc', '.hpp'],
  sh: ['.sh', '.bash'],
};

function isIgnored(absolutePath: string, root: string): boolean {
  const rel = relative(root, absolutePath);
  return rel.split(sep).some((segment) => IGNORED_DIRS.has(segment));
}

async function* walkFiles(root: string, maxDepth = 20): AsyncGenerator<string> {
  async function* visit(directory: string, depth: number): AsyncGenerator<string> {
    if (depth > maxDepth) return;
    let entries;
    try {
      entries = await readdir(directory, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const full = join(directory, entry.name);
      if (entry.isDirectory()) {
        if (IGNORED_DIRS.has(entry.name)) continue;
        yield* visit(full, depth + 1);
      } else if (entry.isFile()) {
        yield full;
      }
    }
  }
  yield* visit(root, 0);
}

/** Minimal glob matcher for grep's `glob` filter (supports **, *, ?). */
export function matchGlob(relPath: string, pattern: string): boolean {
  const path = relPath.split(sep).join('/');
  let source = '';
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i];
    if (ch === '*') {
      if (pattern[i + 1] === '*') {
        i += 1;
        if (pattern[i + 1] === '/') {
          i += 1;
          source += '(?:.*/)?';
        } else {
          source += '.*';
        }
      } else {
        source += '[^/]*';
      }
    } else if (ch === '?') {
      source += '[^/]';
    } else {
      source += ch!.replace(/[.+^${}()|[\]\\]/g, '\\$&');
    }
  }
  try {
    return new RegExp(`^${source}$`).test(path);
  } catch {
    return true;
  }
}

export function createGlobTool(roots: string[]): Tool {
  return {
    name: 'glob',
    description:
      'Find files by name pattern (e.g. "**/*.ts"), sorted by modification time, ' +
      'newest first. Searches the given root (defaults to the first allowed root).',
    parameters: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Glob pattern, e.g. "**/*.py"' },
        path: { type: 'string', description: 'Directory to search (optional)' },
      },
      required: ['pattern'],
    },
    metadata: {
      requiredPermission: PERMISSION_READ,
      summarise: (args) => `glob ${String(args.pattern ?? '')}`,
    },
    async invoke(args) {
      try {
        const pattern = String(args.pattern ?? '').trim();
        if (!pattern) return 'Error: pattern is required';
        const root = resolveInRoots(
          typeof args.path === 'string' ? args.path : undefined,
          roots,
          { defaultToCwd: true, label: 'path' },
        );
        const glob = new Bun.Glob(pattern);
        const matches: { path: string; mtimeMs: number }[] = [];
        for await (const file of glob.scan({ cwd: root, dot: true, onlyFiles: true })) {
          const absolute = resolve(root, file);
          if (isIgnored(absolute, root)) continue;
          const info = await stat(absolute).catch(() => null);
          matches.push({ path: file, mtimeMs: info?.mtimeMs ?? 0 });
          if (matches.length >= MAX_RESULTS * 4) break;
        }
        matches.sort((a, b) => b.mtimeMs - a.mtimeMs);
        const listed = matches.slice(0, MAX_RESULTS);
        const header = `Found ${listed.length} file(s) matching ${pattern} in ${root} (sorted by mtime, newest first)`;
        return [header, ...listed.map((match) => match.path)].join('\n');
      } catch (error) {
        return `Error: ${error instanceof Error ? error.message : String(error)}`;
      }
    },
  };
}

function clampHeadLimit(value: unknown): number {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (!Number.isFinite(parsed) || parsed < 1) return DEFAULT_GREP_HEAD_LIMIT;
  return Math.min(parsed, MAX_GREP_HEAD_LIMIT);
}

export function createGrepTool(roots: string[]): Tool {
  return {
    name: 'grep',
    description:
      'Search file contents by regular expression (ripgrep-style options: glob, ' +
      'output_mode content/files_with_matches/count, ignore_case, context lines, ' +
      'head_limit, multiline, type).',
    parameters: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Regular expression' },
        path: { type: 'string', description: 'Directory or file to search (optional)' },
        glob: { type: 'string', description: 'Filename glob filter, e.g. "*.py"' },
        output_mode: {
          type: 'string',
          enum: ['content', 'files_with_matches', 'count'],
          description: 'Output shape (default: files_with_matches)',
        },
        ignore_case: { type: 'boolean', description: 'Case-insensitive match' },
        line_number: {
          type: 'boolean',
          description: 'Show line numbers in content mode (default true)',
        },
        after_context: { type: 'integer', description: 'Lines after each match' },
        before_context: { type: 'integer', description: 'Lines before each match' },
        context: { type: 'integer', description: 'Lines before and after each match' },
        head_limit: {
          type: 'integer',
          description: 'Max result entries (default 250, max 10000)',
        },
        multiline: { type: 'boolean', description: 'Match across line boundaries' },
        type: { type: 'string', description: 'File type alias, e.g. py, ts, js, json' },
      },
      required: ['pattern'],
    },
    metadata: {
      requiredPermission: PERMISSION_READ,
      summarise: (args) => `grep ${String(args.pattern ?? '')}`,
    },
    async invoke(args) {
      const pattern = String(args.pattern ?? '');
      if (!pattern) return 'Error: pattern is required';
      const modeRaw = String(args.output_mode ?? 'files_with_matches').toLowerCase();
      const mode =
        modeRaw === 'content' || modeRaw === 'count' || modeRaw === 'files_with_matches'
          ? modeRaw
          : 'files_with_matches';
      const ignoreCase = args.ignore_case === true;
      const multiline = args.multiline === true;
      const headLimit = clampHeadLimit(args.head_limit);
      const context = Number.parseInt(String(args.context ?? 0), 10) || 0;
      const before = Math.max(
        0,
        Number.parseInt(String(args.before_context ?? context), 10) || 0,
      );
      const after = Math.max(
        0,
        Number.parseInt(String(args.after_context ?? context), 10) || 0,
      );
      const showLineNumber = args.line_number === undefined ? true : args.line_number === true;
      const fileType = String(args.type ?? '').trim().toLowerCase();
      const typeExts = TYPE_TO_EXTS[fileType] ?? [];
      const globFilter = typeof args.glob === 'string' ? args.glob.trim() : '';

      let regex: RegExp;
      try {
        regex = new RegExp(pattern, `${ignoreCase ? 'i' : ''}m${multiline ? 's' : ''}`);
      } catch (error) {
        return `Error: invalid regex: ${error instanceof Error ? error.message : String(error)}`;
      }

      let root: string;
      try {
        root = resolveInRoots(
          typeof args.path === 'string' ? args.path : undefined,
          roots,
          { defaultToCwd: true, label: 'path' },
        );
      } catch (error) {
        return `Error: ${error instanceof Error ? error.message : String(error)}`;
      }

      const results: string[] = [];
      const fileMatches = new Set<string>();
      try {
        const rootInfo = await stat(root);
        const isFile = rootInfo.isFile();
        const candidates: string[] = [];
        if (isFile) {
          candidates.push(root);
        } else {
          for await (const file of walkFiles(root)) {
            const rel = relative(root, file);
            if (globFilter && !matchGlob(rel, globFilter)) continue;
            if (typeExts.length > 0 && !typeExts.some((ext) => file.toLowerCase().endsWith(ext))) {
              continue;
            }
            candidates.push(file);
          }
        }

        for (const file of candidates) {
          if (results.length >= headLimit) break;
          if (!isFile && isIgnored(file, root)) continue;
          const info = await stat(file).catch(() => null);
          if (!info || info.size > MAX_FILE_BYTES) continue;
          const buffer = await readFile(file).catch(() => null);
          if (!buffer || buffer.subarray(0, 4096).includes(0)) continue;
          const text = buffer.toString('utf8');
          const rel = isFile ? file : relative(root, file);
          const lines = text.split('\n');

          if (multiline) {
            const matcher = new RegExp(
              pattern,
              `g${ignoreCase ? 'i' : ''}m${multiline ? 's' : ''}`,
            );
            let match: RegExpExecArray | null;
            let count = 0;
            while ((match = matcher.exec(text)) !== null) {
              if (match[0].length === 0) {
                matcher.lastIndex += 1;
                continue;
              }
              count += 1;
              if (mode === 'content' && results.length < headLimit) {
                const line = text.slice(0, match.index).split('\n').length;
                const snippet = match[0].replace(/\s*\n\s*/g, ' ⏎ ').slice(0, 400);
                results.push(`${rel}:${line}: ${snippet}`);
              } else if (mode === 'files_with_matches') {
                fileMatches.add(rel);
                break;
              }
            }
            if (mode === 'count' && count > 0) results.push(`${rel}:${count}`);
            continue;
          }

          regex.lastIndex = 0;
          const matchLineNumbers: number[] = [];
          for (const [index, line] of lines.entries()) {
            regex.lastIndex = 0;
            if (regex.test(line)) matchLineNumbers.push(index);
          }
          if (matchLineNumbers.length === 0) continue;

          if (mode === 'files_with_matches') {
            fileMatches.add(rel);
            continue;
          }
          if (mode === 'count') {
            results.push(`${rel}:${matchLineNumbers.length}`);
            continue;
          }

          const wanted = new Map<number, { text: string; matched: boolean }>();
          for (const lineIndex of matchLineNumbers) {
            const start = Math.max(0, lineIndex - before);
            const end = Math.min(lines.length - 1, lineIndex + after);
            for (let i = start; i <= end; i += 1) {
              const existing = wanted.get(i);
              const isMatch = matchLineNumbers.includes(i);
              if (!existing || isMatch) {
                wanted.set(i, { text: lines[i] ?? '', matched: isMatch });
              }
            }
          }
          const ordered = [...wanted.entries()].sort((a, b) => a[0] - b[0]);
          for (const [lineIndex, entry] of ordered) {
            if (results.length >= headLimit) break;
            const separator = entry.matched ? ':' : '-';
            const prefix = showLineNumber
              ? `${rel}${separator}${lineIndex + 1}${separator}`
              : `${rel}${separator}`;
            results.push(`${prefix} ${entry.text.slice(0, 400)}`);
          }
        }

        if (mode === 'files_with_matches') {
          for (const file of fileMatches) {
            if (results.length >= headLimit) break;
            results.push(file);
          }
        }

        const header = [
          `Pattern: /${pattern}/`,
          `mode: ${mode}`,
          `scope: ${root}`,
          globFilter ? `glob: ${globFilter}` : '',
          fileType ? `type: ${fileType}` : '',
        ]
          .filter(Boolean)
          .join('  ');
        const body = results.length > 0 ? results.join('\n') : 'No matches.';
        return `${header}\n${body}`;
      } catch (error) {
        return `Error: ${error instanceof Error ? error.message : String(error)}`;
      }
    },
  };
}