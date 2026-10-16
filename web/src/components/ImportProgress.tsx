import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { X } from 'lucide-react';
import {
  cancelImportSession,
  listImportSessions,
  resolveImportSession,
  resumeImportSession,
  snapshotImportFiles,
  uploadFilesToImportSession,
  type ImportSessionStatus,
} from '../api/open';
import { usePoll } from '../hooks/usePoll';
import {
  forgetImportSelection,
  importSessionSnapshot,
  isImportSessionBrowserUploading,
  matchesImportSelection,
  rememberedImportSelection,
  setImportSessionBrowserUploading,
  subscribeImportSessions,
} from './importProgressSession';
import { SearchPreparationReceipt } from './SearchPreparationReceipt';

const POLL_MS = 2_000;

function isPolling(session: ImportSessionStatus): boolean {
  return ['admitting', 'running', 'paused', 'cancelling'].includes(session.state);
}

function isReadOnly(session: ImportSessionStatus): boolean {
  return ['admitting', 'running', 'paused', 'cancelling', 'cancelled'].includes(session.state);
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
  const [preparingRef, setPreparingRef] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [preparation, setPreparation] = useState<{ projectId: string; batchId: string | null }>({
    projectId, batchId: null,
  });
  if (preparation.projectId !== projectId) setPreparation({ projectId, batchId: null });
  const openPreparationBatchId = preparation.projectId === projectId ? preparation.batchId : null;
  const setOpenPreparationBatchId = (batchId: string | null) => setPreparation({ projectId, batchId });
  const browserUploadVersion = useSyncExternalStore(
    subscribeImportSessions,
    importSessionSnapshot,
    importSessionSnapshot,
  );
  const preparationDialogRef = useRef<HTMLDialogElement | null>(null);
  const preparationCloseRef = useRef<HTMLButtonElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const continueRef = useRef<ImportSessionStatus | null>(null);
  const preparationGeneration = useRef(0);
  const previousRef = useRef<Map<string, string> | null>(null);

  useEffect(() => () => {
    preparationGeneration.current += 1;
  }, [projectId]);

  useEffect(() => {
    const dialog = preparationDialogRef.current;
    if (openPreparationBatchId && dialog && !dialog.open) {
      dialog.showModal();
      preparationCloseRef.current?.focus();
    }
  }, [openPreparationBatchId]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await listImportSessions(projectId, { signal });
      if (!signal?.aborted) {
        const next = new Map(result.sessions.map((session) => [
          session.import_ref,
          `${session.state}:${session.committed_rows}:${session.sheet_id ?? ''}:${session.search_preparation_batch_id ?? ''}`,
        ]));
        for (const session of result.sessions) {
          if (session.sealed || ['cancelled', 'completed', 'kept', 'removed'].includes(session.state)) {
            forgetImportSelection(session.import_ref);
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
    const timer = window.setTimeout(() => void refresh(controller.signal), 0);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [browserUploadVersion, projectId, refresh]);

  usePoll(() => refresh(), {
    active: sessions.some(isPolling),
    intervalMs: POLL_MS,
    guardOverlap: true,
  });

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

  const continueUpload = async (
    session: ImportSessionStatus,
    files: File[],
  ) => {
    setMessage(null);
    setImportSessionBrowserUploading(session.import_ref, true);
    try {
      await uploadFilesToImportSession(projectId, session, files);
      onChanged?.(session.sheet_id);
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      setMessage(text);
      onError?.(`Import failed: ${text}`);
    } finally {
      setImportSessionBrowserUploading(session.import_ref, false);
    }
  };

  if (!sessions.length) return null;
  const banner = (
    <section className="notification-settings-notice" aria-label="Import progress" data-testid="import-progress">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        aria-label="Reselect import files"
        onChange={(event) => {
          const session = continueRef.current;
          const files = snapshotImportFiles(event.target.files);
          event.target.value = '';
          if (!session || !files.length) return;
          const expected = rememberedImportSelection(session.import_ref);
          const generation = ++preparationGeneration.current;
          setPreparingRef(session.import_ref);
          void (async () => {
            try {
              const matches = await matchesImportSelection(expected, files);
              if (generation !== preparationGeneration.current) return;
              if (!matches) {
                setMessage('These are not the same files in the same order. Reselect the original set, or cancel this import.');
                return;
              }
              if (files.length < session.through) {
                setMessage(`Reselect the original file set (${session.through} files were already uploaded).`);
                return;
              }
              await continueUpload(session, files);
            } finally {
              if (generation === preparationGeneration.current) {
                setPreparingRef((current) => current === session.import_ref ? null : current);
              }
            }
          })();
        }}
      />
      <div>
        <strong>Imports</strong>
        {sessions.map((session) => {
          const busy = busyRef === session.import_ref;
          const preparing = preparingRef === session.import_ref;
          const browserUploading = isImportSessionBrowserUploading(session.import_ref);
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
              {session.search_preparation_batch_id ? (
                <button
                  type="button"
                  className="mini-btn"
                  data-testid="import-progress-open-preparation"
                  onClick={() => setOpenPreparationBatchId(session.search_preparation_batch_id)}
                >
                  Open preparation
                </button>
              ) : null}{' '}
              {browserUploading ? <span> · Uploading files… You can close the importer.</span> : null}
              {preparing ? <span> · Preparing files…</span> : null}
              {!preparing && !browserUploading && !session.sealed && !['cancelling', 'cancelled', 'kept', 'removed'].includes(session.state) ? (
                <>
                  <span>Upload was interrupted. Reselect the same files to continue, or cancel.</span>{' '}
                  <button type="button" className="mini-btn" disabled={busy} onClick={() => {
                    continueRef.current = session;
                    const remembered = rememberedImportSelection(session.import_ref);
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
                  if (preparingRef === session.import_ref) {
                    preparationGeneration.current += 1;
                    setPreparingRef(null);
                  }
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
  return (
    <>
      {banner}
      {openPreparationBatchId && (
        <dialog
          ref={preparationDialogRef}
          className="import-workspace-layer"
          aria-label="Search preparation"
          onCancel={(event) => {
            event.preventDefault();
            setOpenPreparationBatchId(null);
          }}
          onClose={() => setOpenPreparationBatchId(null)}
        >
          <button
            type="button"
            className="import-workspace-backdrop"
            tabIndex={-1}
            aria-label="Close preparation backdrop"
            onMouseDown={() => setOpenPreparationBatchId(null)}
          />
          <section className="import-workspace import-workspace-receipt">
            <header className="import-workspace-header import-workspace-header-receipt">
              <button
                type="button"
                className="icon-btn"
                ref={preparationCloseRef}
                aria-label="Close preparation"
                onClick={() => setOpenPreparationBatchId(null)}
              >
                <X size={16} />
              </button>
            </header>
            <div className="import-workspace-main import-workspace-main-receipt">
              <div className="import-workspace-content">
                <div className="import-workspace-panel">
                  <SearchPreparationReceipt
                    key={openPreparationBatchId}
                    projectId={projectId}
                    batchId={openPreparationBatchId}
                    onBatchChange={(batch) => setOpenPreparationBatchId(batch.id)}
                    onDone={() => setOpenPreparationBatchId(null)}
                    onError={(text) => {
                      setMessage(text);
                      onError?.(text);
                    }}
                  />
                </div>
              </div>
            </div>
          </section>
        </dialog>
      )}
    </>
  );
}
