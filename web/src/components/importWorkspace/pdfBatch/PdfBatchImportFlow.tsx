import { AlertTriangle, LoaderCircle } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';

import {
  pdfImportsApi,
  PdfImportResponseError,
  type PdfImportCategory,
  type PdfImportChoices,
  type PdfImportDestination,
  type PdfImportFileSummary,
  type PdfImportPage,
  type PdfImportOcrSelection,
  type PdfImportPreviewEstimate,
  type PdfImportSnapshot,
  type PdfImportUpload,
  type PdfImportsApi,
} from '../../../api/pdfImports';
import { usePoll } from '../../../hooks/usePoll';
import { CostGateModal } from '../../CostGateModal';
import {
  PDF_BATCH_STAGES,
  bytesLabel,
  canHandlePdfBatch,
  count,
  destinationIsReady,
  newerSnapshot,
  ocrPolicyCount,
  visibleCategories,
  type PdfBatchStage,
} from './model';
import { AddFilesStep, CheckTextStep, DestinationStep, SearchSetupStep } from './PdfBatchSteps';
import './PdfBatchImportFlow.css';

export interface PdfBatchDestinationSheet {
  id: number;
  name: string;
}

export interface PdfBatchShellContext {
  active: true;
  stage: PdfBatchStage;
  stages: typeof PDF_BATCH_STAGES;
  subtitle: string;
  busy: boolean;
}

export interface PdfBatchImportFlowProps {
  projectId: string;
  files?: readonly File[];
  resumeDraftId?: string | null;
  destinationSheets: readonly PdfBatchDestinationSheet[];
  initialDestinationSheetId?: number | null;
  api?: PdfImportsApi;
  onContextChange(context: PdfBatchShellContext): void;
  registerCloseGuard(guard: (() => boolean) | null): void;
  onImported(sheetId: number): void;
  onDiscarded(): void;
  onError(message: string): void;
}

interface PendingPreviewConsent {
  estimate: PdfImportPreviewEstimate;
  fileId: string;
  page: number;
}

const CHECK_CATEGORY_ORDER: readonly PdfImportCategory[] = [
  'suspicious', 'missing', 'text', 'image', 'other', 'unreadable',
];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function ocrEngine(selection: PdfImportOcrSelection): string {
  return selection.engine;
}

function ocrSelectionKey(selection: PdfImportOcrSelection): string {
  return selection.kind === 'engine'
    ? `engine:${selection.engine}`
    : `engine_model:${selection.engine}:${selection.model ?? ''}`;
}

function ocrActionParamsKey(params: Record<string, unknown> | null | undefined): string | null {
  if (!params || typeof params.engine !== 'string') return null;
  return Object.hasOwn(params, 'model')
    ? `engine_model:${params.engine}:${typeof params.model === 'string' ? params.model : ''}`
    : `engine:${params.engine}`;
}

function pageMatchesSelection(page: PdfImportPage | null, selection: PdfImportOcrSelection): boolean {
  if (!page || page.ocr_text === null) return false;
  return ocrActionParamsKey(page.ocr_action_params) === ocrSelectionKey(selection);
}

function subtitle(snapshot: PdfImportSnapshot | null, files: readonly File[]): string {
  const total = snapshot?.upload.files_total ?? files.length;
  const totalBytes = snapshot?.upload.bytes_total
    ?? files.reduce((sum, file) => sum + file.size, 0);
  return `${count(total, 'PDF')} · ${bytesLabel(totalBytes)}`;
}

function selectedFolderName(files: readonly File[]): string | null {
  const logicalPath = files[0]?.webkitRelativePath;
  if (!logicalPath?.includes('/')) return null;
  return logicalPath.split('/')[0] || null;
}

