/** Minimal line-oriented unified diff for tool approval previews. */

export interface UnifiedDiffOptions {
  context?: number;
  maxLines?: number;
}

export function unifiedDiff(
  oldText: string,
  newText: string,
  filePath: string,
  options: UnifiedDiffOptions = {},
): string {
  if (oldText === newText) return `(no changes to ${filePath})`;
  const context = Math.max(0, options.context ?? 3);
  const maxLines = options.maxLines ?? 200;

  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');

  let prefix = 0;
  while (
    prefix < oldLines.length &&
    prefix < newLines.length &&
    oldLines[prefix] === newLines[prefix]
  ) {
    prefix += 1;
  }

  let oldEnd = oldLines.length;
  let newEnd = newLines.length;
  while (oldEnd > prefix && newEnd > prefix && oldLines[oldEnd - 1] === newLines[newEnd - 1]) {
    oldEnd -= 1;
    newEnd -= 1;
  }

  const from = Math.max(0, prefix - context);
  const oldBlockEnd = Math.min(oldLines.length, oldEnd + context);
  const newBlockEnd = Math.min(newLines.length, newEnd + context);

  const before = oldLines.slice(from, prefix);
  const removed = oldLines.slice(prefix, oldEnd);
  const added = newLines.slice(prefix, newEnd);
  const after = oldLines.slice(oldEnd, oldBlockEnd);

  const body: string[] = [];
  body.push(`--- a/${filePath}`);
  body.push(`+++ b/${filePath}`);
  body.push(`@@ -${from + 1},${oldBlockEnd - from} +${from + 1},${newBlockEnd - from} @@`);
  for (const line of before) body.push(` ${line}`);
  for (const line of removed) body.push(`-${line}`);
  for (const line of added) body.push(`+${line}`);
  for (const line of after) body.push(` ${line}`);

  if (body.length > maxLines) {
    return [...body.slice(0, maxLines), `… (${body.length - maxLines} more diff lines)`].join('\n');
  }
  return body.join('\n');
}