import { importFileLogicalPath } from '../api/open';

const browserUploads = new Set<string>();
const browserUploadAborts = new Map<string, () => void>();
const listeners = new Set<() => void>();
let version = 0;

function selectionKey(importRef: string): string {
  return `frisket:import-session-files:${importRef}`;
}

const FINGERPRINT_YIELD_FILES = 2_048;

function yieldToBrowser(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 0));
}

async function selectionIdentity(files: File[]): Promise<string> {
  let hash = 0x811c9dc5;
  let directory = false;
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    if ((file as File & { webkitRelativePath?: unknown }).webkitRelativePath) directory = true;
    const value = `${importFileLogicalPath(file)}\0${file.size}\0${file.lastModified}\0${file.type}\n`;
    for (let offset = 0; offset < value.length; offset += 1) {
      hash = Math.imul(hash ^ value.charCodeAt(offset), 0x01000193);
    }
    if ((index + 1) % FINGERPRINT_YIELD_FILES === 0) await yieldToBrowser();
  }
  return `${directory ? 'directory' : 'files'}:${files.length}:${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

export async function rememberImportSessionFiles(
  importRef: string,
  files: File[],
): Promise<void> {
  try {
    localStorage.setItem(selectionKey(importRef), await selectionIdentity(files));
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
): Promise<boolean> {
  return remembered === await selectionIdentity(files);
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

/**
 * Connects a committed import's still-running browser upload to the shared
 * post-import Stop action. The importer owns the controller and unregisters
 * it when intake settles; this module only keeps the current callback.
 */
export function registerImportSessionUploadAbort(
  importRef: string,
  abort: () => void,
): () => void {
  browserUploadAborts.set(importRef, abort);
  notifyImportSessionChanged();
  return () => {
    if (browserUploadAborts.get(importRef) !== abort) return;
    browserUploadAborts.delete(importRef);
    notifyImportSessionChanged();
  };
}

/** Stops local byte transfer before the durable import cancellation request. */
export function abortImportSessionBrowserUpload(importRef: string): void {
  const abort = browserUploadAborts.get(importRef);
  if (!abort) return;
  browserUploadAborts.delete(importRef);
  abort();
  notifyImportSessionChanged();
}

export function importSessionSnapshot(): number {
  return version;
}

export function subscribeImportSessions(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
