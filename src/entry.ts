#!/usr/bin/env bun
/**
 * Alex TS entry point — composition root for the TypeScript rewrite.
 *
 * Every feature is a bus module; the TUI is one of them (no WebSocket bridge).
 *
 *   alex-ts --tui       interactive OpenTUI
 *   alex-ts --demo      headless scripted demo
 *   bun run src/entry.ts --help
 */

import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { CliError, VERSION, helpText, parseCliArgs } from './cli';
import { AsyncEventBus } from './kernel/bus';
import { ModuleHost } from './kernel/runtime';
import { AgentModule } from './modules/agent/module';
import { StoreModule } from './modules/store/module';
import { SessionModule } from './session/module';
import { TuiModule } from './modules/tui/module';
import { ToolsModule } from './tools/module';
import { MCPModule } from './mcp/module';
import { ChatClient } from './llm/client';
import { loadDotEnv, loadLLMConfig } from './llm/config';
import { loadSettings } from './settings/store';
import { attachmentStoreDir } from './attachments/store';
import { SettingsModule } from './settings/module';
import { PromptModule } from './prompts/module';
import { AgentRegistryModule } from './agents/module';
import { SkillModule } from './skill/module';
import { CronModule } from './cron/module';
import { launchTui } from './tui/launcher';
import {
  GetSessionProjection,
  ListSessions,
} from './kernel/contracts/session';
import { GetMCPStatus, GetToolCatalog } from './kernel/contracts/tools';

async function main(): Promise<void> {
  let options;
  try {
    options = parseCliArgs(process.argv.slice(2));
  } catch (error) {
    if (error instanceof CliError) {
      console.error(`Error: ${error.message}\n`);
      console.error(helpText());
      process.exitCode = 2;
      return;
    }
    throw error;
  }

  if (options.help) {
    console.log(helpText());
    return;
  }
  if (options.version) {
    console.log(`alex-ts ${VERSION}`);
    return;
  }

  const demo = options.mode === 'demo';
  const tuiMode =
    !demo && (options.mode === 'tui' || Boolean(process.stdin.isTTY));
  const sessionsDir = demo
    ? await mkdtemp(join(tmpdir(), 'alex-ts-demo-'))
    : undefined;

  const logger = {
    debug(message: string) {
      if (process.env.ALEX_DEBUG) console.debug(message);
    },
    info(message: string) {
      console.log(message);
    },
    warn(message: string) {
      console.warn(message);
    },
    error(message: string) {
      console.error(message);
    },
  };

  await loadDotEnv();
  const settingsEnv = demo ? { ...process.env } : process.env;
  if (demo) settingsEnv.ALEX_SETTINGS_PATH = join(sessionsDir!, 'settings.json');
  const settings = await loadSettings(settingsEnv);
  const llmConfig = loadLLMConfig(settingsEnv, {
    provider: settings.provider,
    baseUrl: settings.baseUrl,
    model: settings.model,
    apiKey: settings.apiKey,
    maxTokens: settings.maxOutputTokens,
  });
  const useModel = !demo && Boolean(llmConfig.apiKey);

  const model = useModel ? new ChatClient(llmConfig) : undefined;
  const bus = new AsyncEventBus();
  const host = new ModuleHost(bus, logger);
  const agent = new AgentModule({
    model,
    agentId: 'main',
    teamId: 'default',
    contextWindowTokens: settings.maxContextTokens,
    compactThresholdRatio: settings.compactThresholdRatio,
    imageMaxDimension: settings.imageMaxDimension,
    thinkingEnabled: settings.thinkingEnabled,
    effort: settings.effort,
    maxIterations: settings.maxIterations,
  });
  const tui = new TuiModule({
    thinkingEnabled: settings.thinkingEnabled,
    effort: settings.effort,
    model: settings.model,
    contextWindowTokens: settings.maxContextTokens,
  });

  host.register(
    new SettingsModule({
      settings,
      path: settingsEnv.ALEX_SETTINGS_PATH || undefined,
      env: settingsEnv,
    }),
  );
  host.register(new PromptModule());
  host.register(new SessionModule());
  host.register(new StoreModule(sessionsDir ? { sessionsDir } : {}));
  host.register(
    new ToolsModule({
      roots: [process.cwd(), attachmentStoreDir()],
      reviewer: model,
    }),
  );
  const registry = new AgentRegistryModule({
    model,
    defaultAgentId: 'main',
    defaultTeamId: 'default',
    persistTasks: true,
    persistMailbox: true,
    persistAgents: true,
    maxIterations: settings.maxIterations,
  });
  registry.attachAgent(agent, { agentId: 'main', role: 'lead', teamId: 'default' });
  host.register(registry);
  // In the demo we skip reading ~/.alex/mcp.json so the run stays offline.
  host.register(
    new MCPModule(!options.mcp || demo ? { configs: [] } : {}),
  );
  host.register(
    new CronModule(
      demo && sessionsDir ? { directory: join(sessionsDir, 'cron') } : {},
    ),
  );
  host.register(
    new SkillModule({
      model,
      ...(demo && sessionsDir ? { skillsPath: join(sessionsDir, 'skills.json') } : {}),
      autoReflect: settings.autoReflect,
      reflectionMinTools: settings.reflectionMinTools,
      reflectionMinWriteTools: settings.reflectionMinWriteTools,
    }),
  );
  host.register(agent);
  host.register(tui);

  await host.startAll();
  console.log(
    `modules started: ${host.startedModules.map((module) => module.name).join(' → ')}`,
  );
  console.log(`agent mode: ${useModel ? `llm:${llmConfig.model}` : 'echo (no ALEX_API_KEY)'}`);

  const sessionId =
    options.sessionId ??
    process.env.ALEX_SESSION_ID ??
    `session-${Date.now().toString(36)}`;

  try {
    if (tuiMode) {
      const toolCount = (await bus.request(new GetToolCatalog())).length;
      await launchTui({ tui, sessionId, toolCount });
    } else if (demo) {
      const demoSessionId = 'demo_session';
      tui.submitUserTurn('hello from the bus', demoSessionId);
      await agent.whenIdle();
      await bus.idle();

      const [turn] = tui.viewState.turns;
      console.log(`view: ${turn?.status} "${turn?.content}" (${turn?.kind})`);
      console.log(
        `history messages: ${(await bus.request(new GetSessionProjection({ sessionId: demoSessionId }))).messages.length}`,
      );
      console.log(`sessions persisted: ${(await bus.request(new ListSessions())).length}`);
      console.log(`tools in catalog: ${(await bus.request(new GetToolCatalog())).length}`);
      console.log(`mcp: ${(await bus.request(new GetMCPStatus())).statusMessage}`);
    } else {
      console.log('Tip: run with --tui for the interactive interface, or --demo for a scripted run.');
    }
  } finally {
    await host.stopAll();
    if (sessionsDir) await rm(sessionsDir, { recursive: true, force: true });
  }
}

await main();