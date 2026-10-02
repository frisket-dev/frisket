const browserUploads = new Set<string>();
const listeners = new Set<() => void>();
let version = 0;

function selectionKey(importRef: string): string {
  return `frisket:import-session-files:${importRef}`;
}

const FINGERPRINT_YIELD_FILES = 2_048;

function yieldToBrowser(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 0));
}

async function selectionSignature(files: File[], logicalPaths: string[]): Promise<string> {
  let hash = 0x811c9dc5;
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    const value = `${logicalPaths[index]}\0${file.size}\0${file.lastModified}\0${file.type}\n`;
    for (let offset = 0; offset < value.length; offset += 1) {
      hash = Math.imul(hash ^ value.charCodeAt(offset), 0x01000193);
    }
    if ((index + 1) % FINGERPRINT_YIELD_FILES === 0) await yieldToBrowser();
  }
  return `${files.length}:${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

export function isDirectorySelection(files: File[]): boolean {
  return files.some((file) => Boolean(
    (file as File & { webkitRelativePath?: unknown }).webkitRelativePath,
  ));
}

export async function rememberImportSessionFiles(
  importRef: string,
  files: File[],
  logicalPaths: string[],
): Promise<void> {
  try {
    const kind = isDirectorySelection(files) ? 'directory' : 'files';
    localStorage.setItem(selectionKey(importRef), `${kind}:${await selectionSignature(files, logicalPaths)}`);
  } catch {
    // Admission still works when browser storage is disabled; only safe
    // re-selection after a reload is unavailable.
  }
}

export function rememberedImportSelection(importRef: string): string | null {
  try {
    return localStorage.getItem(selectionKey(importRef));
  } catch {
    return null;
  }
}

export async function matchesImportSelection(
  remembered: string | null,
  files: File[],
  logicalPaths: string[],
): Promise<boolean> {
  const kind = isDirectorySelection(files) ? 'directory' : 'files';
  return remembered === `${kind}:${await selectionSignature(files, logicalPaths)}`;
}

export function forgetImportSelection(importRef: string): void {
  try { localStorage.removeItem(selectionKey(importRef)); } catch { /* storage is optional */ }
}

export function notifyImportSessionChanged(): void {
  version += 1;
  for (const listener of listeners) listener();
}

export function setImportSessionBrowserUploading(
  importRef: string,
  uploading: boolean,
): void {
  if (uploading) browserUploads.add(importRef);
  else browserUploads.delete(importRef);
  notifyImportSessionChanged();
}

export function isImportSessionBrowserUploading(importRef: string): boolean {
  return browserUploads.has(importRef);
}

export function importSessionSnapshot(): number {
  return version;
}

export function subscribeImportSessions(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
