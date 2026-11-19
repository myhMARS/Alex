import { describe, expect, test } from 'bun:test';

import { CliError, parseCliArgs } from '../src/cli';

describe('CLI parsing', () => {
  test('defaults to auto mode with MCP enabled', () => {
    const options = parseCliArgs([]);
    expect(options.mode).toBe('auto');
    expect(options.mcp).toBe(true);
    expect(options.help).toBe(false);
    expect(options.version).toBe(false);
  });

  test('parses --tui / --demo / --no-mcp', () => {
    expect(parseCliArgs(['--tui']).mode).toBe('tui');
    expect(parseCliArgs(['--demo']).mode).toBe('demo');
    expect(parseCliArgs(['--tui', '--no-mcp'])).toMatchObject({ mode: 'tui', mcp: false });
  });

  test('parses --session in both forms', () => {
    expect(parseCliArgs(['--session', 'abc123']).sessionId).toBe('abc123');
    expect(parseCliArgs(['--session=abc123']).sessionId).toBe('abc123');
    expect(parseCliArgs(['--tui', '--session', 's1']).mode).toBe('tui');
  });

  test('rejects invalid combinations and unknown options', () => {
    expect(() => parseCliArgs(['--tui', '--demo'])).toThrow(CliError);
    expect(() => parseCliArgs(['--session'])).toThrow(CliError);
    expect(() => parseCliArgs(['--nope'])).toThrow(CliError);
    expect(() => parseCliArgs(['positional'])).toThrow(CliError);
  });

  test('help and version flags', () => {
    expect(parseCliArgs(['-h']).help).toBe(true);
    expect(parseCliArgs(['--version']).version).toBe(true);
  });
});