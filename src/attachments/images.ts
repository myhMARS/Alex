/**
 * Image normalization and content-addressed storage.
 *
 * Mirrors DSH's attachment pipeline in spirit: validate source dimensions,
 * normalize to a bounded raster, encode WebP quality ladder, then dedupe by
 * sha256 of the normalized bytes.
 */

import { createHash } from 'node:crypto';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import { attachmentStoreDir } from './store';

const MAX_SOURCE_BYTES = 50 * 1024 * 1024;
const MAX_SOURCE_PIXELS = 64_000_000;
const MAX_SOURCE_DIMENSION = 65_535;
const MAX_NORMALIZED_PIXELS = 2_048 * 2_048;
const MAX_NORMALIZED_DIMENSION = 8_192;
const TARGET_BYTES = 4 * 1024 * 1024;
const QUALITIES = [85, 75, 60];

interface BunImage {
  metadata(): Promise<{ width: number; height: number; format: string; orientation?: number }>;
  resize(width: number, height: number): Promise<BunImage>;
  webp(options: { quality: number }): BunImage | Promise<BunImage>;
  bytes(): Promise<Uint8Array>;
  width: number;
  height: number;
}

interface BunImageConstructor {
  new (data: Uint8Array): BunImage;
}

function imageConstructor(): BunImageConstructor {
  const ctor = (Bun as unknown as { Image?: BunImageConstructor }).Image;
  if (!ctor) throw new Error('当前 Bun 版本不支持图片处理（Bun.Image）');
  return ctor;
}

export interface NormalizedImage {
  hash: string;
  path: string;
  mime: 'image/webp';
  bytes: Uint8Array;
  dataUrl: string;
  width: number;
  height: number;
}

/** Fit within a total-pixel budget and a maximum long edge, never upscaling. */
export function fitImageDimensions(
  width: number,
  height: number,
  maxPixels = MAX_NORMALIZED_PIXELS,
  maxDimension = MAX_NORMALIZED_DIMENSION,
): { width: number; height: number } {
  const scale = Math.min(
    1,
    Math.sqrt(maxPixels / (width * height)),
    maxDimension / Math.max(width, height),
  );
  if (scale >= 1) return { width, height };
  let nextWidth = Math.max(1, Math.floor(width * scale));
  let nextHeight = Math.max(1, Math.round(nextWidth * (height / width)));
  while (nextWidth * nextHeight > maxPixels && nextWidth > 1) {
    nextWidth -= 1;
    nextHeight = Math.max(1, Math.round(nextWidth * (height / width)));
  }
  return { width: nextWidth, height: nextHeight };
}

async function encodeLadder(image: BunImage): Promise<{ bytes: Uint8Array; width: number; height: number }> {
  let smallest: { bytes: Uint8Array; width: number; height: number } | null = null;
  for (const quality of QUALITIES) {
    const encoded = await image.webp({ quality });
    const bytes = await encoded.bytes();
    const candidate = { bytes, width: encoded.width, height: encoded.height };
    if (smallest === null || bytes.length < smallest.bytes.length) smallest = candidate;
    if (bytes.length <= TARGET_BYTES) return candidate;
  }
  if (!smallest) throw new Error('图片编码失败');
  return smallest;
}

export async function normalizeImage(
  bytes: Uint8Array,
  name: string,
): Promise<NormalizedImage> {
  if (bytes.length > MAX_SOURCE_BYTES) {
    throw new Error(`图片过大（${(bytes.length / 1024 / 1024).toFixed(1)} MB，上限 50 MB）`);
  }
  const ImageCtor = imageConstructor();
  let image: BunImage;
  try {
    image = new ImageCtor(bytes);
  } catch {
    throw new Error('无法识别图片格式');
  }
  const meta = await image.metadata();
  const supported = ['png', 'jpeg', 'webp', 'gif'];
  if (!supported.includes(meta.format)) {
    throw new Error(`不支持的图片格式：${meta.format}`);
  }
  if (
    meta.width <= 0 ||
    meta.height <= 0 ||
    meta.width > MAX_SOURCE_DIMENSION ||
    meta.height > MAX_SOURCE_DIMENSION ||
    meta.width * meta.height > MAX_SOURCE_PIXELS
  ) {
    throw new Error(`图片尺寸超限：${meta.width}×${meta.height}`);
  }

  const target = fitImageDimensions(meta.width, meta.height);
  let processed = image;
  if (target.width !== meta.width || target.height !== meta.height) {
    processed = await image.resize(target.width, target.height);
  }
  const encoded = await encodeLadder(processed);
  const hash = createHash('sha256').update(encoded.bytes).digest('hex');
  const dir = join(attachmentStoreDir(), 'images');
  await mkdir(dir, { recursive: true });
  const path = join(dir, `${hash}.webp`);
  try {
    await access(path);
  } catch {
    await writeFile(path, encoded.bytes);
  }
  return {
    hash,
    path,
    mime: 'image/webp',
    bytes: encoded.bytes,
    dataUrl: `data:image/webp;base64,${Buffer.from(encoded.bytes).toString('base64')}`,
    width: encoded.width,
    height: encoded.height,
  };
}

/** Read a stored normalized image back as a data URL (for request projection). */
export async function normalizedImageDataUrl(path: string): Promise<string> {
  const bytes = await readFile(path);
  return `data:image/webp;base64,${bytes.toString('base64')}`;
}

export const ATTACHMENT_URL_PREFIX = 'alex-attachment:';

/** Resolve a persisted image reference to its request-time data URL. */
export async function resolveAttachmentRef(
  ref: string,
  maxDimension = 0,
): Promise<string> {
  const hash = ref.startsWith(ATTACHMENT_URL_PREFIX)
    ? ref.slice(ATTACHMENT_URL_PREFIX.length)
    : ref;
  if (!/^[a-f0-9]{64}$/i.test(hash)) throw new Error(`无效的图片引用：${ref}`);
  const dir = join(attachmentStoreDir(), 'images');
  const path = join(dir, `${hash}.webp`);
  let bytes: Uint8Array;
  try {
    bytes = await readFile(path);
    const actual = createHash('sha256').update(bytes).digest('hex');
    if (actual !== hash.toLowerCase()) throw new Error('digest mismatch');
  } catch {
    throw new Error(`图片附件不存在或已损坏：${hash.slice(0, 16)}`);
  }
  const canonical = `data:image/webp;base64,${Buffer.from(bytes).toString('base64')}`;
  if (!Number.isFinite(maxDimension) || maxDimension <= 0) return canonical;

  const variantPath = join(dir, `${hash}@${Math.floor(maxDimension)}.webp`);
  try {
    const cached = await readFile(variantPath);
    return `data:image/webp;base64,${cached.toString('base64')}`;
  } catch {
    // build the route-specific variant below
  }
  try {
    const ImageCtor = imageConstructor();
    const image = new ImageCtor(bytes);
    const meta = await image.metadata();
    const target = fitImageDimensions(
      meta.width,
      meta.height,
      Math.floor(maxDimension) * Math.floor(maxDimension),
      Math.floor(maxDimension),
    );
    if (target.width === meta.width && target.height === meta.height) {
      return canonical;
    }
    const encoded = await encodeLadder(
      target.width === meta.width && target.height === meta.height
        ? image
        : await image.resize(target.width, target.height),
    );
    await mkdir(dir, { recursive: true });
    await writeFile(variantPath, encoded.bytes);
    return `data:image/webp;base64,${Buffer.from(encoded.bytes).toString('base64')}`;
  } catch {
    // Best-effort: fall back to the canonical image when a variant cannot be built.
    return canonical;
  }
}