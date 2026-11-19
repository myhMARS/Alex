/** Built-in filesystem tools: read / write / edit. */

import { readFileSync } from 'node:fs';
import { mkdir, readFile, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

import type { Tool } from '../tool';
import { PERMISSION_READ, PERMISSION_WRITE } from '../permissions';
import { resolveInRoots } from '../path';
import { unifiedDiff } from '../diff';

const DEFAULT_MAX_READ_BYTES = 256 * 1024;
const MAX_READ_BYTES_CAP = 4 * DEFAULT_MAX_READ_BYTES;
const BINARY_PROBE_BYTES = 4096;

function isBinary(buffer: Buffer): boolean {
  const probe = buffer.subarray(0, Math.min(buffer.length, BINARY_PROBE_BYTES));
  return probe.includes(0);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function normalizeEncoding(raw: unknown): string {
  const value = String(raw ?? 'utf-8').trim().toLowerCase() || 'utf-8';
  const aliases: Record<string, string> = {
    utf8: 'utf-8',
    'utf-8': 'utf-8',
    utf16le: 'utf-16le',
    'utf-16le': 'utf-16le',
    latin1: 'iso-8859-1',
    binary: 'iso-8859-1',
    ascii: 'us-ascii',
    'us-ascii': 'us-ascii',
  };
  return aliases[value] ?? value;
}

type DecoderEncoding = ConstructorParameters<typeof TextDecoder>[0];

function decodeContent(buffer: Buffer, encoding: string): string {
  return new TextDecoder(normalizeEncoding(encoding) as DecoderEncoding).decode(buffer);
}

function encodeContent(content: string, encoding: string): Buffer {
  const normalized = normalizeEncoding(encoding);
  const bufferEncoding: Record<string, BufferEncoding> = {
    'utf-8': 'utf8',
    'utf-16le': 'utf16le',
    'iso-8859-1': 'latin1',
    'us-ascii': 'ascii',
  };
  const target = bufferEncoding[normalized];
  if (!target) {
    throw new Error(`Unsupported encoding: ${encoding}`);
  }
  return Buffer.from(content, target);
}

/** Read a file for an approval preview; returns '' when unreadable/too large. */
function readForPreview(
  rawPath: string,
  roots: string[],
  encoding = 'utf-8',
): string {
  try {
    const path = resolveInRoots(rawPath, roots, { label: 'path' });
    const buffer = readFileSync(path);
    if (buffer.length > DEFAULT_MAX_READ_BYTES || isBinary(buffer)) return '';
    return decodeContent(buffer, encoding);
  } catch {
    return '';
  }
}

function toPositiveInt(value: unknown, fallback: number): number {
  const parsed =
    typeof value === 'number' ? Math.floor(value) : Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function filePathArg(args: Record<string, unknown>): string {
  return String(args.file_path ?? args.path ?? '');
}

export function createReadTool(roots: string[]): Tool {
  return {
    name: 'read',
    description:
      'Read a text file. Detects binary files; supports max_bytes/encoding and ' +
      'optional line offset/limit.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'File path to read' },
        max_bytes: {
          type: 'integer',
          description: 'Maximum number of bytes to read (default 256 KiB, max 1 MiB)',
        },
        encoding: { type: 'string', description: 'Text encoding (default utf-8)' },
        offset: { type: 'integer', description: '1-based first line (optional)' },
        limit: { type: 'integer', description: 'Max lines to return (optional)' },
      },
      required: ['path'],
    },
    metadata: {
      requiredPermission: PERMISSION_READ,
      summarise: (args) => `read ${String(args.path ?? '')}`,
    },
    async invoke(args) {
      try {
        const path = resolveInRoots(
          typeof args.path === 'string' ? args.path : undefined,
          roots,
          { label: 'path' },
        );
        const info = await stat(path);
        if (info.isDirectory()) return `Error: '${path}' is a directory`;
        const maxBytes = Math.min(
          toPositiveInt(args.max_bytes, DEFAULT_MAX_READ_BYTES),
          MAX_READ_BYTES_CAP,
        );
        const buffer = await readFile(path);
        if (isBinary(buffer)) return `Error: '${path}' looks like a binary file`;

        const truncated = buffer.length > maxBytes;
        const text = decodeContent(truncated ? buffer.subarray(0, maxBytes) : buffer, String(args.encoding ?? 'utf-8'));
        const lines = text.split('\n');
        const offset = toPositiveInt(args.offset, 1);
        const limit = toPositiveInt(args.limit, lines.length);
        const slice = lines.slice(offset - 1, offset - 1 + limit);
        const header = [
          `Path: ${path}`,
          `Lines: ${offset}-${offset + slice.length - 1} of ${lines.length}`,
          truncated ? `[Truncated: ${buffer.length} bytes > max_bytes=${maxBytes}]` : '',
        ]
          .filter(Boolean)
          .join('\n');
        return `${header}\n\n${slice.join('\n')}`;
      } catch (error) {
        return `Error: ${errorMessage(error)}`;
      }
    },
  };
}

export function createWriteTool(roots: string[]): Tool {
  return {
    name: 'write',
    description:
      'Write a full text file atomically. Requires the write permission; ' +
      'parent directories are not created unless create_dirs=true.',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'File path to write' },
        content: { type: 'string', description: 'Full file content' },
        encoding: { type: 'string', description: 'Text encoding (default utf-8)' },
        create_dirs: {
          type: 'boolean',
          description: 'Create missing parent directories (default false)',
        },
      },
      required: ['path', 'content'],
    },
    metadata: {
      requiredPermission: PERMISSION_WRITE,
      summarise: (args) => {
        const path = String(args.path ?? '');
        const content = typeof args.content === 'string' ? args.content : '';
        const existing = readForPreview(path, roots, String(args.encoding ?? 'utf-8'));
        return `write ${path}\n${unifiedDiff(existing, content, path)}`;
      },
    },
    async invoke(args) {
      try {
        const path = resolveInRoots(
          typeof args.path === 'string' ? args.path : undefined,
          roots,
          { label: 'path' },
        );
        const content = typeof args.content === 'string' ? args.content : String(args.content ?? '');
        const encoding = String(args.encoding ?? 'utf-8');
        const buffer = encodeContent(content, encoding);

        if (args.create_dirs === true) {
          await mkdir(dirname(path), { recursive: true });
        } else {
          const parent = await stat(dirname(path)).catch(() => null);
          if (!parent || !parent.isDirectory()) {
            return `Error: parent directory does not exist (set create_dirs=true): ${dirname(path)}`;
          }
        }

        const tmp = `${path}.${process.pid}.${Math.random().toString(36).slice(2, 8)}.tmp`;
        try {
          await writeFile(tmp, buffer);
          await rename(tmp, path);
        } catch (error) {
          await unlink(tmp).catch(() => undefined);
          throw error;
        }
        return `Wrote ${buffer.length} bytes to ${path}`;
      } catch (error) {
        return `Error: ${errorMessage(error)}`;
      }
    },
  };
}

