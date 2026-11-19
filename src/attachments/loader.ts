/** Load local images/text files into model-visible attachments. */

import { readFile, stat } from 'node:fs/promises';
import { basename, extname, resolve } from 'node:path';

import { attachmentStoreDir, storeFileBytes } from './store';
import { ATTACHMENT_URL_PREFIX, normalizeImage } from './images';

import type { AttachmentDTO, ContentPart } from '../kernel/dto/message';

const IMAGE_MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
};



export function isImagePath(path: string): boolean {
  return extname(path).toLowerCase() in IMAGE_MIME;
}

export async function loadAttachment(
  inputPath: string,
  forcedKind?: 'image' | 'file',
): Promise<AttachmentDTO> {
  const path = resolve(inputPath);
  const info = await stat(path);
  if (!info.isFile()) throw new Error(`不是文件：${inputPath}`);
  const name = basename(path);
  const extension = extname(path).toLowerCase();
  const image = forcedKind === 'image' || (forcedKind !== 'file' && isImagePath(path));

  if (image) {
    const normalized = await normalizeImage(await readFile(path), name);
    return {
      kind: 'image',
      name,
      path: normalized.path,
      mime: normalized.mime,
      size: normalized.bytes.length,
      dataUrl: normalized.dataUrl,
      hash: normalized.hash,
      width: normalized.width,
      height: normalized.height,
    };
  }

  const storedPath = await storeFileBytes(await readFile(path), name);
  return { kind: 'file', name, path: storedPath, size: info.size };
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** Build OpenAI-compatible user content from text plus attachments. */
export function buildUserContent(
  text: string,
  attachments: readonly AttachmentDTO[],
): string | ContentPart[] {
  if (attachments.length === 0) return text;
  const parts: ContentPart[] = [];
  if (text.trim()) parts.push({ type: 'text', text });
  for (const attachment of attachments) {
    if (attachment.kind === 'image') {
      const reference = attachment.hash
        ? `${ATTACHMENT_URL_PREFIX}${attachment.hash}`
        : attachment.dataUrl;
      if (reference) {
        parts.push({ type: 'image_url', image_url: { url: reference } });
        continue;
      }
    }
    parts.push({
      type: 'text',
      text: attachment.path
        ? `<attached-file name="${attachment.name}" bytes="${attachment.size}" path="${attachment.path}"/>\nUse the read tool to inspect this file.`
        : `[attached file: ${attachment.name}, ${formatBytes(attachment.size)}]`,
    });
  }
  if (parts.length === 0) parts.push({ type: 'text', text: text || '(empty)' });
  return parts;
}

/** Short label for chat rendering. */
export function attachmentLabel(
  attachment: Pick<AttachmentDTO, 'kind' | 'name' | 'size'>,
): string {
  const kind = attachment.kind === 'image' ? 'image' : 'file';
  return `[${kind}] ${attachment.name} · ${formatBytes(attachment.size)}`;
}
