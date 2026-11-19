import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { loadAttachment, buildUserContent, formatBytes } from '../src/attachments/loader';
import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { MemoryModule } from '../src/modules/memory/module';
import { GetToolCatalog } from '../src/kernel/contracts/tools';
import { UserTurnRequested } from '../src/kernel/contracts/chat';
import type { ChatModel, ChatRequest, ChatStreamEvent } from '../src/llm/client';

const tempDirs: string[] = [];
const previousAttachmentDir = process.env.ALEX_ATTACHMENT_DIR;

afterEach(async () => {
  if (previousAttachmentDir === undefined) delete process.env.ALEX_ATTACHMENT_DIR;
  else process.env.ALEX_ATTACHMENT_DIR = previousAttachmentDir;
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

async function tempDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'alex-ts-attach-'));
  tempDirs.push(dir);
  return dir;
}

describe('attachment loader', () => {
  test('normalizes images to WebP and dedupes by content hash', async () => {
    const dir = await tempDir();
    const path = join(dir, 'pixel.png');
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
      'base64',
    );
    await writeFile(path, png);
    const attachment = await loadAttachment(path);
    expect(attachment.kind).toBe('image');
    expect(attachment.name).toBe('pixel.png');
    expect(attachment.dataUrl).toStartWith('data:image/webp;base64,');
    expect(attachment.mime).toBe('image/webp');
    expect(attachment.path).toEndWith('.webp');
    expect(attachment.hash).toBeTruthy();
    expect(attachment.width).toBe(1);
    expect(attachment.height).toBe(1);

    const again = await loadAttachment(path);
    expect(again.path).toBe(attachment.path);
    expect(again.hash).toBe(attachment.hash);
  });

  test('loads text files and summarizes binary files', async () => {
    const dir = await tempDir();
    const textPath = join(dir, 'notes.md');
    await writeFile(textPath, '# Title\nbody');
    const text = await loadAttachment(textPath);
    expect(text.kind).toBe('file');
    expect(text.text).toBeUndefined();

    const binaryPath = join(dir, 'archive.bin');
    await writeFile(binaryPath, new Uint8Array([0, 1, 2]));
    const binary = await loadAttachment(binaryPath);
    expect(binary.kind).toBe('file');
    expect(binary.text).toBeUndefined();
    expect(binary.size).toBe(3);
  });

  test('builds multimodal user content', () => {
    const content = buildUserContent('look', [
      { kind: 'image', name: 'a.png', size: 4, dataUrl: 'data:image/png;base64,AAAA' },
      { kind: 'file', name: 'a.txt', size: 5, path: '/tmp/a.txt' },
    ]);
    expect(Array.isArray(content)).toBe(true);
    if (Array.isArray(content)) {
      expect(content[0]).toEqual({ type: 'text', text: 'look' });
      expect(content[1]?.type).toBe('image_url');
      expect(content[2]?.type).toBe('text');
      if (content[2]?.type === 'text') expect(content[2].text).toContain('attached-file');
    }
    expect(formatBytes(2048)).toBe('2.0 KB');
  });

  test('rejects missing paths and directories', async () => {
    const dir = await tempDir();
    await expect(loadAttachment(join(dir, 'missing.png'))).rejects.toThrow();
    await expect(loadAttachment(dir)).rejects.toThrow('不是文件');
  });
});

class CaptureModel implements ChatModel {
  readonly calls: ChatRequest[] = [];

  async *streamChat(request: ChatRequest): AsyncIterable<ChatStreamEvent> {
    this.calls.push({ ...request, messages: [...request.messages] });
    yield {
      type: 'done',
      result: { content: 'received', thinking: '', toolCalls: [], finishReason: 'stop' },
    };
  }
}

describe('agent multimodal turns', () => {
  test('passes image parts through to the model request', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const memory = new MemoryModule();
    const model = new CaptureModel();
    const agent = new AgentModule({ model, systemPrompt: 'SYS' });
    await memory.start(bus);
    await agent.start(bus);
    await bus.start();

    bus.publish(
      new UserTurnRequested({
        sessionId: 's1',
        userText: 'look',
        attachments: [
          {
            kind: 'image',
            name: 'pixel.png',
            size: 4,
            dataUrl: 'data:image/png;base64,AAAA',
          },
        ],
      }),
    );
    await agent.whenIdle();
    await bus.idle();

    const userMessage = model.calls[0]?.messages.at(-1);
    expect(Array.isArray(userMessage?.content)).toBe(true);
    if (Array.isArray(userMessage?.content)) {
      expect(userMessage.content[1]).toEqual({
        type: 'image_url',
        image_url: { url: 'data:image/png;base64,AAAA' },
      });
    }
    await bus.shutdown();
  });
});