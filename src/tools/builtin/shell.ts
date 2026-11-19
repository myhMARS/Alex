/** Built-in shell tools: bash / pwsh (port of alex/tools/shell.py, simplified). */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { stat } from 'node:fs/promises';

import type { Tool } from '../tool';
import { PERMISSION_SHELL } from '../permissions';
import { resolveInRoots } from '../path';

const execFileAsync = promisify(execFile);
const DEFAULT_TIMEOUT_SECONDS = 15;
const MAX_TIMEOUT_SECONDS = 120;
const MAX_OUTPUT = 32 * 1024;

const DENY_PATTERNS: RegExp[] = [
  /\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+\/(?:\s|$)/i,
  /\bmkfs(?:\.\w+)?\b/i,
  /\bdd\s+if=/i,
  /\bshutdown\b/i,
  /\breboot\b/i,
  /\bformat\s+[a-z]:/i,
  /\bdel\s+\/[fsq]\s+[a-z]:\\/i,
  />\s*\/dev\/(?:sd|nvme|hd)/i,
];

export function checkShellCommand(command: string): string | null {
  for (const pattern of DENY_PATTERNS) {
    if (pattern.test(command)) {
      return `Error: command refused by safety policy (${pattern.source})`;
    }
  }
  return null;
}

function truncate(text: string): string {
  return text.length <= MAX_OUTPUT ? text : `${text.slice(0, MAX_OUTPUT)}\n[Output truncated...]`;
}

function makeShellTool(options: {
  name: string;
  description: string;
  roots: string[];
  argv: (command: string) => string[];
}): Tool {
  return {
    name: options.name,
    description: options.description,
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Command to run' },
        cwd: { type: 'string', description: 'Working directory (optional)' },
        working_dir: { type: 'string', description: 'Alias for cwd' },
        timeout_seconds: {
          type: 'integer',
          description: 'Wall-clock timeout in seconds (default 15, max 120)',
        },
      },
      required: ['command'],
    },
    metadata: {
      requiredPermission: PERMISSION_SHELL,
      summarise: (args) =>
        `${options.name}: ${String(args.command ?? '')} (cwd: ${String(args.cwd ?? args.working_dir ?? '') || '(default)'}, timeout: ${String(args.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS)}s)`,
    },
    async invoke(args) {
      const command = typeof args.command === 'string' ? args.command : '';
      if (!command.trim()) return 'Error: command is required';
      const denied = checkShellCommand(command);
      if (denied) return denied;

      try {
        const rawCwd = typeof args.cwd === 'string' ? args.cwd : typeof args.working_dir === 'string' ? args.working_dir : undefined;
        const cwd = resolveInRoots(rawCwd, options.roots, {
          defaultToCwd: true,
          label: 'cwd',
        });
        const info = await stat(cwd).catch(() => null);
        if (!info || !info.isDirectory()) return `Error: cwd '${cwd}' is not a directory`;
        const timeoutSeconds = Math.min(
          Math.max(1, Number.parseInt(String(args.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS), 10) || DEFAULT_TIMEOUT_SECONDS),
          MAX_TIMEOUT_SECONDS,
        );
        const timeoutMs = timeoutSeconds * 1000;

        const argv = options.argv(command);
        const [file, ...rest] = argv;
        if (!file) return 'Error: empty shell argv';
        try {
          const { stdout, stderr } = await execFileAsync(file, rest, {
            cwd,
            timeout: timeoutMs,
            maxBuffer: 8 * MAX_OUTPUT,
          });
          return [
            `$ ${command}`,
            `cwd: ${cwd}`,
            `timeout: ${timeoutSeconds}s`,
            'exit_code: 0',
            '--- stdout ---',
            truncate(stdout),
            '--- stderr ---',
            truncate(stderr),
          ].join('\n');
        } catch (error) {
          const detail = error as { code?: number | string; stdout?: string; stderr?: string; message?: string; killed?: boolean };
          const timedOut = detail.killed === true || detail.code === 'ETIMEDOUT';
          return [
            `$ ${command}`,
            `cwd: ${cwd}`,
            `timeout: ${timeoutSeconds}s`,
            timedOut ? `Error: command timed out after ${timeoutSeconds}s` : '',
            `exit_code: ${String(detail.code ?? 'unknown')}`,
            '--- stdout ---',
            truncate(detail.stdout ?? ''),
            '--- stderr ---',
            truncate(detail.stderr ?? detail.message ?? ''),
          ].join('\n');
        }
      } catch (error) {
        return `Error: ${error instanceof Error ? error.message : String(error)}`;
      }
    },
  };
}

export function createBashTool(roots: string[]): Tool {
  return makeShellTool({
    name: 'bash',
    description:
      'Run a shell command with bash. Destructive commands are refused; output is capped.',
    roots,
    argv: (command) => ['bash', '-lc', command],
  });
}

export function createPwshTool(roots: string[]): Tool {
  return makeShellTool({
    name: 'pwsh',
    description:
      'Run a PowerShell command with pwsh. Destructive commands are refused; output is capped.',
    roots,
    argv: (command) => [
      'pwsh',
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      command,
    ],
  });
}

export function createBashWslTool(roots: string[]): Tool {
  return makeShellTool({
    name: 'bash_wsl',
    description:
      'Run a shell command inside WSL bash (Windows). Destructive commands are refused; output is capped.',
    roots,
    argv: (command) => ['wsl.exe', '-e', 'bash', '-lc', command],
  });
}
