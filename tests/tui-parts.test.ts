import { describe, expect, test } from 'bun:test';

import { TuiViewState } from '../src/modules/tui/view-state';
import {
  ThinkingUpdated,
  TokenEmitted,
  TokenUsageReported,
  TurnCompleted,
  TurnStarted,
} from '../src/kernel/contracts/chat';
import { ToolFinished, ToolStarted } from '../src/kernel/contracts/tools';
import { SkillLoaded, SkillsReflected } from '../src/kernel/contracts/skills';
import {
  CompactionStarted,
  ContextCompacted,
} from '../src/kernel/contracts/compaction';

describe('turn parts timeline', () => {
  test('keeps text → tool → text in chronological order', () => {
    const state = new TuiViewState();
    state.reset();
    state.apply(new TurnStarted({ sessionId: 's1', turnId: 't1', userInput: 'run' }));
    state.apply(new TokenEmitted({ sessionId: 's1', turnId: 't1', delta: 'before ' }));
    state.apply(
      new ToolStarted({
        sessionId: 's1',
        turnId: 't1',
        toolId: 'tool-1',
        toolName: 'read',
        toolInput: { path: 'x' },
      }),
    );
    state.apply(new ToolFinished({ sessionId: 's1', turnId: 't1', toolId: 'tool-1', output: 'body' }));
    state.apply(new TokenEmitted({ sessionId: 's1', turnId: 't1', delta: 'after' }));
    state.apply(
      new TurnCompleted({
        sessionId: 's1',
        turnId: 't1',
        content: 'before after',
        messages: [],
      }),
    );

    const turn = state.turns[0]!;
    expect(turn.parts.map((part) => part.kind)).toEqual(['text', 'tool', 'text']);
    expect(turn.parts[0]?.kind === 'text' && turn.parts[0].text).toBe('before ');
    expect(turn.parts[1]?.kind === 'tool' && turn.parts[1].tool.name).toBe('read');
    expect(turn.parts[1]?.kind === 'tool' && turn.parts[1].tool.output).toBe('body');
    expect(turn.parts[2]?.kind === 'text' && turn.parts[2].text).toBe('after');
    // Streaming deltas win over the final aggregated content.
    expect(turn.content).toBe('before after');
  });

  test('renders compaction as a synthetic turn', () => {
    const state = new TuiViewState();
    state.reset();
    state.apply(
      new CompactionStarted({ id: 'c1', reason: 'manual', thresholdTokens: 800 }),
    );
    state.apply(
      new ContextCompacted({
        result: {
          id: 'c1',
          reason: 'manual',
          compactedMessages: 8,
          keptMessages: 3,
          freedTokens: 1_200,
          summary: 'SUMMARY',
          thresholdTokens: 800,
          skipped: false,
        },
      }),
    );
    const turn = state.turns[0];
    expect(turn?.kind).toBe('compaction');
    const part = turn?.parts[0];
    expect(part?.kind).toBe('compaction');
    if (part?.kind === 'compaction') {
      expect(part.status).toBe('done');
      expect(part.text).toContain('上下文已压缩');
      expect(part.detail).toContain('8 条消息');
      expect(part.summary).toBe('SUMMARY');
    }
  });

  test('records provider token usage', () => {
    const state = new TuiViewState();
    state.reset();
    state.apply(
      new TokenUsageReported({
        promptTokens: 1200,
        completionTokens: 40,
        totalTokens: 1240,
      }),
    );
    expect(state.lastUsage).toMatchObject({
      promptTokens: 1200,
      completionTokens: 40,
      totalTokens: 1240,
    });
  });

  test('records reflection history entries', () => {
    const state = new TuiViewState();
    state.reset();
    state.apply(
      new SkillsReflected({
        created: 1,
        updated: 2,
        deprecated: 1,
        names: ['debug-import'],
        updatedNames: ['greet', 'deploy'],
      }),
    );
    const history = state.reflectionHistory;
    expect(history).toHaveLength(1);
    expect(history[0]?.created).toBe(1);
    expect(history[0]?.updated).toBe(2);
    expect(history[0]?.deprecated).toBe(1);
    expect(history[0]?.names).toEqual(['debug-import']);
    expect(history[0]?.updatedNames).toEqual(['greet', 'deploy']);
    expect(history[0]?.at).toBeGreaterThan(0);
  });

  test('thinking and skills keep their positions', () => {
    const state = new TuiViewState();
    state.reset();
    state.apply(new TurnStarted({ sessionId: 's1', turnId: 't2', userInput: 'x' }));
    state.apply(new ThinkingUpdated({ sessionId: 's1', turnId: 't2', delta: 'hmm' }));
    state.apply(new SkillLoaded({ sessionId: 's1', turnId: 't2', skillName: 'debug', skillPattern: 'imports' }));
    state.apply(new TokenEmitted({ sessionId: 's1', turnId: 't2', delta: 'answer' }));
    const turn = state.turns[0]!;
    expect(turn.parts.map((part) => part.kind)).toEqual(['thinking', 'skills', 'text']);
    expect(turn.thinking).toBe('hmm');
    expect(turn.skills).toHaveLength(1);
  });
});
