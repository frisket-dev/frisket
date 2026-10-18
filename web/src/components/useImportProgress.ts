import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {
  cancelImportSession,
  listImportSessions,
  resolveImportSession,
  resumeImportSession,
  snapshotImportFiles,
  uploadFilesToImportSession,
  type ImportPreparationChild,
  type ImportSessionStatus,
  type RunProgress,
} from '../api/open';
import { usePoll } from '../hooks/usePoll';
import type { SearchPreparationNotice } from './useSearchPreparationNotice';
import {
  abortImportSessionBrowserUpload,
  forgetImportSelection,
  importSessionSnapshot,
  isImportSessionBrowserUploading,
  matchesImportSelection,
  rememberedImportSelection,
  setImportSessionBrowserUploading,
  subscribeImportSessions,
} from './importProgressSession';

const POLL_MS = 2_000;
const ACTIVE_CHILD_STATES = new Set(['queued', 'running', 'stalled', 'orphaned']);

function isPolling(session: ImportSessionStatus): boolean {
  return ['admitting', 'running', 'paused', 'cancelling'].includes(session.state)
    || session.preparation_completed < session.preparation_total
    || session.preparation_children.some((child) => ACTIVE_CHILD_STATES.has(child.status));
}

function childRunStatus(status: string): RunProgress['status'] {
  if (status === 'done' || status === 'complete' || status === 'completed') return 'complete';
  if (status === 'failed' || status === 'partial') return 'failed';
  if (status === 'cancelled') return 'cancelled';
  if (status === 'running' || status === 'stalled' || status === 'orphaned') return status;
  return 'queued';
}

export interface ImportActivityLine {
  key: 'adding' | 'native' | 'ocr';
  label: string;
  completed: number;
  total: number;
  detail?: string;
  complete?: boolean;
}

export interface ImportActivity {
  importRef: string;
  session: ImportSessionStatus;
  lines: ImportActivityLine[];
  liveRun: RunProgress | null;
  needsRecovery: boolean;
  canStop: boolean;
}

export function pendingImportRowCount(activity: ImportActivity | null): number {
  if (!activity || !['admitting', 'running'].includes(activity.session.state)) return 0;
  const selectedTotal = activity.session.selected_total ?? activity.session.admitted_files;
  return Math.max(selectedTotal - activity.session.committed_rows, 0);
}

export function preferredImportActivity(activities: ImportActivity[]): ImportActivity | null {
  const live = activities.find((activity) => (
    !['paused', 'cancelled'].includes(activity.session.state)
    && (['admitting', 'running', 'cancelling'].includes(activity.session.state)
      || activity.liveRun !== null)
  ));
  if (live) return live;
  return activities.find((activity) => !activity.needsRecovery) ?? activities[0] ?? null;
}

function activeChild(session: ImportSessionStatus): ImportPreparationChild | null {
  const children = [...session.preparation_children].reverse();
  return children.find((child) => child.run_id !== null && ACTIVE_CHILD_STATES.has(child.status))
    ?? children.find((child) => child.run_id !== null && child.status === 'partial')
    ?? null;
}

