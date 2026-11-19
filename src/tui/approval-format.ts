/** Bounded rendering of approval previews (diffs / command previews). */

export interface ApprovalPreview {
  lines: string[];
  total: number;
  truncated: boolean;
}

/** Approximate terminal column width (CJK/full-width chars count as 2). */
function charWidth(char: string): number {
  const code = char.codePointAt(0) ?? 0;
  if (code === 0 || code < 32 || (code >= 0x7f && code < 0xa0)) return 0;
  if (code >= 0x0300 && code <= 0x036f) return 0;
  if (
    (code >= 0x1100 && code <= 0x115f) ||
    (code >= 0x2e80 && code <= 0xa4cf) ||
    (code >= 0xac00 && code <= 0xd7a3) ||
    (code >= 0xf900 && code <= 0xfaff) ||
    (code >= 0xfe30 && code <= 0xfe6f) ||
    (code >= 0xff00 && code <= 0xff60) ||
    (code >= 0xffe0 && code <= 0xffe6) ||
    (code >= 0x1f300 && code <= 0x1faff)
  ) {
    return 2;
  }
  return 1;
}

function displayWidth(text: string): number {
  let width = 0;
  for (const char of text) width += charWidth(char);
  return width;
}

/** Wrap one logical line into visual lines that fit the panel width. */
export function wrapLine(line: string, maxWidth: number, continuationIndent = '  '): string[] {
  if (!line) return [''];
  const width = Math.max(8, maxWidth);
  const out: string[] = [];
  let current = '';
  let currentWidth = 0;

  for (const char of line) {
    const charCols = charWidth(char);
    if (currentWidth + charCols > width && current.trim().length > 0) {
      out.push(current);
      current = continuationIndent;
      currentWidth = displayWidth(continuationIndent);
      // Avoid a pathological indent wider than the panel.
      if (currentWidth >= width) {
        current = '';
        currentWidth = 0;
      }
    }
    current += char;
    currentWidth += charCols;
  }
  out.push(current);
  return out;
}

/**
 * Split a preview into wrapped display lines, bounding total lines.
 * Long single-line commands are wrapped instead of being cut at the panel edge.
 */
export function formatApprovalPreview(
  preview: string,
  width: number,
  maxLines = 400,
): ApprovalPreview {
  const maxLineWidth = Math.max(24, width - 6);
  const logical = preview
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map((line) => line.replace(/\t/g, '  ').replace(/\s+$/, ''));
  while (logical.length > 0 && logical[logical.length - 1] === '') logical.pop();

  const visual: string[] = [];
  for (const line of logical) {
    visual.push(...wrapLine(line, maxLineWidth));
  }

  const lines = visual.slice(0, maxLines);
  return { lines, total: visual.length, truncated: visual.length > lines.length };
}