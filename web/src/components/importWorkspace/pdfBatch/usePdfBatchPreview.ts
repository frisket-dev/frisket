import { useCallback, useEffect, useRef, useState } from 'react';

import type {
  PdfImportFileSummary,
  PdfImportOcrSelection,
  PdfImportPage,
  PdfImportPreviewEstimate,
  PdfImportSnapshot,
  PdfImportsApi,
} from '../../../api/pdfImports';

interface PendingPreviewConsent {
  estimate: PdfImportPreviewEstimate;
  fileId: string;
  page: number;
}

function selectionKey(selection: PdfImportOcrSelection): string {
  return selection.kind === 'engine'
    ? `engine:${selection.engine}`
    : `engine_model:${selection.engine}:${selection.model ?? ''}`;
}

function actionParamsKey(params: Record<string, unknown> | null | undefined): string | null {
  if (!params || typeof params.engine !== 'string') return null;
  return Object.hasOwn(params, 'model')
    ? `engine_model:${params.engine}:${typeof params.model === 'string' ? params.model : ''}`
    : `engine:${params.engine}`;
}

function pageMatches(page: PdfImportPage | null, selection: PdfImportOcrSelection): boolean {
  return Boolean(page && page.ocr_text !== null
    && actionParamsKey(page.ocr_action_params) === selectionKey(selection));
}

export function usePdfBatchPreview({
  api,
  projectId,
  snapshot,
  sampleFiles,
  currentDraft,
  reportError,
}: {
  api: PdfImportsApi;
  projectId: string;
  snapshot: PdfImportSnapshot | null;
  sampleFiles: readonly PdfImportFileSummary[];
  currentDraft(): string | null;
  reportError(problem: unknown): void;
}) {
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [page, setPage] = useState<PdfImportPage | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [textSource, setTextSource] = useState<'native' | 'ocr'>('native');
  const [starting, setStarting] = useState(false);
  const [consent, setConsent] = useState<PendingPreviewConsent | null>(null);
  const selectedFileRef = useRef<string | null>(null);
  const requestEpoch = useRef(0);
  const pageController = useRef<AbortController | null>(null);

  const chosenFile = sampleFiles.find((file) => file.occurrence_id === selectedFileId) ?? null;
  const previewJob = snapshot?.preview_jobs?.find((job) => (
    job.occurrence_id === selectedFileId
    && job.page === 1
    && actionParamsKey(job.ocr_action_params) === selectionKey(snapshot.choices.ocr_selection)
  ));
  const cached = Boolean(snapshot && pageMatches(page, snapshot.choices.ocr_selection));
  const running = !cached && (starting || previewJob?.status === 'running');

  const loadPage = useCallback(async (file: PdfImportFileSummary) => {
    if (!snapshot) return;
    const epoch = ++requestEpoch.current;
    const controller = new AbortController();
    pageController.current?.abort();
    pageController.current = controller;
    setPageLoading(true);
    try {
      const result = await api.getPage(
        projectId, snapshot.draft_id, file.occurrence_id, 1, { signal: controller.signal },
      );
      if (!controller.signal.aborted
        && epoch === requestEpoch.current
        && selectedFileRef.current === result.occurrence_id
        && currentDraft() === result.draft_id) setPage(result);
    } catch (problem) {
      if (!controller.signal.aborted) reportError(problem);
    } finally {
      if (!controller.signal.aborted && epoch === requestEpoch.current) setPageLoading(false);
    }
  }, [api, currentDraft, projectId, reportError, snapshot]);

  const select = useCallback(async (file: PdfImportFileSummary | null) => {
    selectedFileRef.current = file?.occurrence_id ?? null;
    setSelectedFileId(file?.occurrence_id ?? null);
    setTextSource('native');
    setPage(null);
    requestEpoch.current += 1;
    pageController.current?.abort();
    if (file && file.category !== 'other' && file.category !== 'unreadable') await loadPage(file);
  }, [loadPage]);

  const clear = useCallback(() => {
    selectedFileRef.current = null;
    setSelectedFileId(null);
    setTextSource('native');
    setPage(null);
    setPageLoading(false);
    setStarting(false);
    setConsent(null);
    requestEpoch.current += 1;
    pageController.current?.abort();
  }, []);

  useEffect(() => {
    if (previewJob?.status !== 'done' || cached || !chosenFile) return;
    queueMicrotask(() => void loadPage(chosenFile));
  }, [cached, chosenFile, loadPage, previewJob?.status]);

  const start = useCallback(async (
    fileId: string,
    selectedPage = 1,
    confirmation?: string,
    refresh?: () => Promise<void>,
  ) => {
    if (!snapshot || starting) return;
    setStarting(true);
    try {
      if (!confirmation) {
        const estimate = await api.estimateOcrPreview(
          projectId, snapshot.draft_id, fileId, selectedPage, snapshot.choices.ocr_selection,
        );
        const token = estimate.estimate?.promise_set_hash;
        if (estimate.estimate?.requires_confirmation) {
          if (!token) throw new Error('The OCR estimate did not include an authorization token.');
          setConsent({ estimate, fileId, page: selectedPage });
          return;
        }
        if (estimate.cached) {
          const file = sampleFiles.find((item) => item.occurrence_id === fileId);
          if (file) await loadPage(file);
          setTextSource('ocr');
          return;
        }
      }
      await api.startOcrPreview(
        projectId, snapshot.draft_id, fileId, selectedPage,
        snapshot.choices.ocr_selection, confirmation,
      );
      setTextSource('ocr');
      await refresh?.();
    } catch (problem) {
      reportError(problem);
    } finally {
      setStarting(false);
    }
  }, [api, loadPage, projectId, reportError, sampleFiles, snapshot, starting]);

  return {
    cached,
    chosenFile,
    clear,
    consent,
    page,
    pageLoading,
    previewJob,
    running,
    select,
    selectedFileId,
    setConsent,
    setTextSource,
    start,
    textSource,
  };
}
