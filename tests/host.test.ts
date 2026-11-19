import { describe, expect, test } from 'bun:test';

import { AsyncEventBus } from '../src/kernel/bus';
import {
  CircularModuleDependency,
  ModuleHost,
  type Logger,
  type Module,
} from '../src/kernel/runtime';

const silentLogger: Logger = { info() {}, warn() {}, error() {} };

function makeModule(
  name: string,
  dependencies: string[],
  started: string[],
  options: { fail?: boolean; stopped?: string[] } = {},
): Module {
  return {
    name,
    dependencies,
    async start() {
      if (options.fail) throw new Error(`module ${name} failed`);
      started.push(name);
    },
    async stop() {
      options.stopped?.push(name);
    },
  };
}

describe('ModuleHost', () => {
  test('starts modules in dependency order', async () => {
    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, silentLogger);
    const started: string[] = [];

    host.register(makeModule('agent', ['memory', 'tools'], started));
    host.register(makeModule('tools', [], started));
    host.register(makeModule('memory', [], started));

    await host.startAll();

    expect(started).toEqual(['memory', 'tools', 'agent']);
    expect(host.startedModules.map((module) => module.name)).toEqual([
      'memory',
      'tools',
      'agent',
    ]);
  });

  test('starts TUI after the agent engine', async () => {
    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, silentLogger);
    const started: string[] = [];

    host.register(makeModule('tui', ['agent'], started));
    host.register(makeModule('agent', [], started));

    await host.startAll();
    expect(started).toEqual(['agent', 'tui']);
  });

  test('rolls back already-started modules when a later module fails', async () => {
    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, silentLogger);
    const started: string[] = [];
    const stopped: string[] = [];

    host.register(makeModule('a', [], started, { stopped }));
    host.register(makeModule('b', ['a'], started, { fail: true, stopped }));

    await expect(host.startAll()).rejects.toThrow('module b failed');
    expect(stopped).toEqual(['a']);
  });

  test('detects circular dependencies', async () => {
    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, silentLogger);
    const started: string[] = [];

    host.register(makeModule('a', ['b'], started));
    host.register(makeModule('b', ['a'], started));

    await expect(host.startAll()).rejects.toBeInstanceOf(CircularModuleDependency);
  });

  test('tolerates missing optional dependencies', async () => {
    const debugLogs: string[] = [];
    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, {
      debug(message) {
        debugLogs.push(message);
      },
      info() {},
      warn() {},
      error() {},
    });
    const started: string[] = [];

    host.register(makeModule('optional', ['not-registered'], started));
    await host.startAll();

    expect(started).toEqual(['optional']);
    expect(debugLogs.join('\n')).toContain('not-registered');
  });

  test('stopAll stops modules in reverse start order', async () => {
    const bus = new AsyncEventBus();
    const host = new ModuleHost(bus, silentLogger);
    const stopped: string[] = [];
    const started: string[] = [];

    host.register(makeModule('first', [], started, { stopped }));
    host.register(makeModule('second', ['first'], started, { stopped }));

    await host.startAll();
    await host.stopAll();

    expect(stopped).toEqual(['second', 'first']);
  });
});