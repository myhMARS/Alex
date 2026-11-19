import { afterEach, describe, expect, test } from 'bun:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { AsyncEventBus } from '../src/kernel/bus';
import { AgentRegistryModule } from '../src/agents/module';
import { AgentModule } from '../src/modules/agent/module';
import { AgentMailbox } from '../src/agents/mailbox';
import { SessionModule } from '../src/session/module';
import {
  AckAgentMessages,
  GetAgentMailboxStatus,
  ListAgentMessages,
  SendAgentMessage,
  SpawnAgent,
} from '../src/kernel/contracts/agents';

const dirs: string[] = [];

afterEach(async () => {
  while (dirs.length > 0) {
    const dir = dirs.pop();
    if (dir) await rm(dir, { recursive: true, force: true });
  }
});

describe('agent mailbox persistence', () => {
  test('persists messages for an agent and restores them on restart', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-mailbox-'));
    dirs.push(dir);
    const mailboxPath = join(dir, 'mailbox.jsonl');

    const firstBus = new AsyncEventBus();
    const firstSession = new SessionModule();
    const first = new AgentRegistryModule({
      registerTools: false,
      mailboxPath,
      autoAckMailbox: false,
    });
    await firstSession.start(firstBus);
    await first.start(firstBus);
    await firstBus.start();
    await firstBus.request(
      new SpawnAgent({ spec: { agentId: 'worker', teamId: 'team-1' } }),
    );
    await firstBus.request(
      new SendAgentMessage({
        fromAgentId: 'main',
        toAgentId: 'worker',
        content: 'persisted hello',
      }),
    );
    const live = await firstBus.request(
      new ListAgentMessages({ agentId: 'worker' }),
    );
    expect(live).toHaveLength(1);
    expect(live[0]?.content).toBe('persisted hello');
    expect(live[0]?.teamId).toBe('team-1');
    expect(live[0]?.read).toBe(false);
    expect(
      await firstBus.request(
        new ListAgentMessages({ agentId: 'worker', unreadOnly: true }),
      ),
    ).toHaveLength(1);
    expect(
      (
        await firstBus.request(
          new AckAgentMessages({
            agentId: 'worker',
            messageIds: [live[0]!.messageId],
          }),
        )
      ).acked,
    ).toBe(1);
    expect(
      await firstBus.request(
        new ListAgentMessages({ agentId: 'worker', unreadOnly: true }),
      ),
    ).toHaveLength(0);
    expect(
      (await firstBus.request(new ListAgentMessages({ agentId: 'worker' })))[0]?.read,
    ).toBe(true);
    const status = await firstBus.request(
      new GetAgentMailboxStatus({ agentId: 'worker' }),
    );
    expect(status.total).toBe(1);
    expect(status.read).toBe(1);
    expect(status.unread).toBe(0);
    expect(status.lastReadMessageId).toBe(live[0]!.messageId);
    expect(status.lastReadAt).toBeTruthy();
    await first.stop();
    await firstBus.shutdown();

    const secondBus = new AsyncEventBus();
    const secondSession = new SessionModule();
    const second = new AgentRegistryModule({
      registerTools: false,
      mailboxPath,
      autoAckMailbox: false,
    });
    await secondSession.start(secondBus);
    await second.start(secondBus);
    await secondBus.start();
    const restored = await secondBus.request(
      new ListAgentMessages({ agentId: 'worker' }),
    );
    expect(restored).toHaveLength(1);
    expect(restored[0]?.content).toBe('persisted hello');
    expect(restored[0]?.read).toBe(true);
    const restoredStatus = await secondBus.request(
      new GetAgentMailboxStatus({ agentId: 'worker' }),
    );
    expect(restoredStatus.lastReadMessageId).toBe(restored[0]?.messageId);
    expect(restoredStatus.lastReadAt).toBeTruthy();
    expect(
      await secondBus.request(
        new ListAgentMessages({ agentId: 'worker', unreadOnly: true }),
      ),
    ).toHaveLength(0);
    await second.stop();
    await secondBus.shutdown();
  });

  test('tracks replay attempts and dead letters', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-mailbox-dead-'));
    dirs.push(dir);
    const mailbox = new AgentMailbox(join(dir, 'mailbox.jsonl'));
    const entry = {
      messageId: 'm-dead',
      fromAgentId: 'main',
      toAgentId: 'worker',
      teamId: 'team-1',
      content: 'poison',
      at: new Date().toISOString(),
    };
    mailbox.record(entry);
    expect(mailbox.deadLetters('worker', 3)).toHaveLength(0);
    await mailbox.recordAttempt(entry.messageId);
    await mailbox.recordAttempt(entry.messageId);
    await mailbox.recordAttempt(entry.messageId);
    const dead = mailbox.deadLetters('worker', 3);
    expect(dead).toHaveLength(1);
    expect(dead[0]?.attempts).toBe(3);
    expect(mailbox.status('worker').dead).toBe(1);
    expect(await mailbox.revive([entry.messageId])).toBe(1);
    expect(mailbox.deadLetters('worker', 3)).toHaveLength(0);
    await mailbox.recordAttempt(entry.messageId);
    await mailbox.recordAttempt(entry.messageId);
    await mailbox.recordAttempt(entry.messageId);
    expect(mailbox.deadLetters('worker', 3)).toHaveLength(1);
    await mailbox.ack('worker', [entry.messageId]);
    expect(mailbox.deadLetters('worker', 3)).toHaveLength(0);
  });

  test('failed consumption counts retries and becomes a dead letter', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-mailbox-fail-'));
    dirs.push(dir);
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({
      registerTools: false,
      mailboxPath: join(dir, 'mailbox.jsonl'),
      mailboxMaxAttempts: 1,
      createAgent: (spec) =>
        new AgentModule({
          agentId: spec.agentId,
          parentAgentId: spec.parentAgentId,
          teamId: spec.teamId,
          acceptBroadcast: false,
          responder: {
            // eslint-disable-next-line require-yield
            async *respond() {
              throw new Error('worker failed');
            },
          },
        }),
    });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'worker', teamId: 'team-fail' } }),
    );
    await bus.request(
      new SendAgentMessage({
        fromAgentId: 'main',
        toAgentId: 'worker',
        content: 'poison message',
      }),
    );
    await registry.whenIdle();
    await bus.idle();
    const status = await bus.request(
      new GetAgentMailboxStatus({ agentId: 'worker' }),
    );
    expect(status.unread).toBe(1);
    expect(status.dead).toBe(1);
    const unread = await bus.request(
      new ListAgentMessages({ agentId: 'worker', unreadOnly: true }),
    );
    expect(unread).toHaveLength(1);
    expect(unread[0]?.attempts).toBeGreaterThanOrEqual(1);
    expect(unread[0]?.failureReason).toContain('worker failed');
    await registry.stop();
    await bus.shutdown();
  });

  test('auto-acks a message after the target agent processes it', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'alex-ts-mailbox-auto-'));
    dirs.push(dir);
    const bus = new AsyncEventBus();
    const session = new SessionModule();
    const registry = new AgentRegistryModule({
      registerTools: false,
      mailboxPath: join(dir, 'mailbox.jsonl'),
    });
    await session.start(bus);
    await registry.start(bus);
    await bus.start();
    await bus.request(
      new SpawnAgent({ spec: { agentId: 'worker', teamId: 'team-auto' } }),
    );
    await bus.request(
      new SendAgentMessage({
        fromAgentId: 'main',
        toAgentId: 'worker',
        content: 'auto ack me',
      }),
    );
    await registry.whenIdle();
    await bus.idle();
    const messages = await bus.request(
      new ListAgentMessages({ agentId: 'worker', unreadOnly: true }),
    );
    expect(messages).toHaveLength(0);
    const all = await bus.request(new ListAgentMessages({ agentId: 'worker' }));
    expect(all[0]?.read).toBe(true);
    const status = await bus.request(
      new GetAgentMailboxStatus({ agentId: 'worker' }),
    );
    expect(status.unread).toBe(0);
    expect(status.lastReadMessageId).toBe(all[0]?.messageId);
    await registry.stop();
    await bus.shutdown();
  });
});