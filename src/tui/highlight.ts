/** Tiny language-aware syntax highlighter for TUI code blocks. */

export type CodeTokenKind = 'plain' | 'keyword' | 'string' | 'comment' | 'number';

export interface CodeToken {
  text: string;
  kind: CodeTokenKind;
}

const KEYWORDS: Record<string, Set<string>> = {
  ts: new Set([
    'const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while',
    'import', 'from', 'export', 'class', 'new', 'async', 'await', 'try', 'catch',
    'finally', 'throw', 'interface', 'type', 'enum', 'extends', 'implements',
    'public', 'private', 'protected', 'static', 'readonly', 'true', 'false',
    'null', 'undefined', 'this', 'super', 'switch', 'case', 'break', 'continue',
  ]),
  python: new Set([
    'def', 'class', 'return', 'import', 'from', 'as', 'if', 'elif', 'else',
    'for', 'while', 'try', 'except', 'finally', 'with', 'lambda', 'True',
    'False', 'None', 'and', 'or', 'not', 'in', 'is', 'pass', 'raise', 'yield',
    'async', 'await',
  ]),
  json: new Set(['true', 'false', 'null']),
  bash: new Set([
    'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'do', 'done', 'in',
    'function', 'case', 'esac', 'return', 'export', 'local',
  ]),
};

const ALIASES: Record<string, string> = {
  typescript: 'ts',
  javascript: 'ts',
  js: 'ts',
  tsx: 'ts',
  jsx: 'ts',
  py: 'python',
  sh: 'bash',
  shell: 'bash',
  zsh: 'bash',
  jsonc: 'json',
};

function normalizeLanguage(language: string): string {
  const key = language.trim().toLowerCase();
  return ALIASES[key] ?? key;
}

function commentPattern(language: string): RegExp | null {
  if (['python', 'bash', 'yaml', 'toml'].includes(language)) return /#.*$/;
  if (['ts', 'c', 'cpp', 'rust', 'go', 'java'].includes(language)) return /\/\/.*$/;
  return null;
}

export function highlightLine(line: string, language: string): CodeToken[] {
  const lang = normalizeLanguage(language);
  const keywords = KEYWORDS[lang] ?? KEYWORDS.ts!;
  const comment = commentPattern(lang);
  const tokens: CodeToken[] = [];

  // Strings/comments first; everything else is tokenized by words/numbers.
  const pattern = new RegExp(
    [
      '("(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'|`(?:\\\\.|[^`\\\\])*`)',
      comment ? `(${comment.source})` : '()',
    ].join('|'),
    'g',
  );

  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(line)) !== null) {
    if (match[0].length === 0) {
      pattern.lastIndex += 1;
      continue;
    }
    if (match.index > lastIndex) {
      tokens.push(...tokenizeWords(line.slice(lastIndex, match.index), keywords));
    }
    if (match[1] !== undefined) {
      tokens.push({ text: match[1], kind: 'string' });
    } else if (match[2] !== undefined) {
      tokens.push({ text: match[2], kind: 'comment' });
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < line.length) {
    tokens.push(...tokenizeWords(line.slice(lastIndex), keywords));
  }
  return tokens.length > 0 ? tokens : [{ text: line, kind: 'plain' }];
}

function tokenizeWords(text: string, keywords: Set<string>): CodeToken[] {
  const tokens: CodeToken[] = [];
  const wordPattern = /(\b[A-Za-z_]\w*\b|\b\d+(?:\.\d+)?\b)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = wordPattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({ text: text.slice(lastIndex, match.index), kind: 'plain' });
    }
    const word = match[0];
    let kind: CodeTokenKind = 'plain';
    if (/^\d/.test(word)) kind = 'number';
    else if (keywords.has(word)) kind = 'keyword';
    tokens.push({ text: word, kind });
    lastIndex = match.index + word.length;
  }
  if (lastIndex < text.length) {
    tokens.push({ text: text.slice(lastIndex), kind: 'plain' });
  }
  return tokens;
}

export function highlightCode(code: string, language: string): CodeToken[][] {
  return code.split('\n').map((line) => highlightLine(line, language));
}