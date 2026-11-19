/** Read an image from the OS clipboard (Windows/macOS) into a temp PNG path. */

import { execFile } from 'node:child_process';
import { access, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

const WINDOWS_SCRIPT = [
  'Add-Type -AssemblyName System.Windows.Forms',
  'Add-Type -AssemblyName System.Drawing',
  '$img = [System.Windows.Forms.Clipboard]::GetImage()',
  'if ($img -eq $null) { exit 3 }',
  `$path = Join-Path $env:TEMP ("alex-clip-${randomUUID()}.png")`,
  '$img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)',
  'Write-Output $path',
].join('; ');

/** Return a temp PNG path when the clipboard holds an image, else null. */
export async function readClipboardImagePath(): Promise<string | null> {
  if (process.platform === 'win32') {
    try {
      const { stdout } = await execFileAsync(
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-Command', WINDOWS_SCRIPT],
        { timeout: 8_000, windowsHide: true },
      );
      const path = stdout.trim().split(/\r?\n/).at(-1) ?? '';
      if (!path) return null;
      await access(path);
      return path;
    } catch {
      return null;
    }
  }
  if (process.platform === 'darwin') {
    const path = join(tmpdir(), `alex-clip-${randomUUID()}.png`);
    try {
      await execFileAsync('osascript', ['-e', `set pngData to the clipboard as «class PNGf»`]);
      // AppleScript cannot write binary directly here; fall back to pngpaste when present.
      await execFileAsync('pngpaste', [path]);
      await access(path);
      return path;
    } catch {
      await rm(path, { force: true }).catch(() => undefined);
      return null;
    }
  }
  return null;
}