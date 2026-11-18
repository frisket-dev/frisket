import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {
  cancelImportSession,
  estimateImportPreparationRetry,
  listImportSessions,
  resolveImportSession,
  resumeImportSession,
  retryImportPreparation,
  snapshotImportFiles,
  uploadFilesToImportSession,
  type ImportSessionStatus,
  type RunEstimate,
} from '../api/open';
import { usePoll } from '../hooks/usePoll';
import { countLabel } from '../format';
import type { SearchPreparationNotice } from './useSearchPreparationNotice';
import {
  forgetImportSelection,
  importSessionSnapshot,
  isImportSessionBrowserUploading,
  matchesImportSelection,
  rememberedImportSelection,
  setImportSessionBrowserUploading,
  subscribeImportSessions,
} from './importProgressSession';
import { errorMessage } from '../hooks/errorMessage';

const POLL_MS = 2_000;
const ACTIVE_CHILD_STATES = new Set(['queued', 'running', 'stalled', 'orphaned']);

// Failed pages are settled: only a retry puts them back in the queue.
function preparationIncomplete(session: ImportSessionStatus): boolean {
  return session.preparation_completed + session.failed_pages < session.preparation_total;
}

function isPolling(session: ImportSessionStatus): boolean {
  return ['admitting', 'running', 'paused', 'cancelling'].includes(session.state)
    || preparationIncomplete(session)
    || session.preparation_children.some((child) => ACTIVE_CHILD_STATES.has(child.status));
}

export interface ImportActivityLine {
  key: 'adding' | 'native' | 'ocr';
  label: string;
  completed: number;
  total: number | null;
  detail?: string;
  complete?: boolean;
}

/** Rows of an import's sheet whose page text is being read now or still
 * waits, front first. The server bounds each list. */
export interface ImportPageReading {
  importRef: string;
  sheetId: string;
  readingRowIds: string[];
  waitingRowIds: string[];
}

export interface ImportActivity {
  importRef: string;
  session: ImportSessionStatus;
  lines: ImportActivityLine[];
  pageReading: ImportPageReading | null;
  needsRecovery: boolean;
  canStop: boolean;
  canRetry: boolean;
}

/** Consent to one retry estimate, carried by value to the retry request. */
export interface ImportRetryConsent {
  importRef: string;
  planToken: string;
  confirmation: string;
  estimate: RunEstimate;
  pages: number;
}

export function pendingImportRowCount(activity: ImportActivity | null): number {
  if (!activity || !['admitting', 'running'].includes(activity.session.state)) return 0;
  const selectedTotal = importRowTotal(activity.session) ?? activity.session.committed_rows;
  const processed = activity.session.intake_kind === 'table'
    ? activity.session.processed_rows ?? activity.session.committed_rows
    : activity.session.committed_rows;
  return Math.max(selectedTotal - processed, 0);
}

function importRowTotal(session: ImportSessionStatus): number | null {
  if (session.intake_kind !== 'table') return session.selected_total ?? session.admitted_files;
  if (session.selected_total != null) return session.selected_total;
  if (session.estimated_rows == null) return null;
  return Math.max(session.estimated_rows, session.processed_rows ?? 0, session.committed_rows);
}

export function preferredImportActivity(activities: ImportActivity[]): ImportActivity | null {
  const live = activities.find((activity) => (
    !['paused', 'cancelled'].includes(activity.session.state)
    && (['admitting', 'running', 'cancelling'].includes(activity.session.state)
      || activity.pageReading !== null)
  ));
  if (live) return live;
  return activities.find((activity) => !activity.needsRecovery) ?? activities[0] ?? null;
}

