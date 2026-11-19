/** Structured Markdown renderer for OpenTUI (theme-aware spans). */

import React, { useMemo } from 'react';

import { parseMarkdown, type InlineToken } from './markdown';
import { highlightLine, type CodeTokenKind } from './highlight';
import { theme } from './theme';

function codeColor(kind: CodeTokenKind): string {
  if (kind === 'keyword') return theme.keyword;
  if (kind === 'string') return theme.string;
  if (kind === 'comment') return theme.codeComment;
  if (kind === 'number') return theme.number;
  return theme.text;
}

function InlineText({ tokens }: { tokens: InlineToken[] }) {
  return (
    <>
      {tokens.map((token, index) => {
        if (token.code) {
          return (
            <span key={index} fg={theme.tool}>
              {token.text}
            </span>
          );
        }
        if (token.link) {
          return (
            <span key={index} fg={theme.accent}>
              {token.text}
            </span>
          );
        }
        if (token.bold) {
          return (
            <span key={index}>
              <strong>{token.text}</strong>
            </span>
          );
        }
        return <span key={index}>{token.text}</span>;
      })}
    </>
  );
}

export function MarkdownText({ text, width }: { text: string; width: number }) {
  const blocks = useMemo(() => parseMarkdown(text), [text]);
  return (
    <box style={{ flexDirection: 'column', width }}>
      {blocks.map((block, index) => {
        const previous = index > 0 ? blocks[index - 1] : undefined;
        // Keep a blank line between major blocks, but let headings flow into
        // their following content instead of adding an extra row after them.
        const marginTop =
          index > 0 && block.gapBefore && previous?.type !== 'heading' ? 1 : 0;
        if (block.type === 'heading') {
          return (
            <text key={index} fg={theme.accent} style={{ marginTop }}>
              <strong>
                <InlineText tokens={block.tokens} />
              </strong>
            </text>
          );
        }
        if (block.type === 'list') {
          return (
            <text key={index} fg={theme.text} style={{ marginTop }}>
              <span>• </span>
              <InlineText tokens={block.tokens} />
            </text>
          );
        }
        if (block.type === 'quote') {
          return (
            <text key={index} fg={theme.muted} style={{ marginTop }}>
              <span>│ </span>
              <InlineText tokens={block.tokens} />
            </text>
          );
        }
        if (block.type === 'code') {
          return (
            <box key={index} style={{ flexDirection: 'column', width, marginTop }}>
              {block.content.split('\n').map((line, lineIndex) => (
                <text key={lineIndex}>
                  <span fg={theme.border}>│ </span>
                  {line
                    ? highlightLine(line, block.language).map((token, tokenIndex) => (
                        <span key={tokenIndex} fg={codeColor(token.kind)}>
                          {token.text}
                        </span>
                      ))
                    : ' '}
                </text>
              ))}
            </box>
          );
        }
        if (block.type === 'hr') {
          return (
            <text key={index} fg={theme.border} style={{ marginTop }}>
              ────
            </text>
          );
        }
        return (
          <text key={index} fg={theme.text} style={{ marginTop }}>
            <InlineText tokens={block.tokens} />
          </text>
        );
      })}
    </box>
  );
}