export function PdfBatchImportFlow({
  projectId,
  files = [],
  resumeDraftId = null,
  destinationSheets,
  initialDestinationSheetId = null,
  api = pdfImportsApi,
  onContextChange,
  registerCloseGuard,
  onImported,
  onDiscarded,
  onError,
}: PdfBatchImportFlowProps) {
  const [snapshot, setSnapshot] = useState<PdfImportSnapshot | null>(null);
  const [stage, setStage] = useState<PdfBatchStage>('add');
  const [error, setError] = useState<string | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<PdfImportCategory>('suspicious');
  const [sampleFiles, setSampleFiles] = useState<PdfImportFileSummary[]>([]);
  const [samplesLoading, setSamplesLoading] = useState(false);
  const [sampleCursor, setSampleCursor] = useState<string | null>(null);
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [page, setPage] = useState<PdfImportPage | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [textSource, setTextSource] = useState<'native' | 'ocr'>('native');
  const [previewStarting, setPreviewStarting] = useState(false);
  const [previewConsent, setPreviewConsent] = useState<PendingPreviewConsent | null>(null);
  const [batchEstimateBusy, setBatchEstimateBusy] = useState(false);
  const [batchEstimateError, setBatchEstimateError] = useState<string | null>(null);
  const [discardPrompt, setDiscardPrompt] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [reselecting, setReselecting] = useState(false);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [destination, setDestination] = useState<PdfImportDestination>(() => (
    initialDestinationSheetId == null
      ? { kind: 'new', sheet_name: selectedFolderName(files) ?? 'Imported PDFs' }
      : { kind: 'existing', sheet_id: initialDestinationSheetId }
  ));
  const [semanticEnabled, setSemanticEnabled] = useState<boolean | null>(null);
  const uploadController = useRef<AbortController | null>(null);
  const intakeRun = useRef<Promise<void> | null>(null);
  const liveController = useRef<AbortController | null>(null);
  const refreshController = useRef<AbortController | null>(null);
  const batchEstimateInFlight = useRef<symbol | null>(null);
  const discardPause = useRef<Promise<void> | null>(null);
  const commitKey = useRef(crypto.randomUUID());
  const currentDraft = useRef<string | null>(null);
  const generation = useRef(0);
  const sampleRequestEpoch = useRef(0);
  const pageRequestEpoch = useRef(0);
  const selectedFileRef = useRef<string | null>(null);
  const ocrSelectionRef = useRef<string | null>(null);
  const importedSheet = useRef<number | null>(null);
  const filesProp = useRef(files);
  filesProp.current = files;
  const initialFiles = useRef<readonly File[]>([]);
  const recoveryFiles = useRef<File[]>([]);
  const fileInventory = useRef<Array<{
    file: File;
    fileId: string;
    logicalPath: string;
    batchId: string;
  }>>([]);
  const manifestBatchIds = useRef<string[]>([]);
  const committed = useRef(false);
  const onErrorRef = useRef(onError);
  const onImportedRef = useRef(onImported);
  const onDiscardedRef = useRef(onDiscarded);
  const initialDestinationRef = useRef(initialDestinationSheetId);
  onErrorRef.current = onError;
  onImportedRef.current = onImported;
  onDiscardedRef.current = onDiscarded;
  initialDestinationRef.current = initialDestinationSheetId;
  ocrSelectionRef.current = snapshot ? ocrSelectionKey(snapshot.choices.ocr_selection) : null;

  const publish = useCallback((next: PdfImportSnapshot) => {
    if (currentDraft.current && next.draft_id !== currentDraft.current) return;
    currentDraft.current = next.draft_id;
    setSnapshot((current) => newerSnapshot(current, next));
  }, []);

  const reportError = useCallback((problem: unknown) => {
    const message = errorMessage(problem);
    setError(message);
    onErrorRef.current(message);
  }, []);

  const runIntake = useCallback(async (
    task: (controller: AbortController) => Promise<void>,
  ) => {
    const prior = intakeRun.current;
    if (prior) {
      uploadController.current?.abort();
      await prior.catch(() => undefined);
    }
    const controller = new AbortController();
    uploadController.current = controller;
    const running = task(controller);
    intakeRun.current = running;
    try {
      await running;
    } finally {
      if (intakeRun.current === running) intakeRun.current = null;
      if (uploadController.current === controller) uploadController.current = null;
    }
  }, []);

  const stopIntake = useCallback(async () => {
    uploadController.current?.abort();
    const running = intakeRun.current;
    if (running) await running.catch(() => undefined);
  }, []);

  const runFreshIntake = useCallback(async (source: PdfImportSnapshot, epoch: number) => {
    await runIntake(async (controller) => {
      let latest = source;
      if (!latest.manifest.sealed) {
        for (let start = 0, index = 0; start < fileInventory.current.length; start += 256, index += 1) {
          const manifest = fileInventory.current.slice(start, start + 256).map((item) => ({
            client_file_id: item.fileId,
            logical_path: item.logicalPath,
            filename: item.file.name,
            size: item.file.size,
          }));
          latest = await api.addManifest(
            projectId,
            source.draft_id,
            manifest,
            manifestBatchIds.current[index],
            { signal: controller.signal },
          );
          if (generation.current === epoch) publish(latest);
        }
        latest = await api.sealManifest(projectId, source.draft_id, { signal: controller.signal });
        if (generation.current === epoch) publish(latest);
      }
      for (const item of fileInventory.current) {
        const uploaded = await api.upload(
          projectId,
          source.draft_id,
          [{ fileId: item.fileId, file: item.file, logicalPath: item.logicalPath }],
          item.batchId,
          { signal: controller.signal },
        );
        if (generation.current === epoch) publish(uploaded);
      }
    });
  }, [api, projectId, publish, runIntake]);

  useEffect(() => {
    const epoch = ++generation.current;
    const controller = new AbortController();
    liveController.current?.abort();
    liveController.current = controller;
    currentDraft.current = null;
    const selectedFiles = filesProp.current;
    const sourceFolder = selectedFolderName(selectedFiles);
    initialFiles.current = selectedFiles;
    fileInventory.current = selectedFiles.map((file) => ({
      file,
      fileId: crypto.randomUUID(),
      logicalPath: file.webkitRelativePath || file.name,
      batchId: crypto.randomUUID(),
    }));
    manifestBatchIds.current = Array.from(
      { length: Math.ceil(selectedFiles.length / 256) },
      () => crypto.randomUUID(),
    );
    selectedFileRef.current = null;
    batchEstimateInFlight.current = null;
    sampleRequestEpoch.current += 1;
    pageRequestEpoch.current += 1;
    recoveryFiles.current = [];
    commitKey.current = crypto.randomUUID();
    importedSheet.current = null;
    committed.current = false;
    const start = async () => {
      await Promise.resolve();
      if (controller.signal.aborted || generation.current !== epoch) return;
      setSnapshot(null);
      setStage('add');
      setError(null);
      setSelectedCategory('suspicious');
      setSampleFiles([]);
      setSamplesLoading(false);
      setSampleCursor(null);
      setSelectedFileId(null);
      setPage(null);
      setPageLoading(false);
      setTextSource('native');
      setPreviewStarting(false);
      setPreviewConsent(null);
      setBatchEstimateBusy(false);
      setBatchEstimateError(null);
      setDiscardPrompt(false);
      setDiscarding(false);
      setReselecting(false);
      setMutationBusy(false);
      setDestination(initialDestinationRef.current == null
        ? { kind: 'new', sheet_name: sourceFolder ?? 'Imported PDFs' }
        : { kind: 'existing', sheet_id: initialDestinationRef.current });
      setSemanticEnabled(null);
      if (!resumeDraftId) {
        const invalid = !canHandlePdfBatch(initialFiles.current);
        if (invalid) throw new Error('Choose at least two PDFs for this import.');
      }
      const created = resumeDraftId
        ? await api.get(projectId, resumeDraftId, { signal: controller.signal })
        : await api.create(
          projectId,
          fileInventory.current.length,
          fileInventory.current.reduce((sum, item) => sum + item.file.size, 0),
          sourceFolder
            ? sourceFolder
            : `${fileInventory.current.length} PDFs`,
          { signal: controller.signal },
        );
      if (controller.signal.aborted || generation.current !== epoch) return;
      if (resumeDraftId) setDestination(created.choices.destination);
      publish(created);
      if (!resumeDraftId) {
        await runFreshIntake(created, epoch);
      }
    };
    void start().catch((problem) => {
      if (!controller.signal.aborted && !(problem instanceof DOMException && problem.name === 'AbortError')) {
        reportError(problem);
      }
    });
    return () => {
      controller.abort();
      refreshController.current?.abort();
      if (generation.current === epoch) generation.current += 1;
      if (!committed.current) uploadController.current?.abort();
    };
  }, [api, projectId, publish, reportError, resumeDraftId, runFreshIntake]);

  const refresh = useCallback(async () => {
    const draftId = currentDraft.current;
    if (!draftId) return;
    const epoch = generation.current;
    const controller = new AbortController();
    refreshController.current?.abort();
    refreshController.current = controller;
    try {
      const next = await api.get(projectId, draftId, { signal: controller.signal });
      if (controller.signal.aborted || generation.current !== epoch || currentDraft.current !== draftId) return;
      publish(next);
      if (next.state === 'cancelled') onDiscardedRef.current();
      const sheetId = next.commit?.result?.sheet_id;
      if (next.state === 'committed' && sheetId && importedSheet.current !== sheetId) {
        committed.current = true;
        importedSheet.current = sheetId;
        onImportedRef.current(sheetId);
      }
    } catch (problem) {
      if (!controller.signal.aborted && generation.current === epoch) reportError(problem);
    } finally {
      if (refreshController.current === controller) refreshController.current = null;
    }
  }, [api, projectId, publish, reportError]);

  usePoll(refresh, {
    active: Boolean(snapshot && (
      snapshot.state === 'committing'
      || snapshot.state === 'cancelling'
      || (!snapshot.paused && snapshot.state === 'open'
        && (!snapshot.upload.sealed || snapshot.analysis.files_done < (snapshot.analysis.files_total ?? Infinity)))
      || snapshot.preview_jobs?.some((job) => (
        job.occurrence_id === selectedFileId
        && job.page === 1
        && ocrActionParamsKey(job.ocr_action_params) === ocrSelectionKey(snapshot.choices.ocr_selection)
        && (job.status === 'queued' || job.status === 'running')
        && !pageMatchesSelection(page, snapshot.choices.ocr_selection)
      ))
    )),
    intervalMs: 1_200,
    guardOverlap: true,
  });

  const chosenFile = sampleFiles.find((file) => file.occurrence_id === selectedFileId) ?? null;
  const previewJob = snapshot?.preview_jobs?.find((job) => (
    job.occurrence_id === selectedFileId
    && job.page === 1
    && ocrActionParamsKey(job.ocr_action_params) === ocrSelectionKey(snapshot.choices.ocr_selection)
  ));
  const previewCached = Boolean(snapshot && pageMatchesSelection(page, snapshot.choices.ocr_selection));

  const loadPage = useCallback(async (file: PdfImportFileSummary) => {
    if (!snapshot) return;
    const requestEpoch = ++pageRequestEpoch.current;
    const controller = new AbortController();
    liveController.current?.abort();
    liveController.current = controller;
    setPageLoading(true);
    try {
      const result = await api.getPage(projectId, snapshot.draft_id, file.occurrence_id, 1, { signal: controller.signal });
      if (!controller.signal.aborted
        && requestEpoch === pageRequestEpoch.current
        && selectedFileRef.current === result.occurrence_id
        && currentDraft.current === (result.draft_id || result.import_id)) setPage(result);
    } catch (problem) {
      if (!controller.signal.aborted) reportError(problem);
    } finally {
      if (!controller.signal.aborted && requestEpoch === pageRequestEpoch.current) setPageLoading(false);
    }
  }, [api, projectId, reportError, snapshot]);

  usePoll(async () => {
    if (!snapshot || !chosenFile) return 'stop';
    const result = await api.getPage(projectId, snapshot.draft_id, chosenFile.occurrence_id, 1);
    if (currentDraft.current !== result.draft_id
      || selectedFileRef.current !== result.occurrence_id
      || result.occurrence_id !== chosenFile.occurrence_id
      || ocrSelectionRef.current !== ocrSelectionKey(snapshot.choices.ocr_selection)) return 'stop';
    setPage(result);
    return pageMatchesSelection(result, snapshot.choices.ocr_selection) ? 'stop' : undefined;
  }, {
    active: Boolean(previewJob && (previewJob.status === 'queued' || previewJob.status === 'running')
      && !previewCached),
    intervalMs: 700,
    guardOverlap: true,
    immediate: true,
    mode: 'settle-relative',
  });

  useEffect(() => {
    if (!snapshot) return;
    onContextChange({
      active: true,
      stage,
      stages: PDF_BATCH_STAGES,
      subtitle: subtitle(snapshot, initialFiles.current),
      busy: mutationBusy || snapshot.state === 'committing' || snapshot.state === 'cancelling',
    });
  }, [mutationBusy, onContextChange, snapshot, stage]);

  const pauseForDiscard = useCallback(() => {
    if (!snapshot || snapshot.state !== 'open' || discardPrompt) return;
    setDiscardPrompt(true);
    const request = (async () => {
      await stopIntake();
      publish(await api.pause(projectId, snapshot.draft_id));
    })().catch(reportError);
    discardPause.current = request;
  }, [api, discardPrompt, projectId, publish, reportError, snapshot, stopIntake]);

  useEffect(() => {
    registerCloseGuard(snapshot && snapshot.state === 'open' ? () => {
      pauseForDiscard();
      return false;
    } : null);
    return () => registerCloseGuard(null);
  }, [pauseForDiscard, registerCloseGuard, snapshot]);

  const updateChoicesWithRetry = async (
    patch: Partial<PdfImportChoices>,
    nextDestination = destination,
  ): Promise<PdfImportSnapshot> => {
    if (!snapshot) throw new Error('The PDF import is not ready.');
    try {
      return await api.updateChoices(projectId, snapshot.draft_id, {
        ...snapshot.choices,
        ...patch,
        destination: nextDestination,
        expected_revision: snapshot.revision,
      });
    } catch (problem) {
      if (!(problem instanceof PdfImportResponseError) || problem.status !== 409) throw problem;
      const latest = await api.get(projectId, snapshot.draft_id);
      publish(latest);
      return api.updateChoices(projectId, latest.draft_id, {
        ...latest.choices,
        ...patch,
        destination: nextDestination,
        expected_revision: latest.revision,
      });
    }
  };

  const mutateChoices = async (patch: Partial<PdfImportChoices>) => {
    if (!snapshot || mutationBusy) return;
    setMutationBusy(true);
    setError(null);
    try {
      publish(await updateChoicesWithRetry(patch));
    } catch (problem) {
      reportError(problem);
    } finally {
      setMutationBusy(false);
    }
  };

  const loadSamples = useCallback(async (category: PdfImportCategory, cursor?: string) => {
    if (!snapshot) return;
    const requestEpoch = ++sampleRequestEpoch.current;
    setSamplesLoading(true);
    try {
      const result = await api.samples(projectId, snapshot.draft_id, category, cursor);
      if (requestEpoch !== sampleRequestEpoch.current || currentDraft.current !== snapshot.draft_id) return;
      setSampleFiles(result.items);
      setSampleCursor(result.next_cursor);
      const first = result.items[0] ?? null;
      selectedFileRef.current = first?.occurrence_id ?? null;
      setSelectedFileId(first?.occurrence_id ?? null);
      setTextSource('native');
      setPage(null);
      if (first && first.category !== 'other' && first.category !== 'unreadable') await loadPage(first);
    } catch (problem) {
      if (requestEpoch === sampleRequestEpoch.current) reportError(problem);
    } finally {
      if (requestEpoch === sampleRequestEpoch.current) setSamplesLoading(false);
    }
  }, [api, loadPage, projectId, reportError, snapshot]);

  const chooseCategory = (category: PdfImportCategory) => {
    setSelectedCategory(category);
    setSampleFiles([]);
    selectedFileRef.current = null;
    setSelectedFileId(null);
    pageRequestEpoch.current += 1;
    liveController.current?.abort();
    void loadSamples(category);
  };

  const selectSample = (file: PdfImportFileSummary) => {
    selectedFileRef.current = file.occurrence_id;
    setSelectedFileId(file.occurrence_id);
    setTextSource('native');
    setPage(null);
    if (file.category !== 'other' && file.category !== 'unreadable') void loadPage(file);
  };

  const reconcileFiles = async (selected: readonly File[], source = snapshot) => {
    if (!source || reselecting) return;
    const epoch = generation.current;
    const isCurrent = () => generation.current === epoch && currentDraft.current === source.draft_id;
    const pdfs = selected.filter((file) => file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'));
    if (pdfs.length === 0) {
      reportError(new Error('Select the original PDF files or folder.'));
      return;
    }
    recoveryFiles.current = pdfs;
    setReselecting(true);
    setError(null);
    try {
      await runIntake(async (controller) => {
        if (source.paused) {
          const resumed = await api.resume(projectId, source.draft_id, { signal: controller.signal });
          if (isCurrent()) publish(resumed);
        }
        const pending: PdfImportUpload[] = [];
        for (let start = 0; start < pdfs.length; start += 256) {
          const batch = pdfs.slice(start, start + 256);
          const facts = batch.map((file) => ({
            logical_path: file.webkitRelativePath || file.name,
            filename: file.name,
            size: file.size,
          }));
          const matched = await api.reconcileReselection(
            projectId, source.draft_id, facts, { signal: controller.signal },
          );
          matched.items.forEach((item, index) => {
            if (item.matched && item.upload_needed && item.occurrence_id) {
              pending.push({ fileId: item.occurrence_id, file: batch[index], logicalPath: item.logical_path });
            }
          });
        }
        for (const item of pending) {
          const uploaded = await api.upload(
            projectId,
            source.draft_id,
            [item],
            crypto.randomUUID(),
            { signal: controller.signal },
          );
          if (isCurrent()) publish(uploaded);
        }
        if (isCurrent()) await refresh();
      });
    } catch (problem) {
      if (!(problem instanceof DOMException && problem.name === 'AbortError') && isCurrent()) reportError(problem);
    } finally {
      if (generation.current === epoch) setReselecting(false);
    }
  };

  const pauseCopying = async () => {
    if (!snapshot) return;
    try {
      await stopIntake();
      publish(await api.pause(projectId, snapshot.draft_id));
    } catch (problem) {
      reportError(problem);
    }
  };

  const resumeCopying = async () => {
    if (!snapshot) return;
    const retained = resumeDraftId ? recoveryFiles.current : initialFiles.current;
    try {
      await stopIntake();
      const resumed = await api.resume(projectId, snapshot.draft_id);
      publish(resumed);
      if (!resumeDraftId) await runFreshIntake(resumed, generation.current);
      else if (retained.length > 0) await reconcileFiles(retained, resumed);
    } catch (problem) {
      reportError(problem);
    }
  };

  const retryPreflight = async () => {
    if (!snapshot || mutationBusy) return;
    setMutationBusy(true);
    setError(null);
    try {
      if (resumeDraftId) {
        publish(await api.sealManifest(projectId, snapshot.draft_id));
      } else {
        await runFreshIntake(snapshot, generation.current);
      }
    } catch (problem) {
      reportError(problem);
      await refresh();
    } finally {
      setMutationBusy(false);
    }
  };

  const startPreview = async (fileId: string, selectedPage = 1, confirmation?: string) => {
    if (!snapshot || previewStarting) return;
    setPreviewStarting(true);
    setError(null);
    try {
      if (!confirmation) {
        const estimate = await api.estimateOcrPreview(
          projectId, snapshot.draft_id, fileId, selectedPage, snapshot.choices.ocr_selection,
        );
        const token = estimate.estimate?.promise_set_hash;
        if (estimate.estimate?.requires_confirmation) {
          if (!token) throw new Error('The OCR estimate did not include an authorization token.');
          setPreviewConsent({ estimate, fileId, page: selectedPage });
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
      await refresh();
    } catch (problem) {
      reportError(problem);
    } finally {
      setPreviewStarting(false);
    }
  };

  const compareDestination = async (next: PdfImportDestination) => {
    if (!snapshot || mutationBusy) return;
    setDestination(next);
    setError(null);
    setMutationBusy(true);
    try {
      const updated = await updateChoicesWithRetry({}, next);
      publish(updated);
      if (next.kind === 'existing') publish(await api.match(projectId, snapshot.draft_id));
    } catch (problem) {
      reportError(problem);
    } finally {
      setMutationBusy(false);
    }
  };

  const estimateBatchOcr = useCallback(async (source: PdfImportSnapshot) => {
    if (batchEstimateInFlight.current) return;
    if (ocrEngine(source.choices.ocr_selection) === 'rapidocr') {
      setBatchEstimateError(null);
      return;
    }
    if (ocrPolicyCount(source, source.choices.ocr_policy) === 0) {
      setBatchEstimateError(null);
      return;
    }
    const request = Symbol('batch OCR estimate');
    batchEstimateInFlight.current = request;
    setBatchEstimateBusy(true);
    setBatchEstimateError(null);
    setError(null);
    try {
      await api.estimateBatchOcr(
        projectId,
        source.draft_id,
        source.choices.ocr_selection,
        source.choices.ocr_policy,
        source.revision,
      );
      if (currentDraft.current !== source.draft_id || batchEstimateInFlight.current !== request) return;
      await refresh();
    } catch (problem) {
      if (currentDraft.current !== source.draft_id || batchEstimateInFlight.current !== request) return;
      setBatchEstimateError(errorMessage(problem));
      reportError(problem);
    } finally {
      if (batchEstimateInFlight.current === request) {
        batchEstimateInFlight.current = null;
        setBatchEstimateBusy(false);
      }
    }
  }, [api, projectId, refresh, reportError]);

  const batchEstimateInputsReady = Boolean(snapshot
    && snapshot.upload.files_done >= snapshot.upload.files_total
    && snapshot.analysis.files_done >= snapshot.analysis.files_total);

  useEffect(() => {
    if (stage !== 'search' || !snapshot || !batchEstimateInputsReady
      || ocrEngine(snapshot.choices.ocr_selection) === 'rapidocr'
      || ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) === 0
      || snapshot.batch_ocr?.state !== 'idle'
      || batchEstimateBusy || batchEstimateError) return;
    queueMicrotask(() => {
      if (stage === 'search' && currentDraft.current === snapshot.draft_id) {
        void estimateBatchOcr(snapshot);
      }
    });
  }, [batchEstimateBusy, batchEstimateError, batchEstimateInputsReady, estimateBatchOcr, snapshot, stage]);

  const continueToSearch = async () => {
    if (!snapshot || mutationBusy) return;
    setMutationBusy(true);
    setError(null);
    try {
      const saved = await updateChoicesWithRetry({}, destination);
      publish(saved);
      const refreshed = await api.refreshSemanticSetup(projectId, saved.draft_id);
      publish(refreshed);
      setStage('search');
    } catch (problem) {
      reportError(problem);
    } finally {
      setMutationBusy(false);
    }
  };

  const confirmDiscard = async () => {
    if (!snapshot || discarding) return;
    setDiscardPrompt(false);
    setDiscarding(true);
    uploadController.current?.abort();
    setError(null);
    try {
      await discardPause.current;
      publish(await api.discard(projectId, snapshot.draft_id));
      await refresh();
    } catch (problem) {
      reportError(problem);
    } finally {
      setDiscarding(false);
    }
  };

  const keepDraft = async () => {
    if (!snapshot) return;
    setDiscardPrompt(false);
    try {
      await discardPause.current;
      discardPause.current = null;
      await resumeCopying();
    } catch (problem) {
      reportError(problem);
    }
  };

  const commit = async () => {
    if (!snapshot || mutationBusy || !destinationIsReady(snapshot, destination)) return;
    const semanticSetup = snapshot.semantic_setup;
    if ((semanticEnabled ?? snapshot.choices.semantic_search)
      && semanticSetup.needs_confirmation
      && snapshot.choices.semantic_setup_confirmation !== semanticSetup.confirmation_hash) return;
    setMutationBusy(true);
    setError(null);
    try {
      const accepted = await api.commit(projectId, snapshot.draft_id, {
        idempotency_key: commitKey.current,
        expected_revision: snapshot.revision,
      });
      committed.current = true;
      const sheetId = accepted.job?.sheet_id;
      if (sheetId) {
        importedSheet.current = sheetId;
        onImported(sheetId);
      }
      await refresh();
    } catch (problem) {
      if (!(problem instanceof PdfImportResponseError)) committed.current = true;
      reportError(problem);
      void refresh();
    } finally {
      setMutationBusy(false);
    }
  };

  if (!snapshot) {
    return <div className="pdf-batch-loading" role="status"><LoaderCircle className="spin" size={18} /> Preparing PDF import…</div>;
  }

  if (snapshot.state === 'cancelling' || snapshot.state === 'cancelled') {
    const total = snapshot.cleanup.bytes_total ?? snapshot.upload.bytes_done ?? 0;
    const done = snapshot.cleanup.bytes_done ?? (snapshot.state === 'cancelled' ? total : 0);
    return (
      <section className="pdf-batch-cancelling" data-testid="pdf-batch-cancelling">
        {snapshot.state === 'cancelling' && <LoaderCircle className="spin" size={22} aria-hidden />}
        <h2>Cancelling import</h2>
        <p>Removing copied files · {bytesLabel(done)} of {bytesLabel(total)}</p>
        <progress max={Math.max(1, total)} value={done} />
        <span>Nothing was added to your project. This closes by itself when done.</span>
        <button type="button" className="btn" onClick={onDiscarded}>Close</button>
      </section>
    );
  }

  const categories = visibleCategories(snapshot);
  const samples = sampleFiles;
  const checkRunning = snapshot.analysis.files_done < (snapshot.analysis.files_total ?? Infinity);
  const totalFiles = snapshot.upload.files_total ?? files.length;
  const semanticSearch = semanticEnabled ?? snapshot.choices.semantic_search;
  const semanticNeedsConsent = semanticSearch && snapshot.semantic_setup.needs_confirmation;
  const semanticAuthorized = !semanticNeedsConsent || (
    snapshot.choices.semantic_setup_confirmation === snapshot.semantic_setup.confirmation_hash
  );
  const matching = destination.kind === 'existing' && snapshot.match.state === 'running';
  const nothingNew = destination.kind === 'existing'
    && snapshot.match.state === 'ready' && snapshot.match.new_count === 0;
  const canCommit = destinationIsReady(snapshot, destination)
    && !matching && semanticAuthorized
    && snapshot.analysis.files_done >= snapshot.upload.files_done
    && (ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) === 0
      || ocrEngine(snapshot.choices.ocr_selection) === 'rapidocr'
      || (snapshot.batch_ocr?.state === 'ready'
        && snapshot.batch_ocr.advisory !== false))
    && snapshot.state === 'open' && !mutationBusy;

  return (
    <>
      <div className="pdf-batch-flow" data-stage={stage} data-testid="pdf-batch-flow">
        {stage === 'add' && (
          <AddFilesStep
            snapshot={snapshot}
            totalFiles={totalFiles}
            needsReselection={Boolean(resumeDraftId && recoveryFiles.current.length === 0 && snapshot.upload.files_done < snapshot.upload.files_total)}
            reselecting={reselecting}
            retryingPreflight={mutationBusy}
            onReselect={(selected) => void reconcileFiles(selected)}
            onPause={() => void (snapshot.paused ? resumeCopying() : pauseCopying())}
            onRetryPreflight={() => void retryPreflight()}
            onCancel={pauseForDiscard}
            onContinue={() => {
              const category = CHECK_CATEGORY_ORDER.find((item) => (snapshot.category_counts[item] ?? 0) > 0);
              if (category) chooseCategory(category);
              setStage('check');
            }}
          />
        )}
        {stage === 'check' && (
          <CheckTextStep
            projectId={projectId}
            snapshot={snapshot}
            categories={categories}
            selectedCategory={selectedCategory}
            samples={samples}
            samplesLoading={samplesLoading}
            selectedFileId={selectedFileId}
            chosenFile={chosenFile}
            page={page}
            pageLoading={pageLoading}
            textSource={textSource}
            previewRunning={!previewCached && (previewStarting || previewJob?.status === 'queued' || previewJob?.status === 'running')}
            checkRunning={checkRunning}
            onCategory={chooseCategory}
            onSample={selectSample}
            onResample={() => {
              void loadSamples(selectedCategory, sampleCursor ?? undefined);
            }}
            onTextSource={setTextSource}
            onPreview={() => chosenFile && void startPreview(chosenFile.occurrence_id)}
            onOcrSelection={(ocrSelection) => void mutateChoices({ ocr_selection: ocrSelection })}
            onPolicy={(ocrPolicy) => {
              setBatchEstimateError(null);
              void mutateChoices({ ocr_policy: ocrPolicy });
            }}
            onBack={() => setStage('add')}
            onContinue={() => {
              setStage('destination');
              if (destination.kind === 'existing') void compareDestination(destination);
            }}
          />
        )}
        {stage === 'destination' && (
          <DestinationStep
            snapshot={snapshot}
            destination={destination}
            sheets={destinationSheets}
            busy={mutationBusy}
            onDestination={(next) => {
              if (next.kind === 'existing') void compareDestination(next);
              else setDestination(next);
            }}
            projectId={projectId}
            onBack={() => setStage('check')}
            onContinue={() => void continueToSearch()}
          />
        )}
        {stage === 'search' && (
          <SearchSetupStep
            snapshot={snapshot}
            semanticEnabled={semanticSearch}
            consent={snapshot.choices.semantic_setup_confirmation === snapshot.semantic_setup.confirmation_hash}
            busy={mutationBusy}
            importLabel={nothingNew ? 'Nothing new to import' : matching ? 'Comparing…' : `Import ${count(
              destination.kind === 'existing' && snapshot.match.state === 'ready'
                ? snapshot.match.new_count : totalFiles,
              'PDF', 'PDFs',
            )}`}
            canImport={canCommit}
            batchEstimateBusy={batchEstimateBusy}
            batchEstimateWaiting={!batchEstimateInputsReady}
            batchEstimateError={batchEstimateError}
            onEstimateOcr={() => {
              void estimateBatchOcr(snapshot);
            }}
            onSemantic={(enabled) => {
              setSemanticEnabled(enabled);
              if (!enabled || !snapshot.semantic_setup.needs_confirmation) {
                void mutateChoices({ semantic_search: enabled, semantic_setup_confirmation: null });
              }
            }}
            onConsent={(consent) => {
              if (consent) void mutateChoices({
                semantic_search: true,
                semantic_setup_confirmation: snapshot.semantic_setup.confirmation_hash,
              });
              else {
                setSemanticEnabled(false);
                void mutateChoices({ semantic_search: false, semantic_setup_confirmation: null });
              }
            }}
            onBack={() => setStage('destination')}
            onImport={() => void commit()}
          />
        )}
        {error && <div className="pdf-batch-error" role="alert">{error}</div>}
      </div>
      {discardPrompt && (
        <div className="modal-backdrop" data-testid="pdf-batch-discard-dialog">
          <form className="modal-card" onSubmit={(event) => { event.preventDefault(); void confirmDiscard(); }}>
            <div className="modal-title"><AlertTriangle size={15} className="modal-warn-icon" /> Discard this import?</div>
            <p className="modal-body-text">Copied files and your choices will be removed. Nothing has been added to the project.</p>
            <div className="form-actions">
              <button type="button" className="btn" onClick={() => void keepDraft()}>Keep working</button>
              <button type="submit" className="btn btn-danger" disabled={discarding}>Discard import</button>
            </div>
          </form>
        </div>
      )}
      {previewConsent?.estimate.estimate && (
        <CostGateModal
          estimate={previewConsent.estimate.estimate}
          message={`Apply ${ocrEngine(snapshot.choices.ocr_selection)} OCR to this page?`}
          onCancel={() => setPreviewConsent(null)}
          onConfirm={() => {
            const pending = previewConsent;
            setPreviewConsent(null);
            void startPreview(pending.fileId, pending.page, pending.estimate.estimate?.promise_set_hash);
          }}
        />
      )}
    </>
  );
}