export function importActivity(session: ImportSessionStatus): ImportActivity | null {
  const selectedTotal = importRowTotal(session);
  const committedRows = selectedTotal === null
    ? session.committed_rows : Math.min(session.committed_rows, selectedTotal);
  const preparationPending = preparationIncomplete(session);
  const active = ['admitting', 'running', 'paused', 'cancelling', 'cancelled'].includes(session.state)
    || (session.intake_kind === 'table' && ['completed', 'kept'].includes(session.state))
    || Boolean(session.error)
    || preparationPending
    || session.preparation_children.some((child) => ACTIVE_CHILD_STATES.has(child.status));
  if (!active) return null;

  const lines: ImportActivityLine[] = [{
    key: 'adding',
    label: 'Adding rows',
    completed: committedRows,
    total: selectedTotal,
    complete: session.state === 'completed'
      && (session.intake_kind === 'table' || (selectedTotal !== null && committedRows >= selectedTotal)),
    ...(session.intake_kind === 'table' ? {
      detail: `${committedRows.toLocaleString()} added${session.skipped_duplicates ? ` · ${session.skipped_duplicates.toLocaleString()} duplicates skipped` : ''}`,
    } : {}),
  }];
  const nativeTotal = Math.max(session.preparation_total - session.ocr_total, 0);
  const nativeCompleted = Math.max(Math.min(
    session.preparation_completed - session.ocr_completed,
    nativeTotal,
  ), 0);
  const unsuccessful = new Set(['failed', 'partial', 'cancelled']);
  const nativeNeedsAttention = session.unreadable_total > 0
    || session.preparation_children.some((child) => (
      child.action_id === 'media.read_pdf_text' && unsuccessful.has(child.status)
    ));
  if (nativeTotal > 0) lines.push({
    key: 'native',
    label: 'Text layer',
    completed: nativeCompleted,
    total: nativeTotal,
    complete: nativeCompleted >= nativeTotal && !nativeNeedsAttention,
    ...(nativeNeedsAttention ? {
      detail: `${nativeCompleted.toLocaleString()} of ${nativeTotal.toLocaleString()} · needs attention`,
    } : {}),
  });
  if (session.ocr_total > 0) {
    const ocrCompleted = Math.min(session.ocr_completed, session.ocr_total);
    const ocrNeedsAttention = session.failed_pages > 0
      || session.preparation_children.some((child) => (
        child.action_id === 'media.ocr' && unsuccessful.has(child.status)
      ));
    const ocrProgress = `${ocrCompleted.toLocaleString()} of ${session.ocr_total.toLocaleString()}`;
    lines.push({
      key: 'ocr',
      label: 'OCR',
      completed: ocrCompleted,
      total: session.ocr_total,
      complete: ocrCompleted >= session.ocr_total && !ocrNeedsAttention,
      ...(ocrNeedsAttention ? {
        detail: session.failed_pages > 0
          ? `${ocrProgress} · ${countLabel(session.failed_pages, 'page')} not read`
          : `${ocrProgress} · needs attention`,
      } : {}),
    });
  }

  const pagesPending = session.reading_row_ids.length > 0 || session.waiting_row_ids.length > 0;
  const pageReading: ImportPageReading | null = pagesPending && session.sheet_id !== null ? {
    importRef: session.import_ref,
    sheetId: String(session.sheet_id),
    readingRowIds: session.reading_row_ids.map(String),
    waitingRowIds: session.waiting_row_ids.map(String),
  } : null;

  return {
    importRef: session.import_ref,
    session,
    lines,
    pageReading,
    needsRecovery: session.state === 'paused' || session.state === 'cancelled'
      || (!session.sealed
        && !isImportSessionBrowserUploading(session.import_ref)
        && !['cancelling', 'cancelled'].includes(session.state)),
    canStop: ['admitting', 'running', 'paused'].includes(session.state)
      || preparationPending,
    canRetry: session.failed_pages > 0 && !pagesPending,
  };
}

export interface ImportProgressController {
  activities: ImportActivity[];
  refresh(): Promise<void>;
  message: string | null;
  busyRef: string | null;
  preparingRef: string | null;
  retryConsent: ImportRetryConsent | null;
  resume(activity: ImportActivity): void;
  stop(activity: ImportActivity): Promise<void>;
  keep(activity: ImportActivity): void;
  remove(activity: ImportActivity): void;
  retryFailedPages(activity: ImportActivity): void;
  confirmRetry(consent: ImportRetryConsent): void;
  cancelRetry(): void;
}

export interface ImportProgressChange {
  sheetId: number | null;
  refreshData: boolean;
}

function pollSignature(session: ImportSessionStatus): string {
  return `${session.state}:${session.committed_rows}:${session.preparation_completed}:${session.ocr_completed}:${session.sheet_id ?? ''}`;
}

function isSoftTableProgress(session: ImportSessionStatus): boolean {
  return session.intake_kind === 'table'
    && (session.state === 'admitting' || session.state === 'running');
}

