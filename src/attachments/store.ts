/** Content-addressed attachment storage helpers. */

import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

export function attachmentStoreDir(
  env: Record<string, string | undefined> = process.env,
): string {
  return env.ALEX_ATTACHMENT_DIR ?? join(homedir(), '.alex', 'attachments');
}

/** Persist arbitrary file bytes and return a stable read-only path. */
export async function storeFileBytes(
  bytes: Uint8Array,
  name: string,
  env: Record<string, string | undefined> = process.env,
): Promise<string> {
  const digest = createHash('sha256').update(bytes).digest('hex').slice(0, 16);
  const safeName = name.replace(/[^\w.-]+/g, '_').slice(0, 80) || 'attachment';
  const dir = attachmentStoreDir(env);
  await mkdir(dir, { recursive: true });
  const target = join(dir, `${digest}-${safeName}`);
  await writeFile(target, bytes);
  return target;
}

export async function storeFilePath(
  inputPath: string,
  name: string,
  env: Record<string, string | undefined> = process.env,
): Promise<string> {
  return storeFileBytes(await readFile(inputPath), name, env);
}