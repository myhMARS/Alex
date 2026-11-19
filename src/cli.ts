/** Minimal CLI argument parsing for the Alex TS entry point. */

export const VERSION = '0.4.1';

export type CliMode = 'auto' | 'tui' | 'demo';

export interface CliOptions {
  mode: CliMode;
  help: boolean;
  version: boolean;
  mcp: boolean;
  sessionId?: string;
}

export class CliError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CliError';
  }
}

export function parseCliArgs(argv: string[]): CliOptions {
  const options: CliOptions = {
    mode: 'auto',
    help: false,
    version: false,
    mcp: true,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const raw = argv[index]!;
    const [flag, inlineValue] = raw.includes('=') ? raw.split('=', 2) : [raw, undefined];

    switch (flag) {
      case '-h':
      case '--help':
        options.help = true;
        break;
      case '-v':
      case '--version':
        options.version = true;
        break;
      case '--tui':
        if (options.mode === 'demo') throw new CliError('--tui and --demo are mutually exclusive');
        options.mode = 'tui';
        break;
      case '--demo':
        if (options.mode === 'tui') throw new CliError('--tui and --demo are mutually exclusive');
        options.mode = 'demo';
        break;
      case '--no-mcp':
        options.mcp = false;
        break;
      case '--session': {
        const value = inlineValue ?? argv[index + 1];
        if (value === undefined || value.startsWith('-')) {
          throw new CliError('--session requires a value');
        }
        options.sessionId = value;
        if (inlineValue === undefined) index += 1;
        break;
      }
      default:
        if (raw.startsWith('-')) throw new CliError(`Unknown option: ${raw}`);
        throw new CliError(`Unexpected argument: ${raw}`);
    }
  }

  return options;
}

export function helpText(): string {
  return [
    `alex-ts ${VERSION} — terminal AI agent (TypeScript rewrite)`,
    '',
    'Usage: alex-ts [options]',
    '',
    'Options:',
    '  --tui               Start the interactive OpenTUI interface',
    '  --demo              Run the headless scripted demo, then exit',
    '  --session <id>      Use a specific session id',
    '  --no-mcp            Skip loading ~/.alex/mcp.json servers',
    '  -h, --help          Show this help',
    '  -v, --version       Show the version',
    '',
    'Environment:',
    '  ALEX_API_KEY        LLM API key; without it the agent runs in echo mode',
    '  ALEX_BASE_URL       OpenAI-compatible base URL (default https://api.deepseek.com)',
    '  ALEX_MODEL          Model name (default deepseek-chat)',
    '  ALEX_SESSION_ID     Default session id',
    '  ALEX_TUI_EXIT_MS    Auto-quit the TUI after N ms (smoke tests)',
  ].join('\n');
}