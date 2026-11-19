/** Permission policy + audit log — TypeScript port of alex/tools/permissions.py (subset). */

import { appendFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';

import type { Permission } from './tool';

export const PERMISSION_READ: Permission = 'read';
export const PERMISSION_WRITE: Permission = 'write';
export const PERMISSION_SHELL: Permission = 'shell';
export const PERMISSION_NETWORK: Permission = 'network';
export const PERMISSION_DANGER: Permission = 'danger';
export const DEFAULT_ALLOWED = [PERMISSION_READ, PERMISSION_NETWORK];

export type PermissionDecision = 'allow' | 'deny' | 'ask';

export interface PermissionPolicyOptions {
  allowed?: Iterable<string>;
  denied?: Iterable<string>;
  env?: Record<string, string | undefined>;
}

function parseList(value: string | undefined): string[] {
  if (!value) return [];
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * Grants / denials for tool permissions.
 *
 * Resolution order matches Python: explicit deny → explicit allow → ask.
 */
export class PermissionPolicy {
  readonly allowed: Set<string>;
  readonly denied: Set<string>;

  constructor(options: PermissionPolicyOptions = {}) {
    const env = options.env ?? process.env;
    const allowed = options.allowed ?? DEFAULT_ALLOWED;
    this.allowed = new Set(allowed);
    this.denied = new Set(options.denied ?? parseList(env.ALEX_TOOL_DENY));
  }

  check(permission: string | undefined): PermissionDecision {
    if (!permission) return 'allow';
    if (this.denied.has(permission)) return 'deny';
    if (this.allowed.has(permission)) return 'allow';
    return 'ask';
  }

  remember(permission: string): void {
    this.allowed.add(permission);
  }

  revoke(permission: string): void {
    this.allowed.delete(permission);
  }
}

export interface AuditEntry {
  ts: string;
  tool: string;
  permission: string;
  decision: 'granted' | 'denied' | 'timeout';
  remember: boolean;
  preview?: string;
  sessionId?: string;
  agentId?: string;
}

export class AuditLogger {
  readonly path: string;

  constructor(path?: string) {
    this.path = path ?? join(homedir(), '.alex', 'audit', 'permissions.jsonl');
  }

  async write(entry: Omit<AuditEntry, 'ts'>): Promise<void> {
    const record: AuditEntry = { ts: new Date().toISOString(), ...entry };
    try {
      await mkdir(dirname(this.path), { recursive: true });
      await appendFile(this.path, `${JSON.stringify(record)}\n`, 'utf8');
    } catch {
      // Auditing must never break tool execution.
    }
  }
}