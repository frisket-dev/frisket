import { AlertTriangle, LoaderCircle } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useMemo,
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
  type PdfImportOcrSelection,
  type PdfImportSnapshot,
  type PdfImportsApi,
} from '../../../api/pdfImports';
import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse, SelectorChoice } from '../../../api/selectorChoices';
import type { RunEstimate } from '../../../api/open';
import { useSelectorChoices } from '../../../engine-selector/useSelectorChoices';
import { usePoll } from '../../../hooks/usePoll';
import { CostGateModal } from '../../CostGateModal';
import {
  registerImportSessionUploadAbort,
  rememberImportSessionFiles,
  setImportSessionBrowserUploading,
} from '../../importProgressSession';
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
import { usePdfBatchIntake } from './usePdfBatchIntake';
import { usePdfBatchPreview } from './usePdfBatchPreview';
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

const CHECK_CATEGORY_ORDER: readonly PdfImportCategory[] = [
  'suspicious', 'missing', 'text', 'image', 'other', 'unreadable',
];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function ocrEngine(selection: PdfImportOcrSelection): string {
  return selection.engine;
}

function selectorChoice(response: HttpSelectorChoicesResponse | null): SelectorChoice | null {
  if (!response) return null;
  const choiceId = response.current_choice_id ?? response.default_choice_id;
  if (!choiceId) return null;
  for (const group of response.groups) {
    const choice = group.choices.find((candidate) => candidate.choice_id === choiceId);
    if (choice) return choice;
  }
  return response.orphaned_current?.choice_id === choiceId ? response.orphaned_current : null;
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

interface PendingBatchOcrConsent {
  draftId: string;
  confirmation: string;
  estimate: RunEstimate;
  selectedCount: number;
  engine: string;
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
  const [batchEstimateBusy, setBatchEstimateBusy] = useState(false);
  const [batchEstimateError, setBatchEstimateError] = useState<string | null>(null);
  const [batchOcrConsent, setBatchOcrConsent] = useState<PendingBatchOcrConsent | null>(null);
  const [discardPrompt, setDiscardPrompt] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [reselecting, setReselecting] = useState(false);
  const [hasRecoveryFiles, setHasRecoveryFiles] = useState(false);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [destination, setDestination] = useState<PdfImportDestination>(() => (
    initialDestinationSheetId == null
      ? { kind: 'new', sheet_name: selectedFolderName(files) ?? 'Imported PDFs' }
      : { kind: 'existing', sheet_id: initialDestinationSheetId }
  ));
  const [semanticEnabled, setSemanticEnabled] = useState<boolean | null>(null);
  const [reportedOcrChoice, setReportedOcrChoice] = useState<SelectorChoice | null>(null);
  const lifecycleController = useRef<AbortController | null>(null);
  const refreshController = useRef<AbortController | null>(null);
  const batchEstimateInFlight = useRef<symbol | null>(null);
  const discardPause = useRef<Promise<void> | null>(null);
  const commitKey = useRef(crypto.randomUUID());
  const currentDraft = useRef<string | null>(null);
  const generation = useRef(0);
  const sampleRequestEpoch = useRef(0);
  const importedSheet = useRef<number | null>(null);
  const registeredUpload = useRef<string | null>(null);
  const filesProp = useRef(files);
  const recoveryFiles = useRef<File[]>([]);
  const committed = useRef(false);
  const onErrorRef = useRef(onError);
  const onImportedRef = useRef(onImported);
  const onDiscardedRef = useRef(onDiscarded);
  const initialDestinationRef = useRef(initialDestinationSheetId);

  useEffect(() => {
    filesProp.current = files;
    onErrorRef.current = onError;
    onImportedRef.current = onImported;
    onDiscardedRef.current = onDiscarded;
    initialDestinationRef.current = initialDestinationSheetId;
  }, [files, initialDestinationSheetId, onDiscarded, onError, onImported]);

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
  const getCurrentDraft = useCallback(() => currentDraft.current, []);
  const preview = usePdfBatchPreview({
    api,
    projectId,
    snapshot,
    sampleFiles,
    currentDraft: getCurrentDraft,
    reportError,
  });
  const intake = usePdfBatchIntake({ api, projectId, publish });
  const {
    active: activeIntake,
    abort: abortIntake,
    configure: configureIntake,
    files: intakeFiles,
    runFresh: runFreshIntake,
    runReselection,
    stop: stopIntake,
  } = intake;
  const {
    cached: previewCached,
    chosenFile,
    clear: clearPreview,
    consent: previewConsent,
    page: previewPage,
    pageLoading,
    previewJob,
    running: previewRunning,
    select: selectPreview,
    selectedFileId,
    setConsent: setPreviewConsent,
    setTextSource,
    start: startPreview,
    textSource,
  } = preview;

  const selectedOcr = snapshot?.choices.ocr_selection
    ?? ({ kind: 'engine', engine: 'rapidocr' } as const);
  const selectedOcrModel = selectedOcr.kind === 'engine_model' ? selectedOcr.model : null;
  const ocrSelectorQuery = useMemo<HttpSelectorChoicesQuery>(() => ({
    schema_version: 'frisket.selector_choices_query.v1',
    subject: {
      kind: 'action',
      action_id: 'media.ocr',
      field: 'engine',
      params: {
        engine: selectedOcr.engine,
        ...(selectedOcr.kind === 'engine_model' ? { model: selectedOcrModel } : {}),
      },
    },
  }), [selectedOcr.engine, selectedOcr.kind, selectedOcrModel]);
  const ocrAvailability = useSelectorChoices({
    projectId,
    query: ocrSelectorQuery,
    queryKey: JSON.stringify(ocrSelectorQuery.subject),
    enabled: Boolean(snapshot),
  });
  const loadedOcrChoice = ocrAvailability.stale || ocrAvailability.error
    ? null
    : selectorChoice(ocrAvailability.response);
  const ocrChoice = reportedOcrChoice ?? loadedOcrChoice;
  const ocrAvailabilityChecking = !ocrChoice && !ocrAvailability.error
    && (ocrAvailability.loading || ocrAvailability.refreshing || !ocrAvailability.response);

  useEffect(() => {
    const epoch = ++generation.current;
    const controller = new AbortController();
    lifecycleController.current?.abort();
    lifecycleController.current = controller;
    currentDraft.current = null;
    const selectedFiles = filesProp.current;
    const sourceFolder = selectedFolderName(selectedFiles);
    const intakeSelection = configureIntake(selectedFiles);
    batchEstimateInFlight.current = null;
    sampleRequestEpoch.current += 1;
    recoveryFiles.current = [];
    commitKey.current = crypto.randomUUID();
    importedSheet.current = null;
    registeredUpload.current = null;
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
      clearPreview();
      setBatchEstimateBusy(false);
      setBatchEstimateError(null);
      setBatchOcrConsent(null);
      setDiscardPrompt(false);
      setDiscarding(false);
      setReselecting(false);
      setHasRecoveryFiles(false);
      setMutationBusy(false);
      setDestination(initialDestinationRef.current == null
        ? { kind: 'new', sheet_name: sourceFolder ?? 'Imported PDFs' }
        : { kind: 'existing', sheet_id: initialDestinationRef.current });
      setSemanticEnabled(null);
      setReportedOcrChoice(null);
      if (!resumeDraftId) {
        const invalid = !canHandlePdfBatch(intakeSelection.files);
        if (invalid) throw new Error('Choose at least two PDFs for this import.');
      }
      const created = resumeDraftId
        ? await api.get(projectId, resumeDraftId, { signal: controller.signal })
        : await api.create(
          projectId,
          intakeSelection.files.length,
          intakeSelection.expectedBytes,
          intakeSelection.sourceLabel,
          { signal: controller.signal },
        );
      if (controller.signal.aborted || generation.current !== epoch) return;
      if (resumeDraftId) setDestination(created.choices.destination);
      publish(created);
      if (!resumeDraftId) {
        await runFreshIntake(created, () => generation.current === epoch);
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
      if (!committed.current) abortIntake();
    };
  }, [abortIntake, api, clearPreview, configureIntake, projectId, publish, reportError, resumeDraftId, runFreshIntake]);

  const registerCommittedUpload = useCallback(async (importRef: string | null | undefined) => {
    if (!importRef || registeredUpload.current === importRef) return;
    const running = activeIntake();
    if (!running) return;
    registeredUpload.current = importRef;
    setImportSessionBrowserUploading(importRef, true);
    const unregister = registerImportSessionUploadAbort(importRef, running.abort);
    const release = () => {
      unregister();
      setImportSessionBrowserUploading(importRef, false);
      if (registeredUpload.current === importRef) registeredUpload.current = null;
    };
    void running.done.then(release, release);
    await rememberImportSessionFiles(importRef, intakeFiles());
  }, [activeIntake, intakeFiles]);

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
      await registerCommittedUpload(next.commit?.result?.import_ref);
      if (next.state === 'cancelled') onDiscardedRef.current();
      const sheetId = next.commit?.result?.sheet_id;
      if (next.state === 'committed' && sheetId && importedSheet.current !== sheetId) {
        committed.current = true;
        importedSheet.current = sheetId;
        onImportedRef.current(sheetId);
      }
      return next;
    } catch (problem) {
      if (!controller.signal.aborted && generation.current === epoch) reportError(problem);
    } finally {
      if (refreshController.current === controller) refreshController.current = null;
    }
  }, [api, projectId, publish, registerCommittedUpload, reportError]);

  usePoll(refresh, {
    active: Boolean(snapshot && (
      snapshot.state === 'committing'
      || snapshot.state === 'cancelling'
      || (!snapshot.paused && snapshot.state === 'open'
        && (!snapshot.upload.sealed || snapshot.analysis.files_done < (snapshot.analysis.files_total ?? Infinity)))
      || (previewJob?.status === 'running' && !previewCached)
    )),
    intervalMs: 1_200,
    guardOverlap: true,
  });

  useEffect(() => {
    if (!snapshot) return;
    onContextChange({
      active: true,
      stage,
      stages: PDF_BATCH_STAGES,
      subtitle: subtitle(snapshot, intakeFiles()),
      busy: mutationBusy || snapshot.state === 'committing' || snapshot.state === 'cancelling',
    });
  }, [intakeFiles, mutationBusy, onContextChange, snapshot, stage]);

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
      await selectPreview(result.items[0] ?? null);
    } catch (problem) {
      if (requestEpoch === sampleRequestEpoch.current) reportError(problem);
    } finally {
      if (requestEpoch === sampleRequestEpoch.current) setSamplesLoading(false);
    }
  }, [api, projectId, reportError, selectPreview, snapshot]);

  const chooseCategory = (category: PdfImportCategory) => {
    setSelectedCategory(category);
    setSampleFiles([]);
    clearPreview();
    void loadSamples(category);
  };

  const selectSample = (file: PdfImportFileSummary) => {
    void selectPreview(file);
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
    setHasRecoveryFiles(true);
    setReselecting(true);
    setError(null);
    try {
      await runReselection(source, pdfs, {
        shouldPublish: isCurrent,
        refresh,
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
    const retained = resumeDraftId ? recoveryFiles.current : intakeFiles();
    try {
      await stopIntake();
      const resumed = await api.resume(projectId, snapshot.draft_id);
      publish(resumed);
      if (!resumeDraftId) {
        const epoch = generation.current;
        await runFreshIntake(resumed, () => generation.current === epoch);
      }
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
        const epoch = generation.current;
        await runFreshIntake(snapshot, () => generation.current === epoch);
      }
    } catch (problem) {
      reportError(problem);
      await refresh();
    } finally {
      setMutationBusy(false);
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
    abortIntake();
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

  const commit = async (ocrConfirmation?: string) => {
    if (!snapshot || mutationBusy || !destinationIsReady(snapshot, destination)) return;
    const semanticSetup = snapshot.semantic_setup;
    if ((semanticEnabled ?? snapshot.choices.semantic_search)
      && semanticSetup.needs_confirmation
      && (!semanticSetup.confirmation_hash
        || snapshot.choices.semantic_setup_confirmation !== semanticSetup.confirmation_hash)) return;
    const remoteOcr = ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) > 0
      && ocrEngine(snapshot.choices.ocr_selection) !== 'rapidocr';
    if (remoteOcr && snapshot.batch_ocr.needs_confirmation) {
      const estimate = snapshot.batch_ocr.estimate;
      const confirmation = estimate?.promise_set_hash;
      if (!estimate || !confirmation) {
        reportError(new Error('The OCR estimate did not include its authorization token.'));
        return;
      }
      if (!ocrConfirmation) {
        setBatchOcrConsent({
          draftId: snapshot.draft_id,
          confirmation,
          estimate,
          selectedCount: snapshot.batch_ocr.selected_count,
          engine: ocrEngine(snapshot.choices.ocr_selection),
        });
        return;
      }
      if (ocrConfirmation !== confirmation) {
        setBatchOcrConsent(null);
        setError('The OCR quote changed. Review the current estimate and confirm again.');
        return;
      }
    }
    setMutationBusy(true);
    setError(null);
    try {
      const accepted = await api.commit(projectId, snapshot.draft_id, {
        idempotency_key: commitKey.current,
        expected_revision: snapshot.revision,
        ...(ocrConfirmation ? { ocr_confirmation: ocrConfirmation } : {}),
      });
      committed.current = true;
      await registerCommittedUpload(accepted.job?.import_ref ?? accepted.draft_id);
      const refreshed = await refresh();
      const importRef = accepted.job?.import_ref ?? refreshed?.commit?.result?.import_ref;
      const sheetId = accepted.job?.sheet_id;
      if (sheetId && importRef && importedSheet.current !== sheetId) {
        importedSheet.current = sheetId;
        onImportedRef.current(sheetId);
      }
    } catch (problem) {
      if (problem instanceof PdfImportResponseError && problem.status === 409) {
        setBatchOcrConsent(null);
        const latest = await refresh();
        if (latest && remoteOcr) await estimateBatchOcr(latest);
        setError('The import details changed. Review the current OCR estimate and confirm again.');
        return;
      }
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
    Boolean(snapshot.semantic_setup.confirmation_hash)
    && snapshot.choices.semantic_setup_confirmation === snapshot.semantic_setup.confirmation_hash
  );
  const matching = destination.kind === 'existing' && snapshot.match.state === 'running';
  const nothingNew = destination.kind === 'existing'
    && snapshot.match.state === 'ready' && snapshot.match.new_count === 0;
  const activeBatchOcrConsent = batchOcrConsent
    && batchOcrConsent.draftId === snapshot.draft_id
    && snapshot.batch_ocr.state === 'ready'
    && batchOcrConsent.confirmation === snapshot.batch_ocr.estimate?.promise_set_hash
    ? batchOcrConsent
    : null;
  const canCommit = destinationIsReady(snapshot, destination)
    && !matching && semanticAuthorized
    && snapshot.analysis.files_done >= snapshot.upload.files_done
    && (ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) === 0
      || (Boolean(ocrChoice?.can_run) && (
        ocrEngine(snapshot.choices.ocr_selection) === 'rapidocr'
        || snapshot.batch_ocr?.state === 'ready')
      ))
    && snapshot.state === 'open' && !mutationBusy;

  return (
    <>
      <div className="pdf-batch-flow" data-stage={stage} data-testid="pdf-batch-flow">
        {stage === 'add' && (
          <AddFilesStep
            snapshot={snapshot}
            totalFiles={totalFiles}
            needsReselection={Boolean(resumeDraftId && !hasRecoveryFiles && snapshot.upload.files_done < snapshot.upload.files_total)}
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
            page={previewPage}
            pageLoading={pageLoading}
            textSource={textSource}
            previewRunning={previewRunning}
            checkRunning={checkRunning}
            busy={mutationBusy}
            onCategory={chooseCategory}
            onSample={selectSample}
            onResample={() => {
              void loadSamples(selectedCategory, sampleCursor ?? undefined);
            }}
            onTextSource={setTextSource}
            onPreview={() => chosenFile && void startPreview(chosenFile.occurrence_id, 1, undefined, refresh)}
            onOcrChoice={(choice) => {
              if (choice) setReportedOcrChoice(choice);
            }}
            onOcrSelection={(ocrSelection) => {
              setReportedOcrChoice(null);
              void mutateChoices({ ocr_selection: ocrSelection });
            }}
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
            consent={Boolean(snapshot.semantic_setup.confirmation_hash)
              && snapshot.choices.semantic_setup_confirmation === snapshot.semantic_setup.confirmation_hash}
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
            ocrAvailabilityChecking={ocrAvailabilityChecking}
            ocrAvailable={ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) === 0 || Boolean(ocrChoice?.can_run)}
            ocrProblem={ocrChoice?.blocker?.message ?? ocrAvailability.error?.message ?? null}
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
              if (consent && snapshot.semantic_setup.confirmation_hash) void mutateChoices({
                semantic_search: true,
                semantic_setup_confirmation: snapshot.semantic_setup.confirmation_hash,
              });
              else {
                setSemanticEnabled(false);
                void mutateChoices({ semantic_search: false, semantic_setup_confirmation: null });
              }
            }}
            onBack={() => setStage('destination')}
            onReviewOcr={() => setStage('check')}
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
            void startPreview(
              pending.fileId,
              pending.page,
              pending.estimate.estimate?.promise_set_hash ?? undefined,
              refresh,
            );
          }}
        />
      )}
      {activeBatchOcrConsent && (
        <CostGateModal
          estimate={activeBatchOcrConsent.estimate}
          message={`Run ${activeBatchOcrConsent.engine} OCR on ${count(activeBatchOcrConsent.selectedCount, 'file')} when importing?`}
          onCancel={() => setBatchOcrConsent(null)}
          onConfirm={() => {
            const pending = activeBatchOcrConsent;
            setBatchOcrConsent(null);
            void commit(pending.confirmation);
          }}
        />
      )}
    </>
  );
}
