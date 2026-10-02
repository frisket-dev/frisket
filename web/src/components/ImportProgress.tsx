import { useCallback, useEffect, useRef, useState } from 'react';
import {
  cancelImportSession,
  listImportSessions,
  resolveImportSession,
  resumeImportSession,
  uploadFilesToImportSession,
  type ImportSessionStatus,
} from '../api/open';

const IMPORT_SESSION_CHANGED = 'frisket:import-session-changed';
const POLL_MS = 2_000;
const browserUploads = new Set<string>();

function selectionKey(importRef: string): string {
  return `frisket:import-session-files:${importRef}`;
}

function selectionSignature(files: File[], logicalPaths: string[]): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < files.length; index += 1) {
    const file = files[index];
    const value = `${logicalPaths[index]}\0${file.size}\0${file.lastModified}\0${file.type}\n`;
    for (let offset = 0; offset < value.length; offset += 1) {
      hash = Math.imul(hash ^ value.charCodeAt(offset), 0x01000193);
    }
  }
  return `${files.length}:${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

function isDirectorySelection(files: File[]): boolean {
  return files.some((file) => Boolean(
    (file as File & { webkitRelativePath?: unknown }).webkitRelativePath,
  ));
}

export function rememberImportSessionFiles(
  importRef: string,
  files: File[],
  logicalPaths: string[],
): void {
  try {
    const kind = isDirectorySelection(files) ? 'directory' : 'files';
    localStorage.setItem(selectionKey(importRef), `${kind}:${selectionSignature(files, logicalPaths)}`);
  } catch {
    // Admission still works when browser storage is disabled; only safe
    // re-selection after a reload is unavailable.
  }
}

function rememberedSelection(importRef: string): string | null {
  try {
    return localStorage.getItem(selectionKey(importRef));
  } catch {
    return null;
  }
}

function forgetSelection(importRef: string): void {
  try { localStorage.removeItem(selectionKey(importRef)); } catch { /* storage is optional */ }
}

export function notifyImportSessionChanged(projectId: string): void {
  window.dispatchEvent(new CustomEvent(IMPORT_SESSION_CHANGED, { detail: { projectId } }));
}

export function setImportSessionBrowserUploading(
  projectId: string,
  importRef: string,
  uploading: boolean,
): void {
  if (uploading) browserUploads.add(importRef);
  else browserUploads.delete(importRef);
  notifyImportSessionChanged(projectId);
}

function isPolling(session: ImportSessionStatus): boolean {
  return session.state === 'admitting' || session.state === 'running' || session.state === 'cancelling';
}

function isReadOnly(session: ImportSessionStatus): boolean {
  return ['admitting', 'running', 'paused', 'cancelling', 'cancelled'].includes(session.state);
}

function logicalPath(file: File): string {
  const relative = (file as File & { webkitRelativePath?: unknown }).webkitRelativePath;
  return typeof relative === 'string' && relative ? relative : file.name;
}

function sessionSummary(session: ImportSessionStatus): string {
  if (session.state === 'admitting') return `${session.admitted_files} files uploaded`;
  if (session.state === 'running' || session.state === 'cancelling') {
    return `${session.committed_rows} of ${session.admitted_files} rows added`;
  }
  if (session.state === 'paused') return `Paused after ${session.committed_rows} rows`;
  if (session.state === 'cancelled') return `Cancelled · ${session.committed_rows} rows retained`;
  if (session.state === 'removed') return 'Added rows removed';
  return `${session.committed_rows} rows added`;
}

export function ImportProgress({ projectId, onOpenSheet, onChanged, onError }: {
  projectId: string;
  onOpenSheet(sheetId: number): void;
  onChanged?(sheetId: number | null): void;
  onError?(message: string): void;
}) {
  const [sessions, setSessions] = useState<ImportSessionStatus[]>([]);
  const [busyRef, setBusyRef] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [, setBrowserUploadVersion] = useState(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const continueRef = useRef<ImportSessionStatus | null>(null);
  const previousRef = useRef<Map<string, string> | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await listImportSessions(projectId, { signal });
      if (!signal?.aborted) {
        const next = new Map(result.sessions.map((session) => [
          session.import_ref,
          `${session.state}:${session.committed_rows}:${session.sheet_id ?? ''}`,
        ]));
        for (const session of result.sessions) {
          if (session.sealed || ['cancelled', 'completed', 'kept', 'removed'].includes(session.state)) {
            forgetSelection(session.import_ref);
          }
        }
        const previous = previousRef.current;
        if (previous && result.sessions.some((session) => (
          previous.get(session.import_ref) !== next.get(session.import_ref)
        ))) onChanged?.(null);
        previousRef.current = next;
        setSessions(result.sessions);
      }
    } catch (error) {
      if (!signal?.aborted) setMessage(error instanceof Error ? error.message : String(error));
    }
  }, [onChanged, projectId]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const changed = (event: Event) => {
      if ((event as CustomEvent<{ projectId?: string }>).detail?.projectId === projectId) {
        setBrowserUploadVersion((version) => version + 1);
        void refresh(controller.signal);
      }
    };
    window.addEventListener(IMPORT_SESSION_CHANGED, changed);
    return () => {
      controller.abort();
      window.removeEventListener(IMPORT_SESSION_CHANGED, changed);
    };
  }, [projectId, refresh]);

  useEffect(() => {
    if (!sessions.some(isPolling)) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void refresh(controller.signal), POLL_MS);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refresh, sessions]);

  const command = async (
    session: ImportSessionStatus,
    run: () => Promise<ImportSessionStatus>,
  ) => {
    setBusyRef(session.import_ref);
    setMessage(null);
    try {
      const next = await run();
      setSessions((current) => (
        next.state === 'kept' || next.state === 'removed'
          ? current.filter((item) => item.import_ref !== next.import_ref)
          : current.map((item) => (item.import_ref === next.import_ref ? next : item))
      ));
      onChanged?.(next.sheet_id);
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      setMessage(text);
      onError?.(`Import failed: ${text}`);
    } finally {
      setBusyRef(null);
    }
  };

  if (!sessions.length) return null;
  return (
    <section className="notification-settings-notice" aria-label="Import progress" data-testid="import-progress">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        aria-label="Reselect import files"
        onChange={(event) => {
          const session = continueRef.current;
          const files = Array.from(event.target.files ?? []);
          event.target.value = '';
          if (!session || !files.length) return;
          const paths = files.map(logicalPath);
          const expected = rememberedSelection(session.import_ref);
          const kind = isDirectorySelection(files) ? 'directory' : 'files';
          if (!expected || expected !== `${kind}:${selectionSignature(files, paths)}`) {
            setMessage('These are not the same files in the same order. Reselect the original set, or cancel this import.');
            return;
          }
          if (files.length < session.through) {
            setMessage(`Reselect the original file set (${session.through} files were already uploaded).`);
            return;
          }
          void command(session, () => uploadFilesToImportSession(
            projectId, session, files, paths,
          ));
        }}
      />
      <div>
        <strong>Imports</strong>
        {sessions.map((session) => {
          const busy = busyRef === session.import_ref;
          const browserUploading = browserUploads.has(session.import_ref);
          return (
            <div key={session.import_ref} data-testid={`import-progress-${session.state}`}>
              <span>{session.sheet_name || 'Files'}: {sessionSummary(session)}</span>
              {session.error ? <span className="import-field-error"> · {session.error}</span> : null}
              {isReadOnly(session) && session.sheet_id != null && session.committed_rows > 0 ? (
                <span> · The sheet is read-only until import settles.</span>
              ) : null}{' '}
              {session.sheet_id != null && session.committed_rows > 0 ? (
                <button type="button" className="mini-btn" onClick={() => onOpenSheet(session.sheet_id!)}>
                  Open sheet
                </button>
              ) : null}{' '}
              {browserUploading ? <span> · Uploading files… You can close the importer.</span> : null}
              {!browserUploading && !session.sealed && !['cancelling', 'cancelled', 'kept', 'removed'].includes(session.state) ? (
                <>
                  <span>Upload was interrupted. Reselect the same files to continue, or cancel.</span>{' '}
                  <button type="button" className="mini-btn" disabled={busy} onClick={() => {
                    continueRef.current = session;
                    const remembered = rememberedSelection(session.import_ref);
                    if (remembered?.startsWith('directory:')) {
                      fileInputRef.current?.setAttribute('webkitdirectory', '');
                    } else {
                      fileInputRef.current?.removeAttribute('webkitdirectory');
                    }
                    fileInputRef.current?.click();
                  }}>Reselect files</button>{' '}
                </>
              ) : null}
              {session.state === 'paused' ? (
                <button type="button" className="mini-btn" disabled={busy} onClick={() => {
                  void command(session, () => resumeImportSession(projectId, session.import_ref));
                }}>Retry</button>
              ) : null}{' '}
              {['admitting', 'running', 'paused'].includes(session.state) ? (
                <button type="button" className="mini-btn" disabled={busy} onClick={() => {
                  void command(session, () => cancelImportSession(projectId, session.import_ref));
                }}>Cancel</button>
              ) : null}
              {session.state === 'cancelled' ? (
                <>
                  <button type="button" className="mini-btn" disabled={busy} onClick={() => {
                    void command(session, () => resolveImportSession(projectId, session.import_ref, 'keep'));
                  }}>Keep</button>{' '}
                  <button type="button" className="mini-btn" disabled={busy} onClick={() => {
                    void command(session, () => resolveImportSession(projectId, session.import_ref, 'remove'));
                  }}>Remove added rows</button>
                </>
              ) : null}
            </div>
          );
        })}
        {message ? <div className="import-field-error" role="alert">{message}</div> : null}
      </div>
    </section>
  );
}