export function importActivity(session: ImportSessionStatus): ImportActivity | null {
  const selectedTotal = session.selected_total ?? session.admitted_files;
  const committedRows = Math.min(session.committed_rows, selectedTotal);
  const preparationIncomplete = session.preparation_completed < session.preparation_total;
  const active = ['admitting', 'running', 'paused', 'cancelling', 'cancelled'].includes(session.state)
    || Boolean(session.error)
    || preparationIncomplete
    || session.preparation_children.some((child) => ACTIVE_CHILD_STATES.has(child.status));
  if (!active) return null;

  const lines: ImportActivityLine[] = [{
    key: 'adding',
    label: 'Adding rows',
    completed: committedRows,
    total: selectedTotal,
    complete: ['completed', 'kept'].includes(session.state) && committedRows >= selectedTotal,
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
    const ocrNeedsAttention = session.preparation_children.some((child) => (
      child.action_id === 'media.ocr' && unsuccessful.has(child.status)
    ));
    lines.push({
      key: 'ocr',
      label: 'OCR',
      completed: ocrCompleted,
      total: session.ocr_total,
      complete: ocrCompleted >= session.ocr_total && !ocrNeedsAttention,
      ...(ocrNeedsAttention ? {
        detail: `${ocrCompleted.toLocaleString()} of ${session.ocr_total.toLocaleString()} · needs attention`,
      } : {}),
    });
  }

  const child = activeChild(session);
  const liveRun: RunProgress | null = child && session.sheet_id !== null ? {
    runId: String(child.run_id),
    actionKind: child.action_id,
    actionName: child.action_id === 'media.ocr' ? 'OCR PDFs' : 'Read PDF text',
    sheetId: String(session.sheet_id),
    targetColumnId: '',
    targetRowIds: null,
    status: childRunStatus(child.status),
    completedRows: child.completed_rows,
    totalRows: child.row_count,
    failedRows: child.status === 'partial'
      ? Math.max(child.row_count - child.completed_rows, 0)
      : 0,
    costSoFar: 0,
  } : null;

  return {
    importRef: session.import_ref,
    session,
    lines,
    liveRun,
    needsRecovery: session.state === 'paused' || session.state === 'cancelled'
      || (!session.sealed
        && !isImportSessionBrowserUploading(session.import_ref)
        && !['cancelling', 'cancelled'].includes(session.state)),
    canStop: ['admitting', 'running', 'paused'].includes(session.state)
      || preparationIncomplete,
  };
}

export interface ImportProgressController {
  activities: ImportActivity[];
  message: string | null;
  busyRef: string | null;
  preparingRef: string | null;
  resume(activity: ImportActivity): void;
  stop(activity: ImportActivity): Promise<void>;
  keep(activity: ImportActivity): void;
}

export function useImportProgress({ projectId, onChanged, onError }: {
  projectId: string;
  onChanged?(sheetId: number | null): void;
  onError?(message: string): void;
}): ImportProgressController {
  const [sessions, setSessions] = useState<ImportSessionStatus[]>([]);
  const [busyRef, setBusyRef] = useState<string | null>(null);
  const [preparingRef, setPreparingRef] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const browserUploadVersion = useSyncExternalStore(
    subscribeImportSessions,
    importSessionSnapshot,
    importSessionSnapshot,
  );
  const preparationGeneration = useRef(0);
  const previousRef = useRef<Map<string, string> | null>(null);

  useEffect(() => () => {
    preparationGeneration.current += 1;
  }, [projectId]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await listImportSessions(projectId, { signal });
      if (signal?.aborted) return;
      const next = new Map(result.sessions.map((session) => [
        session.import_ref,
        `${session.state}:${session.committed_rows}:${session.preparation_completed}:${session.ocr_completed}:${session.sheet_id ?? ''}`,
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

  const command = useCallback(async (
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
  }, [onChanged, onError]);

  const continueUpload = useCallback(async (session: ImportSessionStatus, files: File[]) => {
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
        await command(session, () => resumeImportSession(projectId, session.import_ref));
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

  const activities = sessions.flatMap((session) => {
    const activity = importActivity(session);
    return activity ? [activity] : [];
  });

  return {
    activities,
    message,
    busyRef,
    preparingRef,
    resume(activity) {
      if (!activity.session.sealed) {
        requestReselect(activity.session);
        return;
      }
      void command(activity.session, () => resumeImportSession(projectId, activity.importRef));
    },
    stop(activity) {
      abortImportSessionBrowserUpload(activity.importRef);
      if (preparingRef === activity.importRef) {
        preparationGeneration.current += 1;
        setPreparingRef(null);
      }
      return command(activity.session, async () => {
        const cancelled = await cancelImportSession(projectId, activity.importRef);
        return cancelled.state === 'cancelled'
          ? resolveImportSession(projectId, activity.importRef, 'keep')
          : cancelled;
      });
    },
    keep(activity) {
      void command(activity.session, () => (
        activity.session.state === 'cancelled'
          ? resolveImportSession(projectId, activity.importRef, 'keep')
          : cancelImportSession(projectId, activity.importRef).then((cancelled) => (
            cancelled.state === 'cancelled'
              ? resolveImportSession(projectId, activity.importRef, 'keep')
              : cancelled
          ))
      ));
    },
  };
}

export interface ImportSearchActivityLine {
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
  const readingOcr = ocrLine !== null && ocrLine.completed < ocrLine.total;
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
        : recovering ? 'Import interrupted' : 'Preparing search',
      detail: session.error ?? (recovering
        ? `${session.committed_rows.toLocaleString()} rows added`
        : readingOcr && ocrLine
          ? `Reading text with OCR · ${ocrLine.completed.toLocaleString()} of ${ocrLine.total.toLocaleString()} files`
          : `${readyFiles.toLocaleString()} of ${selectedTotal.toLocaleString()} files ready`),
      progress: null,
      details: search?.details ?? [],
      pdfFiles: search?.pdfFiles,
      pdfFilesTruncated: search?.pdfFilesTruncated,
    },
    lines,
  };
}
