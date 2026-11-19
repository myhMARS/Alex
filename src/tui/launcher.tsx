/** Launches the OpenTUI renderer for the in-process TuiModule. */

import React from 'react';
import { createCliRenderer } from '@opentui/core';
import { createRoot } from '@opentui/react';

import type { TuiModule } from '../modules/tui/module';
import { TuiApp } from './app';

export interface LaunchTuiOptions {
  tui: TuiModule;
  sessionId: string;
  toolCount?: number;
}

/**
 * Mount the TUI and resolve when the user quits. The renderer owns the terminal
 * (alternate screen); on quit we destroy it so the shell is restored.
 */
export async function launchTui(options: LaunchTuiOptions): Promise<void> {
  options.tui.setSessionId(options.sessionId);
  // useMouse enables wheel/click reporting so the chat scrollbox can scroll.
  const renderer = await createCliRenderer({ exitOnCtrlC: false, useMouse: true });
  let finished = false;

  await new Promise<void>((resolve) => {
    const shutdown = () => {
      if (finished) return;
      finished = true;
      try {
        renderer.destroy();
      } catch {
        // best effort — terminal state matters more than the error
      }
      // Give the renderer a tick to restore the terminal before resolving.
      setTimeout(resolve, 50);
    };

    process.on('SIGINT', shutdown);
    process.on('SIGTERM', shutdown);

    // Smoke-test hook: exercise the real renderer/shutdown path without a TTY.
    const exitMs = Number(process.env.ALEX_TUI_EXIT_MS || 0);
    if (exitMs > 0) setTimeout(shutdown, exitMs);

    createRoot(renderer).render(
      <TuiApp
        tui={options.tui}
        sessionId={options.sessionId}
        toolCount={options.toolCount}
        onQuit={shutdown}
      />,
    );
  });
}