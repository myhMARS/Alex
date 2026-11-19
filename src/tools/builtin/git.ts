/** Built-in git_inspect tool — read-only status / diff / log (port of alex/tools/git.py). */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { stat } from 'node:fs/promises';
import { dirname } from 'node:path';

import type { Tool } from '../tool';
import { PERMISSION_READ } from '../permissions';
import { resolveInRoots } from '../path';

const run = promisify(execFile);
const MAX_OUTPUT = 32 * 1024;
const TIMEOUT_MS = 10_000;

type GitAction = 'status' | 'diff' | 'log';

function buildArgv(action: GitAction, maxCount: number, scope?: string): string[] {
  if (action === 'status') return ['git', 'status', '--short', '--branch'];
  if (action === 'diff') {
    const argv = ['git', 'diff', '--no-color'];
    if (scope) argv.push('--', scope);
    return argv;
  }
  return [
    'git',
    'log',
    `--max-count=${maxCount}`,
    '--no-color',
    '--pretty=format:%h %ad %an %s',
    '--date=short',
  ];
}

function truncate(text: string): string {
  return text.length <= MAX_OUTPUT ? text : `${text.slice(0, MAX_OUTPUT)}\n\n[Output truncated...]`;
}

export function createGitInspectTool(roots: string[]): Tool {
  return {
    name: 'git_inspect',
    description:
      'Inspect a git repository read-only: action="status" lists changes, ' +
      '"diff" shows pending edits, "log" shows recent commits. Mutations are not supported.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['status', 'diff', 'log'],
          description: 'Inspection to run (default: status)',
        },
        path: { type: 'string', description: 'Optional path to scope the command to' },
        max_count: { type: 'integer', description: 'Max log entries (default: 20)' },
      },
      required: ['action'],
    },
    metadata: {
      requiredPermission: PERMISSION_READ,
      summarise: (args) => `git_inspect ${String(args.action ?? 'status')}`,
    },
    async invoke(args) {
      try {
        const action = (typeof args.action === 'string' ? args.action : 'status') as GitAction;
        if (!['status', 'diff', 'log'].includes(action)) {
          return `Error: unsupported action: ${action}`;
        }
        const target = resolveInRoots(
          typeof args.path === 'string' ? args.path : undefined,
          roots,
          { defaultToCwd: true, label: 'path' },
        );
        const scope = action === 'diff' && args.path ? target : undefined;
        const maxCount =
          typeof args.max_count === 'number' && args.max_count > 0
            ? Math.min(Math.floor(args.max_count), 200)
            : 20;

        const info = await stat(target).catch(() => null);
        const cwd = info && info.isDirectory() ? target : dirname(target);
        const argv = buildArgv(action, maxCount, scope);
        const git = argv[0];
        const rest = argv.slice(1);
        if (!git) return 'Error: empty git argv';
        try {
          const { stdout, stderr } = await run(git, rest, {
            cwd,
            timeout: TIMEOUT_MS,
            maxBuffer: 4 * MAX_OUTPUT,
          });
          return [
            `action: ${action}`,
            `cwd: ${target}`,
            'exit_code: 0',
            '--- stdout ---',
            truncate(stdout),
            '--- stderr ---',
            truncate(stderr),
          ].join('\n');
        } catch (error) {
          const detail = error as { code?: number; stderr?: string; message?: string };
          return `Error: git ${action} failed (${detail.code ?? 'unknown'}): ${(detail.stderr ?? detail.message ?? '').trim()}`;
        }
      } catch (error) {
        return `Error: ${error instanceof Error ? error.message : String(error)}`;
      }
    },
  };
}