export function useImportProgress({ projectId, onChanged, onError }: {
  projectId: string;
  onChanged?(change: ImportProgressChange): void;
  onError?(message: string): void;
}): ImportProgressController {
  const [sessions, setSessions] = useState<ImportSessionStatus[]>([]);
  const [busyRef, setBusyRef] = useState<string | null>(null);
  const [preparingRef, setPreparingRef] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [retryConsent, setRetryConsent] = useState<ImportRetryConsent | null>(null);
  const browserUploadVersion = useSyncExternalStore(
    subscribeImportSessions,
    importSessionSnapshot,
    importSessionSnapshot,
  );
  const preparationGeneration = useRef(0);
  const refreshGeneration = useRef(0);
  const previousRef = useRef<Map<string, string> | null>(null);

  useEffect(() => () => {
    preparationGeneration.current += 1;
    refreshGeneration.current += 1;
  }, [projectId]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const generation = ++refreshGeneration.current;
    try {
      const result = await listImportSessions(projectId, { signal });
      if (signal?.aborted || generation !== refreshGeneration.current) return;
      const next = new Map(result.sessions.map((session) => [session.import_ref, pollSignature(session)]));
      for (const session of result.sessions) {
        if (session.sealed || ['cancelled', 'completed', 'kept', 'removed'].includes(session.state)) {
          forgetImportSelection(session.import_ref);
        }
      }
      const previous = previousRef.current;
      if (previous) {
        const changed = result.sessions.filter((session) => (
          previous.get(session.import_ref) !== next.get(session.import_ref)
        ));
        const removed = [...previous.keys()].some((importRef) => !next.has(importRef));
        if (changed.length > 0 || removed) {
          onChanged?.({
            sheetId: null,
            refreshData: removed || changed.some((session) => !isSoftTableProgress(session)),
          });
        }
      }
      previousRef.current = next;
      setSessions(result.sessions);
    } catch (error) {
      if (!signal?.aborted && generation === refreshGeneration.current) {
        setMessage(errorMessage(error));
      }
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

  // A command resolving to null changed nothing on the server.
  const command = useCallback(async (
    importRef: string,
    run: () => Promise<ImportSessionStatus | null>,
  ) => {
    setBusyRef(importRef);
    setMessage(null);
    try {
      const next = await run();
      if (next === null) return;
      refreshGeneration.current += 1;
      if (previousRef.current) {
        const updated = new Map(previousRef.current);
        if (next.state === 'removed' || (next.state === 'kept' && next.intake_kind !== 'table')) {
          updated.delete(next.import_ref);
        } else {
          updated.set(next.import_ref, pollSignature(next));
        }
        previousRef.current = updated;
      }
      setSessions((current) => (
        next.state === 'removed' || (next.state === 'kept' && next.intake_kind !== 'table')
          ? current.filter((item) => item.import_ref !== next.import_ref)
          : current.map((item) => (item.import_ref === next.import_ref ? next : item))
      ));
      onChanged?.({ sheetId: next.sheet_id, refreshData: true });
    } catch (error) {
      const text = errorMessage(error);
      setMessage(text);
      onError?.(`Import failed: ${text}`);
    } finally {
      setBusyRef(null);
    }
  }, [onChanged, onError]);

  const continueUpload = useCallback(async (session: ImportSessionStatus, files: File[]) => {
    setMessage(null);
    setImportSessionBrowserUploading(session.import_ref, true);
    try {
      await uploadFilesToImportSession(projectId, session, files);
      onChanged?.({ sheetId: session.sheet_id, refreshData: true });
    } catch (error) {
      const text = errorMessage(error);
      setMessage(text);
      onError?.(`Import failed: ${text}`);
    } finally {
      setImportSessionBrowserUploading(session.import_ref, false);
    }
  }, [onChanged, onError, projectId]);

  const continueSelection = useCallback((session: ImportSessionStatus, files: File[]) => {
    if (!files.length) return;
    const expected = rememberedImportSelection(session.import_ref);
    const generation = ++preparationGeneration.current;
    setPreparingRef(session.import_ref);
    void (async () => {
      try {
        const matches = await matchesImportSelection(expected, files);
        if (generation !== preparationGeneration.current) return;
        if (!matches) {
          setMessage('These are not the same files in the same order. Reselect the original set, or keep what was added.');
          return;
        }
        if (files.length < session.through) {
          setMessage(`Reselect the original file set (${session.through} files were already uploaded).`);
          return;
        }
        await continueUpload(session, files);
        await command(session.import_ref, () => resumeImportSession(projectId, session.import_ref));
      } finally {
        if (generation === preparationGeneration.current) {
          setPreparingRef((current) => current === session.import_ref ? null : current);
        }
      }
    })();
  }, [command, continueUpload, projectId]);

  const requestReselect = useCallback((session: ImportSessionStatus) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.setAttribute('aria-label', 'Reselect import files');
    if (rememberedImportSelection(session.import_ref)?.startsWith('directory:')) {
      input.setAttribute('webkitdirectory', '');
    }
    input.addEventListener('change', () => {
      const files = snapshotImportFiles(input.files);
      input.value = '';
      continueSelection(session, files);
    }, { once: true });
    input.click();
  }, [continueSelection]);

  const activities = useMemo(() => {
    // importActivity also reads browser upload state outside the session snapshot.
    void browserUploadVersion;
    return sessions.flatMap((session) => {
      const activity = importActivity(session);
      return activity ? [activity] : [];
    });
  }, [sessions, browserUploadVersion]);

  return useMemo<ImportProgressController>(() => ({
    activities,
    refresh,
    message,
    busyRef,
    preparingRef,
    retryConsent,
    resume(activity) {
      if (!activity.session.sealed) {
        requestReselect(activity.session);
        return;
      }
      void command(activity.importRef, () => resumeImportSession(projectId, activity.importRef));
    },
    stop(activity) {
      if (preparingRef === activity.importRef) {
        preparationGeneration.current += 1;
        setPreparingRef(null);
      }
      return command(activity.importRef, () => cancelImportSession(projectId, activity.importRef));
    },
    remove(activity) {
      if (activity.session.state !== 'cancelled') return;
      void command(activity.importRef, () => resolveImportSession(projectId, activity.importRef, 'remove'));
    },
    retryFailedPages(activity) {
      setRetryConsent(null);
      const { importRef } = activity;
      void command(importRef, async () => {
        const estimated = await estimateImportPreparationRetry(projectId, importRef);
        if (!estimated.estimate.requires_confirmation) {
          return retryImportPreparation(projectId, importRef, estimated.planToken, null);
        }
        const confirmation = estimated.estimate.promise_set_hash;
        if (!confirmation) throw new Error('The retry estimate is missing its consent token.');
        setRetryConsent({
          importRef,
          planToken: estimated.planToken,
          confirmation,
          estimate: estimated.estimate,
          pages: estimated.pages,
        });
        return null;
      });
    },
    confirmRetry(consent) {
      setRetryConsent(null);
      void command(consent.importRef, () => retryImportPreparation(
        projectId, consent.importRef, consent.planToken, consent.confirmation,
      ));
    },
    cancelRetry() {
      setRetryConsent(null);
    },
    keep(activity) {
      void command(activity.importRef, () => (
        activity.session.state === 'cancelled'
          ? resolveImportSession(projectId, activity.importRef, 'keep')
          : cancelImportSession(projectId, activity.importRef).then((cancelled) => (
            cancelled.state === 'cancelled'
              ? resolveImportSession(projectId, activity.importRef, 'keep')
              : cancelled
          ))
      ));
    },
  }), [activities, message, busyRef, preparingRef, retryConsent, command, requestReselect, projectId, refresh]);
}

interface ImportSearchActivityLine {
  key: string;
  label: string;
  completed: number | null;
  total: number | null;
  detail?: string;
  complete?: boolean;
}

export function composeImportSearchActivity(
  activity: ImportActivity | null,
  search: SearchPreparationNotice | null,
): { notice: SearchPreparationNotice; lines: ImportSearchActivityLine[] } | null {
  if (!activity) return null;
  const { session } = activity;
  if (session.intake_kind === 'table') return composeTableImportActivity(activity, search);
  const selectedTotal = session.selected_total ?? session.admitted_files;
  const preparationRemaining = Math.max(
    session.preparation_total - session.preparation_completed,
    0,
  );
  const readyFiles = Math.max(0, Math.min(session.committed_rows - preparationRemaining, selectedTotal));
  const pdfIndexFiles = search?.pdfFiles ?? [];
  const indexedPages = pdfIndexFiles.reduce((total, file) => total + file.completed, 0);
  const indexPages = pdfIndexFiles.reduce((total, file) => total + file.total, 0);
  const indexActive = search?.tone === 'active';
  const indexComplete = search?.tone === 'complete';
  const ocrLine = activity.lines.find((line) => line.key === 'ocr') ?? null;
  const readingOcr = ocrLine !== null && ocrLine.total !== null && ocrLine.completed < ocrLine.total;
  const lines: ImportSearchActivityLine[] = [
    ...activity.lines.map((line) => ({ ...line })),
    {
      key: 'search-index',
      label: 'Search index',
      completed: indexPages > 0 ? indexedPages : null,
      total: indexPages > 0 ? indexPages : null,
      complete: indexComplete,
      detail: indexPages > 0
        ? `${indexedPages.toLocaleString()} / ${indexPages.toLocaleString()} pages`
        : indexComplete ? 'Ready' : indexActive ? 'Indexing ready files' : 'Waiting for search activity',
    },
  ];
  const failed = Boolean(session.error) || search?.tone === 'error';
  const recovering = activity.needsRecovery;
  return {
    notice: {
      activityKey: `import:${activity.importRef}`,
      reviewBatchId: search?.reviewBatchId ?? null,
      stopBatchId: activity.canStop ? activity.importRef : null,
      stage: failed ? 'failed' : search?.stage ?? 'prepare',
      tone: failed ? 'error' : recovering ? 'warning' : 'active',
      title: session.error
        ? session.selected_total !== null ? 'PDF preparation failed' : 'Import failed'
        : session.state === 'cancelled' ? 'Import cancelled' : recovering ? 'Import interrupted' : 'Preparing search',
      detail: session.error ?? (recovering
        ? `${session.committed_rows.toLocaleString()} rows added`
        : readingOcr && ocrLine
          ? `Reading text with OCR · ${ocrLine.completed.toLocaleString()} of ${ocrLine.total?.toLocaleString()} files`
          : `${readyFiles.toLocaleString()} of ${selectedTotal.toLocaleString()} files ready`),
      progress: null,
      details: search?.details ?? [],
      pdfFiles: search?.pdfFiles,
      pdfFilesTruncated: search?.pdfFilesTruncated,
    },
    lines,
  };
}

function composeTableImportActivity(
  activity: ImportActivity,
  search: SearchPreparationNotice | null,
): { notice: SearchPreparationNotice; lines: ImportSearchActivityLine[] } {
  const { session } = activity;
  const total = importRowTotal(session);
  const added = session.committed_rows.toLocaleString();
  const processed = session.processed_rows ?? session.committed_rows;
  const finished = ['completed', 'kept'].includes(session.state);
  const detail = session.error ?? (activity.needsRecovery || session.state === 'kept'
    ? `${added} rows added · import stopped`
    : `${added} rows added${session.skipped_duplicates ? ` · ${session.skipped_duplicates.toLocaleString()} duplicates skipped` : ''}`);
  return {
    notice: {
      activityKey: `import:${session.import_ref}`,
      reviewBatchId: null,
      stopBatchId: activity.canStop ? session.import_ref : null,
      stage: session.error ? 'failed' : 'prepare',
      tone: session.error ? 'error' : activity.needsRecovery || session.state === 'kept' ? 'warning' : finished ? 'complete' : 'active',
      title: `${session.state === 'cancelled' ? 'Import cancelled:' : session.state === 'kept' ? 'Import stopped:' : finished ? 'Imported' : 'Importing'} ${session.source_label ?? session.sheet_name}`,
      detail,
      progress: null,
      details: session.source_label ? [session.source_label] : [],
    },
    lines: [
      {
        key: 'adding', label: 'Adding rows', completed: processed,
        total, complete: session.state === 'completed',
        detail: session.state === 'kept' ? `${added} added · stopped`
          : total === null ? `${added} added` : `${processed.toLocaleString()} of ${session.selected_total === null ? 'about ' : ''}${total.toLocaleString()}`,
      },
      ...(!finished || search ? [{
        key: 'search-index', label: 'Search index', completed: null, total: null,
        detail: search?.detail ?? 'Rows are indexed as they arrive',
        complete: search?.tone === 'complete',
      }] : []),
    ],
  };
}
