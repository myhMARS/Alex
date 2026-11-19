/** Module protocol and composition root — TypeScript port of kernel/runtime.py + host.py. */

import type { AsyncEventBus } from './bus';

export interface Logger {
  debug?(message: string): void;
  info?(message: string): void;
  warn?(message: string): void;
  error?(message: string): void;
}

/**
 * A pluggable business module.
 *
 * Implementations only receive the bus: they subscribe to events and provide
 * request handlers. They never import another module.
 */
export interface Module {
  readonly name: string;
  readonly dependencies?: readonly string[];
  start(bus: AsyncEventBus): void | Promise<void>;
  stop?(): void | Promise<void>;
}

export class CircularModuleDependency extends Error {
  readonly cycle: string[];

  constructor(cycle: string[]) {
    super(`Circular module dependency: ${cycle.join(' -> ')}`);
    this.name = 'CircularModuleDependency';
    this.cycle = cycle;
  }
}

/**
 * Composition root: registers modules and starts them in dependency order.
 * On a start failure, already-started modules are stopped in reverse order.
 */
export class ModuleHost {
  private readonly modules = new Map<string, Module>();
  private started: Module[] = [];

  constructor(
    private readonly bus: AsyncEventBus,
    private readonly logger: Logger = console,
  ) {}

  register(module: Module): void {
    if (!module.name) throw new Error('Module must declare a name');
    if (this.modules.has(module.name)) {
      throw new Error(`Module already registered: ${module.name}`);
    }
    this.modules.set(module.name, module);
  }

  get moduleNames(): string[] {
    return [...this.modules.keys()];
  }

  get startedModules(): Module[] {
    return [...this.started];
  }

  async startAll(): Promise<void> {
    await this.bus.start();
    const order = this.topologicalOrder();
    try {
      for (const module of order) {
        await module.start(this.bus);
        this.started.push(module);
      }
    } catch (error) {
      this.logger.error?.(`Module start failed, rolling back: ${String(error)}`);
      await this.stopAll().catch(() => undefined);
      throw error;
    }
  }

  async stopAll(): Promise<void> {
    for (const module of [...this.started].reverse()) {
      try {
        await module.stop?.();
      } catch (error) {
        this.logger.warn?.(`Error stopping module ${module.name}: ${String(error)}`);
      }
    }
    this.started = [];
    await this.bus.shutdown();
  }

  /** Dependency-first order with explicit cycle detection. */
  private topologicalOrder(): Module[] {
    const state = new Map<string, 'visiting' | 'done'>();
    const order: Module[] = [];
    const stack: string[] = [];

    const visit = (name: string): void => {
      const status = state.get(name);
      if (status === 'done') return;
      if (status === 'visiting') {
        const from = stack.indexOf(name);
        throw new CircularModuleDependency([...stack.slice(from), name]);
      }
      const module = this.modules.get(name);
      if (!module) {
        this.logger.debug?.(`Missing module dependency: ${name}`);
        return;
      }
      state.set(name, 'visiting');
      stack.push(name);
      for (const dependency of module.dependencies ?? []) {
        if (!this.modules.has(dependency)) {
          this.logger.debug?.(
            `Module '${name}' depends on '${dependency}' (not registered, skipping)`,
          );
          continue;
        }
        visit(dependency);
      }
      stack.pop();
      state.set(name, 'done');
      order.push(module);
    };

    for (const name of this.modules.keys()) visit(name);
    return order;
  }
}