export function createEditTool(roots: string[]): Tool {
  return {
    name: 'edit',
    description:
      'Replace an exact string in an existing file. old_string must occur once ' +
      'unless replace_all=true. Accepts file_path (or path).',
    parameters: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'File path to edit' },
        path: { type: 'string', description: 'Alias for file_path' },
        old_string: { type: 'string', description: 'Exact text to replace' },
        new_string: { type: 'string', description: 'Replacement text' },
        replace_all: { type: 'boolean', description: 'Replace every occurrence' },
        encoding: { type: 'string', description: 'Text encoding (default utf-8)' },
      },
      required: ['old_string', 'new_string'],
    },
    metadata: {
      requiredPermission: PERMISSION_WRITE,
      summarise: (args) => {
        const path = filePathArg(args);
        const oldText = String(args.old_string ?? '');
        const newText = String(args.new_string ?? '');
        const original = readForPreview(path, roots, String(args.encoding ?? 'utf-8'));
        const replaceAll = args.replace_all === true;
        const updated = original
          ? replaceAll
            ? original.split(oldText).join(newText)
            : original.replace(oldText, newText)
          : oldText
            ? `(current content unavailable)\n- ${oldText}\n+ ${newText}`
            : '';
        return `edit ${path}\n${unifiedDiff(original, updated, path)}`;
      },
    },
    async invoke(args) {
      try {
        const rawPath = filePathArg(args);
        if (!rawPath) return 'Error: file_path is required';
        const path = resolveInRoots(rawPath, roots, { label: 'file_path' });
        const oldText = typeof args.old_string === 'string' ? args.old_string : '';
        const newText = typeof args.new_string === 'string' ? args.new_string : '';
        if (!oldText) return 'Error: old_string must not be empty';
        const replaceAll = args.replace_all === true;
        const encoding = String(args.encoding ?? 'utf-8');
        const original = decodeContent(await readFile(path), encoding);
        const occurrences = original.split(oldText).length - 1;
        if (occurrences === 0) return `Error: old_string not found in ${path}`;
        if (occurrences > 1 && !replaceAll) {
          return `Error: old_string occurs ${occurrences} times in ${path}; pass replace_all=true or a unique snippet`;
        }
        const updated = replaceAll
          ? original.split(oldText).join(newText)
          : original.replace(oldText, newText);
        await writeFile(path, encodeContent(updated, encoding));
        return `Edited ${path}`;
      } catch (error) {
        return `Error: ${errorMessage(error)}`;
      }
    },
  };
}