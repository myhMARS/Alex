import { describe, expect, test } from 'bun:test';

import {
  DsmlStreamFilter,
  extractDsml,
  parseDsmlBlock,
} from '../src/llm/dsml';

const SAMPLE = `<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="pwsh">
<｜｜DSML｜｜ parameter name="command" string="true">Get-ChildItem -Force</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>`;

describe('DSML tool calls', () => {
  test('parses a DSML block into a tool call', () => {
    const calls = parseDsmlBlock(SAMPLE);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.function.name).toBe('pwsh');
    expect(JSON.parse(calls[0]!.function.arguments)).toEqual({
      command: 'Get-ChildItem -Force',
    });
  });

  test('extracts DSML from surrounding content', () => {
    const extracted = extractDsml(`before\n${SAMPLE}\nafter`);
    expect(extracted.calls).toHaveLength(1);
    expect(extracted.cleaned).toContain('before');
    expect(extracted.cleaned).toContain('after');
    expect(extracted.cleaned).not.toContain('invoke');
    expect(extracted.cleaned).not.toContain('calls>');
  });

  test('keeps string="false" parameters as JSON values', () => {
    const block = `<｜DSML｜tool_calls>
<｜DSML｜invoke name="tool">
<｜DSML｜parameter name="count" string="false">5</parameter>
<parameter name="ok" string="true">true</parameter>
</invoke>
</tool_calls>`;
    const calls = parseDsmlBlock(block);
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0]!.function.arguments)).toEqual({ count: 5, ok: 'true' });
  });

  test('filters DSML out of streamed content', () => {
    const filter = new DsmlStreamFilter();
    const safe = filter.feed(`before\n${SAMPLE}\nafter`) + filter.finish();
    expect(safe).toContain('before');
    expect(safe).toContain('after');
    expect(safe).not.toContain('invoke');
    expect(filter.takeCalls()).toHaveLength(1);
  });
});