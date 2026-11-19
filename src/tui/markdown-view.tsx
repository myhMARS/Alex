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
        if (block.type === 'heading') {
          return (
            <text key={index} fg={theme.accent}>
              <strong>
                <InlineText tokens={block.tokens} />
              </strong>
            </text>
          );
        }
        if (block.type === 'list') {
          return (
            <text key={index} fg={theme.text}>
              <span>• </span>
              <InlineText tokens={block.tokens} />
            </text>
          );
        }
        if (block.type === 'quote') {
          return (
            <text key={index} fg={theme.muted}>
              <span>│ </span>
              <InlineText tokens={block.tokens} />
            </text>
          );
        }
        if (block.type === 'code') {
          return (
            <box key={index} style={{ flexDirection: 'column', width }}>
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
            <text key={index} fg={theme.border}>
              ────
            </text>
          );
        }
        return (
          <text key={index} fg={theme.text}>
            <InlineText tokens={block.tokens} />
          </text>
        );
      })}
    </box>
  );
}