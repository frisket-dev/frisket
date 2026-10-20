import { useCallback, useEffect, useRef, useState } from 'react';
import { useImportRequest } from '../useImportRequest';

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
  intent: {
    scope: string;
    projectId: string;
    draftId: string;
    fileId: string;
    page: number;
    selection: PdfImportOcrSelection;
  };
  signal: AbortSignal;
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
  const [startingRequest, setStartingRequest] = useState<{ controller: AbortController; scope: string } | null>(null);
  const [consent, setConsent] = useState<PendingPreviewConsent | null>(null);
  const selectedFileRef = useRef<string | null>(null);
  const pageRequest = useImportRequest(projectId);
  const intentRequest = useImportRequest(projectId);
  const inFlight = useRef<AbortController | null>(null);
  const draftId = snapshot?.draft_id ?? null;
  const selectedOcrKey = snapshot ? selectionKey(snapshot.choices.ocr_selection) : null;
  const scope = JSON.stringify([projectId, draftId, selectedOcrKey, selectedFileId]);
  const starting = Boolean(startingRequest?.scope === scope && !startingRequest.controller.signal.aborted);
  const activeConsent = consent?.intent.scope === scope && !consent.signal.aborted ? consent : null;

  useEffect(() => () => intentRequest.abort(), [intentRequest, scope]);

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
    const controller = pageRequest.begin();
    setPageLoading(true);
    try {
      const result = await api.getPage(
        projectId, snapshot.draft_id, file.occurrence_id, 1, { signal: controller.signal },
      );
      if (!controller.signal.aborted
        && selectedFileRef.current === result.occurrence_id
        && currentDraft() === result.draft_id) setPage(result);
    } catch (problem) {
      if (!controller.signal.aborted) reportError(problem);
    } finally {
      if (!controller.signal.aborted) setPageLoading(false);
    }
  }, [api, currentDraft, pageRequest, projectId, reportError, snapshot]);

  const select = useCallback(async (file: PdfImportFileSummary | null) => {
    selectedFileRef.current = file?.occurrence_id ?? null;
    setSelectedFileId(file?.occurrence_id ?? null);
    setTextSource('native');
    setPage(null);
    pageRequest.abort();
    if (file && file.category !== 'other' && file.category !== 'unreadable') await loadPage(file);
  }, [loadPage, pageRequest]);

  const clear = useCallback(() => {
    selectedFileRef.current = null;
    setSelectedFileId(null);
    setTextSource('native');
    setPage(null);
    setPageLoading(false);
    intentRequest.abort();
    setStartingRequest(null);
    setConsent(null);
    pageRequest.abort();
  }, [intentRequest, pageRequest]);

  useEffect(() => {
    if (previewJob?.status !== 'done' || cached || !chosenFile) return;
    queueMicrotask(() => void loadPage(chosenFile));
  }, [cached, chosenFile, loadPage, previewJob?.status]);

  const execute = useCallback(async (
    intent: PendingPreviewConsent['intent'],
    controller: AbortController,
    confirmation: string | undefined,
    refresh?: () => Promise<void>,
  ) => {
    await api.startOcrPreview(
      intent.projectId, intent.draftId, intent.fileId, intent.page,
      intent.selection, confirmation, { signal: controller.signal },
    );
    if (controller.signal.aborted || currentDraft() !== intent.draftId) return;
    setTextSource('ocr');
    await refresh?.();
  }, [api, currentDraft]);

  const start = useCallback(async (fileId: string, selectedPage = 1, refresh?: () => Promise<void>) => {
    if (!snapshot || (inFlight.current && !inFlight.current.signal.aborted)) return;
    const controller = intentRequest.begin();
    inFlight.current = controller;
    setStartingRequest({ controller, scope });
    setConsent(null);
    const intent: PendingPreviewConsent['intent'] = {
      scope, projectId, draftId: snapshot.draft_id, fileId, page: selectedPage,
      selection: { ...snapshot.choices.ocr_selection },
    };
    try {
      const estimate = await api.estimateOcrPreview(
        intent.projectId, intent.draftId, intent.fileId, intent.page, intent.selection,
        { signal: controller.signal },
      );
      if (controller.signal.aborted || currentDraft() !== intent.draftId) return;
      if (estimate.estimate?.requires_confirmation) {
        if (!estimate.estimate.promise_set_hash) throw new Error('The OCR estimate did not include an authorization token.');
        setConsent({ estimate, intent, signal: controller.signal });
        return;
      }
      if (estimate.cached) {
        const file = sampleFiles.find((item) => item.occurrence_id === fileId);
        if (file) await loadPage(file);
        if (!controller.signal.aborted) setTextSource('ocr');
        return;
      }
      await execute(intent, controller, undefined, refresh);
    } catch (problem) {
      if (!controller.signal.aborted) reportError(problem);
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
      setStartingRequest((current) => current?.controller === controller ? null : current);
    }
  }, [api, currentDraft, execute, intentRequest, loadPage, projectId, reportError, sampleFiles, scope, snapshot]);

  const cancelConsent = useCallback(() => {
    intentRequest.abort();
    setConsent(null);
  }, [intentRequest]);

  const confirm = useCallback(async (pending: PendingPreviewConsent, refresh?: () => Promise<void>) => {
    if (pending.signal.aborted || pending.intent.scope !== scope
      || currentDraft() !== pending.intent.draftId
      || (inFlight.current && !inFlight.current.signal.aborted)) return;
    const controller = intentRequest.begin();
    inFlight.current = controller;
    setConsent(null);
    setStartingRequest({ controller, scope });
    try {
      await execute(pending.intent, controller, pending.estimate.estimate?.promise_set_hash ?? undefined, refresh);
    } catch (problem) {
      if (!controller.signal.aborted) reportError(problem);
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
      setStartingRequest((current) => current?.controller === controller ? null : current);
    }
  }, [currentDraft, execute, intentRequest, reportError, scope]);

  return {
    cached,
    chosenFile,
    clear,
    consent: activeConsent,
    cancelConsent,
    confirm,
    page,
    pageLoading,
    previewJob,
    running,
    select,
    selectedFileId,
    setTextSource,
    start,
    textSource,
  };
}
