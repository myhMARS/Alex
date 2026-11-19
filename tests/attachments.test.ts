import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { loadAttachment, buildUserContent, formatBytes } from '../src/attachments/loader';
import { resolveAttachmentRef } from '../src/attachments/images';
import { AsyncEventBus } from '../src/kernel/bus';
import { AgentModule } from '../src/modules/agent/module';
import { StoreModule } from '../src/modules/store/module';
import { SessionModule } from '../src/session/module';
import { TuiModule } from '../src/modules/tui/module';
import { GetToolCatalog } from '../src/kernel/contracts/tools';
import {
  TokenUsageReported,
  TurnCompleted,
  TurnStarted,
  UserTurnRequested,
} from '../src/kernel/contracts/chat';
import {
  CompactionStarted,
  ContextCompacted,
} from '../src/kernel/contracts/compaction';
import { GetSessionProjection } from '../src/kernel/contracts/session';
import { ForkSession } from '../src/kernel/contracts/session';
import { assistantMessage } from '../src/kernel/dto/message';
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

  test('builds and caches a smaller request-time image variant', async () => {
    const dir = await tempDir();
    const path = join(dir, 'grid.png');
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAABdElEQVR4nOXDi0ccAADH8W91u1W7Xtdjtx5XV7dq1+tW7VbtekjlrKQmNVmTSiqnclbOlogxxhhjjDEiYkRERH/brz/k9+EDIOsFhUVyTmEgKOcUBUvknEBxSM55VFoh5wRDYTnncXmtnFNcGZFzSsINck5pTVTOeVIXk3NCkbicU1bfIeeUNybknIpoj5xT2ZKUc6paB+SccDwl51S3D8s5NZ1pOac2MS7n1HVPyjlPe6flnEgyI+c865+Vc+oH5+WchtSinNM4tCTnNI2syDnR9Kqc0zy2Jue0TKzLObHJTTmndWpbzmmb2ZVz4pmsnPP87YGc0z6Xk3M65o/knM6FvJzz4t0XOSexdCrndC2fyTnd77/KOT2r3+Sc3g/f5Zy+jz/knOT6Tznn5cYvOad/67ecM7D9R84Z3Pkr57za+yfnpLLncs7r/Qs5Z+jwUs4Zzv2Xc0Y+Xck5b46v5Zx0/kbOGf18K+eMndzJOeOn93L+AA6ZwZYxKxbHAAAAAElFTkSuQmCC',
      'base64',
    );
    await writeFile(path, png);
    const attachment = await loadAttachment(path);
    const canonical = await resolveAttachmentRef(attachment.hash!);
    const variant = await resolveAttachmentRef(attachment.hash!, 32);
    expect(variant.startsWith('data:image/webp;base64,')).toBe(true);
    expect(variant).not.toBe(canonical);
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
  test('hydrates persisted image references before the model request', async () => {
    const dir = await tempDir();
    process.env.ALEX_ATTACHMENT_DIR = join(dir, 'attachments');
    const imagePath = join(dir, 'hydrate.png');
    await writeFile(
      imagePath,
      Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
        'base64',
      ),
    );
    const attachment = await loadAttachment(imagePath);
    const content = buildUserContent('look', [attachment]);
    expect(Array.isArray(content)).toBe(true);
    if (Array.isArray(content)) {
      expect(content[1]?.type).toBe('image_url');
      if (content[1]?.type === 'image_url') {
        expect(content[1].image_url.url).toStartWith('alex-attachment:');
      }
    }

    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const session = new SessionModule();
    const model = new CaptureModel();
    const agent = new AgentModule({ model, systemPrompt: 'SYS' });
    await session.start(bus);
    await agent.start(bus);
    await bus.start();
    bus.publish(
      new UserTurnRequested({ sessionId: 's1', userText: 'look', attachments: [attachment] }),
    );
    await agent.whenIdle();
    await bus.idle();
    const userMessage = model.calls[0]?.messages.at(-1);
    if (Array.isArray(userMessage?.content)) {
      const image = userMessage.content.find((part) => part.type === 'image_url');
      if (image?.type === 'image_url') {
        expect(image.image_url.url).toStartWith('data:image/webp;base64,');
      }
    }
    await bus.shutdown();
  });

  test('passes image parts through to the model request', async () => {
    const bus = new AsyncEventBus();
    bus.provide(GetToolCatalog, () => []);
    const session = new SessionModule();
    const model = new CaptureModel();
    const agent = new AgentModule({ model, systemPrompt: 'SYS' });
    await session.start(bus);
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

describe('attachment resume', () => {
  test('restores image chips and image content after resume', async () => {
    const dir = await tempDir();
    process.env.ALEX_ATTACHMENT_DIR = join(dir, 'attachments');
    const attachment = {
      kind: 'image' as const,
      name: 'pixel.png',
      size: 4,
      dataUrl: 'data:image/png;base64,AAAA',
    };
    const sessionDir = join(dir, 'sessions');

    const bus1 = new AsyncEventBus();
    const session1 = new SessionModule();
    const store1 = new StoreModule({ sessionsDir: sessionDir });
    const tui1 = new TuiModule();
    await session1.start(bus1);
    await store1.start(bus1);
    await tui1.start(bus1);
    await bus1.start();
    bus1.publish(
      new TurnStarted({
        sessionId: 's1',
        turnId: 't1',
        userInput: 'look',
        attachments: [attachment],
      }),
    );
    bus1.publish(
      new TokenUsageReported({
        sessionId: 's1',
        turnId: 't1',
        promptTokens: 100,
        completionTokens: 10,
        totalTokens: 110,
        cacheHitTokens: 60,
        cacheMissTokens: 40,
      }),
    );
    bus1.publish(
      new CompactionStarted({
        sessionId: 's1',
        id: 'c1',
        reason: 'manual',
        thresholdTokens: 80,
      }),
    );
    bus1.publish(
      new ContextCompacted({
        sessionId: 's1',
        result: {
          id: 'c1',
          reason: 'manual',
          compactedMessages: 2,
          keptMessages: 1,
          freedTokens: 90,
          summary: 'earlier',
          thresholdTokens: 80,
          skipped: false,
        },
      }),
    );
    bus1.publish(
      new TurnCompleted({
        sessionId: 's1',
        turnId: 't1',
        content: 'ok',
        messages: [
          {
            role: 'user',
            content: [
              { type: 'text', text: 'look' },
              { type: 'image_url', image_url: { url: attachment.dataUrl } },
            ],
            attachments: [attachment],
          },
          assistantMessage('ok'),
        ],
      }),
    );
    await bus1.idle();
    await bus1.shutdown();

    const bus2 = new AsyncEventBus();
    const session2 = new SessionModule();
    const store2 = new StoreModule({ sessionsDir: sessionDir });
    const tui2 = new TuiModule();
    await session2.start(bus2);
    await store2.start(bus2);
    await tui2.start(bus2);
    await bus2.start();
    expect(await tui2.loadSession('s1')).toBe(true);

    expect(tui2.viewState.turns[0]?.attachments[0]?.name).toBe('pixel.png');
    expect(tui2.viewState.sessionUsage.inputTokens).toBe(100);
    expect(
      tui2.viewState.turns.some((turn) => turn.kind === 'compaction'),
    ).toBe(true);
    const context = (
      await bus2.request(new GetSessionProjection({ sessionId: 's1' }))
    ).messages;
    const userMessage = context.find((message) => message.role === 'user');
    expect(Array.isArray(userMessage?.content)).toBe(true);

    const fork = await bus2.request(
      new ForkSession({ sourceSessionId: 's1', targetSessionId: 's1-fork' }),
    );
    expect(fork?.sessionId).toBe('s1-fork');
    expect(fork?.messages.length).toBe(context.length);
    expect(fork?.formatVersion).toBe(3);
    await bus2.shutdown();
  });
});

describe('session corruption', () => {
  test('reports a corrupted session instead of silently dropping it', async () => {
    const dir = await tempDir();
    const sessionsDir = join(dir, 'sessions');
    await import('node:fs/promises').then(({ mkdir }) => mkdir(sessionsDir, { recursive: true }));
    await writeFile(join(sessionsDir, 'bad.jsonl'), '{not json', 'utf8');
    const bus = new AsyncEventBus();
    const store = new StoreModule({ sessionsDir });
    const tui = new TuiModule();
    await store.start(bus);
    await tui.start(bus);
    await bus.start();
    expect(await tui.loadSession('bad')).toBe(false);
    expect(tui.getSnapshot().toast ?? '').toContain('会话文件损坏');
    await bus.shutdown();
  });
});