import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type Dispatch,
} from 'react';
import { FileText, LoaderCircle, RefreshCw } from 'lucide-react';
import type { RunEstimate } from '../../../api/open';
import type { SelectorChoice } from '../../../api/selectorChoices';
import {
  cancelPdfPacketOcr,
  commitPdfPacketSplit,
  createPdfPacketSplit,
  deletePdfPacketSplit,
  estimatePdfPacketOcr,
  getPdfPacketPage,
  getPdfPacketSplit,
  matchPdfPacketCandidates,
  setPdfPacketTextSource,
  startPdfPacketOcr,
  type PdfPacketSplitSnapshot,
} from '../../../api/pdfPacketSplits';
import { SelectorField } from '../../../engine-selector/SelectorField';
import { countLabel } from '../../../format';
import { CostGateModal } from '../../CostGateModal';
import { SegmentedToggle } from '../../PanelPrimitives';
import {
  canKeepPdfPacketOcr,
  confirmedDocuments,
  formatPdfPacketDocumentName,
  initialPdfPacketFlowState,
  pdfPacketNamePatternError,
  pdfPacketFlowReducer,
  samplePages,
  resamplePages,
  type PdfPacketDocument,
  type PdfPacketRememberedOptions,
  type PdfPacketStage,
} from './model';
import { PacketFindStep } from './PacketFindStep';
import { PacketThumbnail } from './PacketThumbnail';

export interface PdfPacketShellContext {
  active: boolean;
  stage: PdfPacketStage;
  subtitle: string | null;
  busy: boolean;
}

interface PdfPacketImportFlowProps {
  projectId: string;
  onContextChange(context: PdfPacketShellContext): void;
  registerCloseGuard(guard: (() => boolean) | null): void;
  onImported(sheetId: number): void;
  onError(message: string): void;
}

interface PendingOcrConsent {
  estimate: RunEstimate;
  engine: string;
  scope: 'sample' | 'all';
  pages?: number[];
  cachedPages: number[];
  confirmation: string;
}

interface PendingFullOcrSource {
  engine: string;
  jobId: string;
  analysisRevision: number;
}

function engineOf(choice: SelectorChoice): string | null {
  const selection = choice.authored_selection;
  return selection.kind === 'engine' || selection.kind === 'engine_model'
    ? selection.engine
    : null;
}

function pageCountOf(snapshot: PdfPacketSplitSnapshot | null): number {
  return snapshot?.packet.page_count ?? 0;
}

function progressText(snapshot: PdfPacketSplitSnapshot): string {
  const progress = snapshot.prepare.progress;
  if (progress.status === 'error') return progress.error ?? 'The packet could not be prepared.';
  const total = progress.total ?? snapshot.packet.page_count;
  return total ? `Preparing pages · ${progress.done} of ${total}` : 'Reading the packet…';
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

function rememberedOptionsKey(projectId: string): string {
  return `frisket.pdf-packet-split.options.${projectId}`;
}

function loadRememberedOptions(projectId: string): PdfPacketRememberedOptions | null {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(rememberedOptionsKey(projectId)) ?? 'null');
    if (!parsed || typeof parsed !== 'object') return null;
    const value = parsed as Partial<PdfPacketRememberedOptions>;
    return typeof value.namePattern === 'string' && typeof value.keepOcrText === 'boolean'
      ? { namePattern: value.namePattern, keepOcrText: value.keepOcrText }
      : null;
  } catch {
    return null;
  }
}

export function PdfPacketImportFlow({
  projectId,
  onContextChange,
  registerCloseGuard,
  onImported,
  onError,
}: PdfPacketImportFlowProps) {
  const [state, dispatch] = useReducer(pdfPacketFlowReducer, initialPdfPacketFlowState);
  const [uploading, setUploading] = useState(false);
  const [showOcr, setShowOcr] = useState(false);
  const [ocrConsent, setOcrConsent] = useState<PendingOcrConsent | null>(null);
  const [pendingFullOcrSource, setPendingFullOcrSource] = useState<PendingFullOcrSource | null>(null);
  const [sessionProjectId, setSessionProjectId] = useState<string | null>(null);
  const sessionProject = useRef<string | null>(null);
  const projectEpoch = useRef(0);
  const actionController = useRef<AbortController | null>(null);
  const splitId = sessionProjectId === projectId ? state.snapshot?.split_id ?? null : null;
  const pageCount = pageCountOf(state.snapshot);
  const candidateGeneration = useRef(0);
  const requestId = useRef(crypto.randomUUID());
  const commitRequestId = useRef(crypto.randomUUID());
  const handledCommitFailureJob = useRef<string | null>(null);
  const committedSheet = useRef<number | null>(null);
  const latestStatus = useRef(state.snapshot?.status);
  const serverCommitBusy = state.snapshot?.status === 'committing';

  const beginAction = useCallback(() => {
    actionController.current?.abort();
    const controller = new AbortController();
    actionController.current = controller;
    return { controller, epoch: projectEpoch.current };
  }, []);

  const actionIsCurrent = useCallback((controller: AbortController, epoch: number) => (
    !controller.signal.aborted && epoch === projectEpoch.current && sessionProject.current === projectId
  ), [projectId]);

  const previousProjectId = useRef(projectId);
  useEffect(() => {
    if (previousProjectId.current === projectId) return;
    previousProjectId.current = projectId;
    projectEpoch.current += 1;
    actionController.current?.abort();
    candidateGeneration.current += 1;
    sessionProject.current = null;
    setSessionProjectId(null);
    requestId.current = crypto.randomUUID();
    commitRequestId.current = crypto.randomUUID();
    handledCommitFailureJob.current = null;
    committedSheet.current = null;
    latestStatus.current = undefined;
    setUploading(false);
    setShowOcr(false);
    setOcrConsent(null);
    setPendingFullOcrSource(null);
    dispatch({ type: 'reset' });
  }, [projectId]);

  useEffect(() => () => {
    projectEpoch.current += 1;
    actionController.current?.abort();
    candidateGeneration.current += 1;
  }, []);

  useEffect(() => {
    latestStatus.current = state.snapshot?.status;
  }, [state.snapshot?.status]);

  useEffect(() => {
    onContextChange({
      active: state.snapshot !== null,
      stage: state.stage,
      subtitle: state.snapshot
        ? `${state.snapshot.packet.filename}${pageCount ? ` · ${countLabel(pageCount, 'page')}` : ''}`
        : null,
      busy: uploading || state.ocrBusy || state.committing || serverCommitBusy,
    });
  }, [onContextChange, pageCount, serverCommitBusy, state.committing, state.ocrBusy, state.snapshot, state.stage, uploading]);

  useEffect(() => {
    registerCloseGuard(state.snapshot ? () => (
      window.confirm('Discard this PDF split and its marked document starts?')
    ) : null);
    return () => registerCloseGuard(null);
  }, [registerCloseGuard, state.snapshot]);

  useEffect(() => {
    if (!splitId) return undefined;
    return () => {
      if (latestStatus.current !== 'completed' && latestStatus.current !== 'cancelled') {
        void deletePdfPacketSplit(projectId, splitId).catch(() => undefined);
      }
    };
  }, [projectId, splitId]);

  const refreshSnapshot = useCallback(async (signal?: AbortSignal) => {
    if (!splitId) return;
    const epoch = projectEpoch.current;
    try {
      const snapshot = await getPdfPacketSplit(projectId, splitId, { signal });
      if (signal?.aborted || epoch !== projectEpoch.current || sessionProject.current !== projectId) return;
      latestStatus.current = snapshot.status;
      dispatch({ type: 'snapshot', snapshot });
      const latestCommitJob = snapshot.jobs.filter((job) => job.kind === 'commit').at(-1);
      const commitFailed = latestCommitJob?.progress.status === 'error'
        || latestCommitJob?.progress.status === 'cancelled';
      if (latestCommitJob && commitFailed) {
        if (handledCommitFailureJob.current !== latestCommitJob.job_id) {
          handledCommitFailureJob.current = latestCommitJob.job_id;
          commitRequestId.current = crypto.randomUUID();
          dispatch({
            type: 'error',
            message: latestCommitJob.progress.error
              ?? (latestCommitJob.progress.status === 'cancelled' ? 'Import was cancelled.' : 'Import failed.'),
          });
        }
      } else if (snapshot.status === 'error') {
        dispatch({ type: 'error', message: snapshot.prepare.progress.error ?? 'Packet processing failed.' });
      }
      if (snapshot.commit_result && committedSheet.current !== snapshot.commit_result.sheet_id) {
        committedSheet.current = snapshot.commit_result.sheet_id;
        onImported(snapshot.commit_result.sheet_id);
      }
    } catch (error) {
      if (!signal?.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  }, [onImported, projectId, splitId]);

  useEffect(() => {
    if (!state.snapshot) return undefined;
    const active = state.snapshot.status === 'preparing'
      || state.snapshot.status === 'committing'
      || state.snapshot.jobs.some((job) => job.progress.status === 'queued' || job.progress.status === 'running');
    if (!active) return undefined;
    const controller = new AbortController();
    const timer = window.setInterval(() => void refreshSnapshot(controller.signal), 900);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [refreshSnapshot, state.snapshot]);

  const loadPage = useCallback(async (page: number, signal?: AbortSignal) => {
    if (!splitId) return;
    const epoch = projectEpoch.current;
    dispatch({ type: 'pageLoading', page });
    try {
      const loaded = await getPdfPacketPage(projectId, splitId, page, { signal });
      if (!signal?.aborted && epoch === projectEpoch.current && sessionProject.current === projectId) {
        dispatch({ type: 'pageLoaded', page: loaded });
      }
    } catch (error) {
      if (!signal?.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  }, [projectId, splitId]);

  useEffect(() => {
    if (!splitId || state.snapshot?.status === 'preparing') return undefined;
    const controller = new AbortController();
    void loadPage(state.selectedPage, controller.signal);
    return () => controller.abort();
  }, [loadPage, splitId, state.selectedPage, state.snapshot?.status]);

  const selectedPageOcrJob = state.snapshot?.jobs
    .filter((job) => job.kind === 'ocr_sample' && job.pages.includes(state.selectedPage))
    .at(-1);
  const sampleJobId = selectedPageOcrJob?.job_id;
  const sampleJobStatus = selectedPageOcrJob?.progress.status;
  const sampleJobError = selectedPageOcrJob?.progress.error;
  const sampleJobEngine = selectedPageOcrJob?.engine;
  useEffect(() => {
    if (!splitId || !sampleJobId || state.stage !== 'check') return undefined;
    if (sampleJobStatus === 'error' || sampleJobStatus === 'cancelled') {
      dispatch({ type: 'error', message: sampleJobError ?? 'Text extraction stopped.' });
      return undefined;
    }
    if (sampleJobStatus !== 'done' || !sampleJobEngine) return undefined;
    const controller = new AbortController();
    const engine = sampleJobEngine;
    const selectAndLoad = async () => {
      try {
        if (state.snapshot?.text_source !== 'ocr' || state.snapshot.ocr_engine !== engine) {
          const snapshot = await setPdfPacketTextSource(
            projectId,
            splitId,
            { kind: 'ocr', engine },
            { signal: controller.signal },
          );
          if (controller.signal.aborted) return;
          dispatch({ type: 'snapshot', snapshot });
          dispatch({ type: 'ocrEngine', engine });
        }
        await loadPage(state.selectedPage, controller.signal);
      } catch (error) {
        if (!controller.signal.aborted) {
          dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
        }
      }
    };
    void selectAndLoad();
    return () => controller.abort();
  }, [loadPage, projectId, sampleJobError, sampleJobEngine, sampleJobId, sampleJobStatus, splitId, state.stage, state.selectedPage, state.snapshot?.ocr_engine, state.snapshot?.text_source]);

  const sampleExtracting = state.ocrBusy || selectedPageOcrJob?.progress.status === 'queued'
    || selectedPageOcrJob?.progress.status === 'running';
  const ocrRunning = state.snapshot?.jobs.some((job) => (
    (job.kind === 'ocr_sample' || job.kind === 'ocr_full')
    && (job.progress.status === 'queued' || job.progress.status === 'running')
  )) ?? false;

  const pendingFullOcrJob = state.snapshot?.jobs.find(
    (candidate) => candidate.job_id === pendingFullOcrSource?.jobId,
  );
  const pendingSnapshotRevision = state.snapshot?.analysis_revision;
  const pendingSnapshotTextSource = state.snapshot?.text_source;
  const pendingSnapshotOcrEngine = state.snapshot?.ocr_engine;
  useEffect(() => {
    if (!splitId || !pendingFullOcrSource || pendingSnapshotRevision === undefined) return undefined;
    const controller = new AbortController();
    const settlePendingSource = async () => {
      if (pendingFullOcrJob?.progress.status === 'error' || pendingFullOcrJob?.progress.status === 'cancelled') {
        setPendingFullOcrSource(null);
        dispatch({ type: 'stage', stage: 'check' });
        dispatch({
          type: 'error',
          message: pendingFullOcrJob.progress.error ?? (pendingFullOcrJob.progress.status === 'cancelled' ? 'OCR was stopped.' : 'OCR failed.'),
        });
        return;
      }
      if (pendingSnapshotTextSource === 'ocr' && pendingSnapshotOcrEngine === pendingFullOcrSource.engine) {
        setPendingFullOcrSource(null);
        return;
      }
      if (pendingSnapshotRevision <= pendingFullOcrSource.analysisRevision) return;
      try {
        const snapshot = await setPdfPacketTextSource(
          projectId,
          splitId,
          { kind: 'ocr', engine: pendingFullOcrSource.engine },
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        dispatch({ type: 'snapshot', snapshot });
        dispatch({ type: 'ocrEngine', engine: pendingFullOcrSource.engine });
        setPendingFullOcrSource(null);
      } catch (error) {
        if (!controller.signal.aborted) {
          dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
        }
      }
    };
    void settlePendingSource();
    return () => controller.abort();
  }, [pendingFullOcrJob?.progress.error, pendingFullOcrJob?.progress.status, pendingFullOcrSource, pendingSnapshotOcrEngine, pendingSnapshotRevision, pendingSnapshotTextSource, projectId, splitId]);

  useEffect(() => {
    if (!splitId || state.stage !== 'find' || state.snapshot?.status === 'preparing' || state.snapshot?.text_source === 'unconfirmed') return undefined;
    const generation = ++candidateGeneration.current;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      dispatch({ type: 'matching' });
      void matchPdfPacketCandidates(projectId, splitId, {
        confirmed_starts: state.confirmedStarts,
        rejected: state.rejected,
        threshold_pct: state.threshold,
        phrases: state.phrases.filter((phrase) => phrase.text.trim()).map((phrase) => ({
          ...phrase,
          text: phrase.text.trim(),
        })),
      }, { signal: controller.signal }).then((matches) => {
        if (generation === candidateGeneration.current && !controller.signal.aborted) {
          dispatch({ type: 'matches', value: matches });
        }
      }).catch((error: unknown) => {
        if (!controller.signal.aborted && generation === candidateGeneration.current) {
          dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
        }
      });
    }, 180);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [projectId, splitId, state.confirmedStarts, state.phrases, state.rejected, state.snapshot?.analysis_revision, state.snapshot?.status, state.snapshot?.text_source, state.stage, state.threshold]);

  useEffect(() => {
    if (state.stage !== 'import' || !splitId) return undefined;
    const controller = new AbortController();
    for (const page of state.confirmedStarts) {
      if (!state.pages[page]) void loadPage(page, controller.signal);
    }
    return () => controller.abort();
  }, [loadPage, splitId, state.confirmedStarts, state.pages, state.stage]);

  const chooseFile = async (file: File) => {
    if (!(file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'))) {
      dispatch({ type: 'error', message: 'Choose a PDF packet.' });
      return;
    }
    const { controller, epoch } = beginAction();
    setUploading(true);
    dispatch({ type: 'error', message: null });
    try {
      const snapshot = await createPdfPacketSplit(projectId, file, requestId.current, { signal: controller.signal });
      if (controller.signal.aborted || epoch !== projectEpoch.current) return;
      sessionProject.current = projectId;
      setSessionProjectId(projectId);
      dispatch({ type: 'prepared', snapshot, remembered: loadRememberedOptions(projectId) });
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = error instanceof Error ? error.message : String(error);
      dispatch({ type: 'error', message });
      onError(`Import failed: ${message}`);
    } finally {
      if (epoch === projectEpoch.current) setUploading(false);
    }
  };

  const launchOcr = async ({
    engine,
    scope,
    pages,
    cachedPages,
    confirmation,
  }: { engine: string; scope: 'sample' | 'all'; pages?: number[]; cachedPages: number[]; confirmation?: string }) => {
    if (!splitId) return;
    const { controller, epoch } = beginAction();
    dispatch({ type: 'ocrBusy', value: true });
    dispatch({ type: 'error', message: null });
    try {
      const started = await startPdfPacketOcr(projectId, splitId, {
        engine,
        scope,
        pages,
        confirmation,
      }, { signal: controller.signal });
      if (!actionIsCurrent(controller, epoch)) return;
      if (scope === 'all') {
        if (cachedPages.length > 0) {
          const snapshot = await setPdfPacketTextSource(
            projectId,
            splitId,
            { kind: 'ocr', engine },
            { signal: controller.signal },
          );
          if (!actionIsCurrent(controller, epoch)) return;
          dispatch({ type: 'snapshot', snapshot });
          dispatch({ type: 'ocrEngine', engine });
        } else {
          setPendingFullOcrSource({
            engine,
            jobId: started.job_id,
            analysisRevision: state.snapshot?.analysis_revision ?? 0,
          });
          await refreshSnapshot(controller.signal);
          if (!actionIsCurrent(controller, epoch)) return;
        }
        dispatch({ type: 'stage', stage: 'find' });
      } else {
        await refreshSnapshot(controller.signal);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
      }
    } finally {
      if (epoch === projectEpoch.current) dispatch({ type: 'ocrBusy', value: false });
    }
  };

  const runOcr = async (scope: 'sample' | 'all') => {
    if (!splitId) return;
    const { controller, epoch } = beginAction();
    const engine = state.ocrEngine;
    const pages = scope === 'sample' ? [state.selectedPage] : undefined;
    dispatch({ type: 'ocrBusy', value: true });
    dispatch({ type: 'error', message: null });
    try {
      const quote = await estimatePdfPacketOcr(projectId, splitId, {
        engine,
        scope,
        pages,
      }, { signal: controller.signal });
      if (!actionIsCurrent(controller, epoch)) return;
      if (quote.estimate === null) {
        if (quote.cached_pages.length === 0) {
          throw new Error('The OCR estimate reported no work and no cached pages.');
        }
        const snapshot = await setPdfPacketTextSource(
          projectId,
          splitId,
          { kind: 'ocr', engine },
          { signal: controller.signal },
        );
        if (!actionIsCurrent(controller, epoch)) return;
        dispatch({ type: 'snapshot', snapshot });
        dispatch({ type: 'ocrEngine', engine });
        if (scope === 'all') dispatch({ type: 'stage', stage: 'find' });
        else await loadPage(state.selectedPage, controller.signal);
        return;
      }
      if (quote.estimate.requires_confirmation) {
        const confirmation = quote.estimate.promise_set_hash;
        if (!confirmation) throw new Error('The OCR estimate did not include its confirmation token.');
        setOcrConsent({
          estimate: quote.estimate,
          engine,
          scope,
          pages,
          cachedPages: quote.cached_pages,
          confirmation,
        });
        return;
      }
      await launchOcr({ engine, scope, pages, cachedPages: quote.cached_pages });
    } catch (error) {
      if (!controller.signal.aborted) {
        dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
      }
    } finally {
      if (epoch === projectEpoch.current) dispatch({ type: 'ocrBusy', value: false });
    }
  };

  const acceptNativeText = async () => {
    if (!splitId) return;
    const { controller, epoch } = beginAction();
    try {
      const snapshot = await setPdfPacketTextSource(projectId, splitId, { kind: 'native' }, { signal: controller.signal });
      if (!actionIsCurrent(controller, epoch)) return;
      dispatch({ type: 'snapshot', snapshot });
      dispatch({ type: 'keepOcrText', value: false });
      dispatch({ type: 'stage', stage: 'find' });
    } catch (error) {
      if (!controller.signal.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  };

  const chooseAnotherPacket = async () => {
    if (!splitId) return;
    const { controller, epoch } = beginAction();
    try {
      await deletePdfPacketSplit(projectId, splitId, { signal: controller.signal });
      if (!actionIsCurrent(controller, epoch)) return;
      latestStatus.current = 'cancelled';
      sessionProject.current = null;
      setSessionProjectId(null);
      requestId.current = crypto.randomUUID();
      commitRequestId.current = crypto.randomUUID();
      handledCommitFailureJob.current = null;
      setShowOcr(false);
      setPendingFullOcrSource(null);
      dispatch({ type: 'reset' });
    } catch (error) {
      if (!controller.signal.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  };

  const cancelOcr = async (jobId: string) => {
    if (!splitId) return;
    const { controller, epoch } = beginAction();
    try {
      const snapshot = await cancelPdfPacketOcr(projectId, splitId, jobId, { signal: controller.signal });
      if (!actionIsCurrent(controller, epoch)) return;
      dispatch({ type: 'snapshot', snapshot });
      dispatch({ type: 'keepOcrText', value: false });
      if (pendingFullOcrSource?.jobId === jobId) {
        setPendingFullOcrSource(null);
        dispatch({ type: 'stage', stage: 'check' });
      }
    } catch (error) {
      if (!controller.signal.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  };

  const submitImport = async () => {
    if (!splitId
      || state.committing
      || state.snapshot?.status === 'committing'
      || !state.destinationName.trim()
      || pdfPacketNamePatternError(state.namePattern)) return;
    const { controller, epoch } = beginAction();
    const canKeepOcrText = canKeepPdfPacketOcr(state.snapshot);
    dispatch({ type: 'error', message: null });
    dispatch({ type: 'committing', value: true });
    try {
      await commitPdfPacketSplit(projectId, splitId, {
        idempotency_key: commitRequestId.current,
        confirmed_starts: state.confirmedStarts,
        destination: { kind: 'new_sheet', name: state.destinationName.trim() },
        name_pattern: state.namePattern,
        keep_ocr_text: canKeepOcrText && state.keepOcrText,
      }, { signal: controller.signal });
      if (!actionIsCurrent(controller, epoch)) return;
      if (state.rememberOptions) {
        window.localStorage.setItem(rememberedOptionsKey(projectId), JSON.stringify({
          namePattern: state.namePattern,
          keepOcrText: canKeepOcrText && state.keepOcrText,
        } satisfies PdfPacketRememberedOptions));
      } else {
        window.localStorage.removeItem(rememberedOptionsKey(projectId));
      }
      await refreshSnapshot(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    } finally {
      if (epoch === projectEpoch.current) dispatch({ type: 'committing', value: false });
    }
  };

  if (!state.snapshot) {
    return <PacketChooser uploading={uploading} error={state.error} onChoose={chooseFile} />;
  }

  if (state.snapshot.status === 'preparing' || !pageCount) {
    return (
      <div className="pdf-packet-preparing" data-testid="pdf-packet-preparing">
        <FileText size={34} aria-hidden />
        <strong>{state.snapshot.packet.filename}</strong>
        <span>{formatBytes(state.snapshot.packet.size)}</span>
        <progress
          max={state.snapshot.prepare.progress.total ?? undefined}
          value={state.snapshot.prepare.progress.done}
        />
        <span>{progressText(state.snapshot)}</span>
        {state.error && <div className="import-field-error" role="alert">{state.error}</div>}
      </div>
    );
  }

  return (
    <>
      <div className="pdf-packet-flow" data-testid="pdf-packet-flow" data-stage={state.stage}>
      {state.stage === 'check' ? (
        <PacketTextStep
          projectId={projectId}
          splitId={state.snapshot.split_id}
          snapshot={state.snapshot}
          page={state.pages[state.selectedPage] ?? null}
          selectedPage={state.selectedPage}
          pageCount={pageCount}
          engine={state.ocrEngine}
          showOcr={showOcr}
          busy={state.ocrBusy || ocrRunning || state.pageLoading === state.selectedPage}
          extracting={sampleExtracting}
          loading={state.pageLoading === state.selectedPage}
          onSelectPage={(page) => dispatch({ type: 'selectPage', page })}
          onBack={() => void chooseAnotherPacket()}
          onEngine={(engine) => dispatch({ type: 'ocrEngine', engine })}
          onShowOcr={() => setShowOcr(true)}
          onRunSample={() => void runOcr('sample')}
          onAcceptNative={() => void acceptNativeText()}
          onAcceptOcr={() => void runOcr('all')}
        />
      ) : state.stage === 'find' ? (
        <PacketFindStep
          projectId={projectId}
          splitId={state.snapshot.split_id}
          state={state}
          pageCount={pageCount}
          dispatch={dispatch}
          onCancelOcr={(jobId) => void cancelOcr(jobId)}
        />
      ) : (
        <PacketImportStep
          state={state}
          pageCount={pageCount}
          canKeepOcrText={canKeepPdfPacketOcr(state.snapshot)}
          onBack={() => dispatch({ type: 'stage', stage: 'find' })}
          onSubmit={() => void submitImport()}
          dispatch={dispatch}
        />
      )}
      {state.error && <div className="pdf-packet-error import-field-error" role="alert">{state.error}</div>}
      </div>
      {ocrConsent ? (
        <CostGateModal
          estimate={ocrConsent.estimate}
          message={`Run ${ocrConsent.engine} OCR on ${ocrConsent.scope === 'all' ? `all ${countLabel(pageCount, 'page')}` : `page ${ocrConsent.pages?.[0] ?? state.selectedPage}`}?`}
          onCancel={() => setOcrConsent(null)}
          onConfirm={() => {
            const approved = ocrConsent;
            setOcrConsent(null);
            void launchOcr(approved);
          }}
        />
      ) : null}
    </>
  );
}

function PacketChooser({
  uploading,
  error,
  onChoose,
}: {
  uploading: boolean;
  error: string | null;
  onChoose(file: File): void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [dropError, setDropError] = useState<string | null>(null);
  return (
    <div
      className="pdf-packet-chooser"
      data-testid="pdf-packet-chooser"
      data-dragging={dragging || undefined}
      onDragOver={(event) => {
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = uploading ? 'none' : 'copy';
        if (!uploading) setDragging(true);
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        event.stopPropagation();
        setDragging(false);
        if (uploading) return;
        const files = event.dataTransfer.files;
        if (!files.length) return;
        if (files.length !== 1) {
          setDropError('Choose one PDF packet at a time.');
          return;
        }
        setDropError(null);
        onChoose(files[0]);
      }}
    >
      <FileText size={34} aria-hidden />
      <h2>Split a PDF packet</h2>
      <p>Mark the first page of each document. The original PDF is never changed.</p>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        data-testid="pdf-packet-input"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) {
            setDropError(null);
            onChoose(file);
          }
        }}
      />
      <button type="button" className="btn btn-primary" disabled={uploading} onClick={() => inputRef.current?.click()}>
        {uploading ? 'Reading packet…' : 'Choose PDF packet'}
      </button>
      <span className="form-hint">or drop a PDF here</span>
      {(dropError || error) && <div className="import-field-error" role="alert">{dropError || error}</div>}
    </div>
  );
}

function PacketTextStep({
  projectId,
  splitId,
  snapshot,
  page,
  selectedPage,
  pageCount,
  engine,
  showOcr,
  busy,
  extracting,
  loading,
  onSelectPage,
  onBack,
  onEngine,
  onShowOcr,
  onRunSample,
  onAcceptNative,
  onAcceptOcr,
}: {
  projectId: string;
  splitId: string;
  snapshot: PdfPacketSplitSnapshot;
  page: Awaited<ReturnType<typeof getPdfPacketPage>> | null;
  selectedPage: number;
  pageCount: number;
  engine: string;
  showOcr: boolean;
  busy: boolean;
  extracting: boolean;
  loading: boolean;
  onSelectPage(page: number): void;
  onBack(): void;
  onEngine(engine: string): void;
  onShowOcr(): void;
  onRunSample(): void;
  onAcceptNative(): void;
  onAcceptOcr(): void;
}) {
  const [defaultSamples, setSamples] = useState(() => samplePages(pageCount));
  const samples = defaultSamples.includes(selectedPage)
    ? defaultSamples
    : [selectedPage, ...defaultSamples.slice(1)].sort((left, right) => left - right);
  const nativeText = page?.native_text ?? '';
  const ocrText = page?.ocr_engine === engine ? page.ocr_text ?? '' : '';
  const [preferredSource, setPreferredSource] = useState<'native' | 'ocr' | null>(null);
  const defaultSource = snapshot.text_source === 'ocr' && snapshot.ocr_engine === engine ? 'ocr' : 'native';
  const source = preferredSource === 'ocr' && ocrText
    ? 'ocr'
    : preferredSource === 'native' && nativeText
      ? 'native'
      : defaultSource === 'ocr' && ocrText
        ? 'ocr'
        : !nativeText && ocrText
          ? 'ocr'
          : 'native';
  const shownText = source === 'ocr' ? ocrText : nativeText;
  const nativeCount = snapshot.prepare.native_text_pages.length;
  return (
    <div className="pdf-packet-check">
      <aside className="pdf-packet-check-sidebar">
        <h3>Text from the PDF</h3>
        <p>{nativeCount > 0
          ? `Extracted text is available on ${nativeCount} of ${countLabel(pageCount, 'page')}.`
          : 'This packet has no extracted text. Run OCR to read it.'}</p>
        <p className="form-hint">Check the text below. If it looks wrong, run OCR.</p>
        <div className="pdf-packet-sample-heading">
          <h3>Sample pages</h3>
          <button type="button" className="icon-btn" aria-label="Refresh sample pages" title="Refresh sample pages" disabled={pageCount <= 6} onClick={() => {
            const nextSamples = resamplePages(pageCount, samples);
            setSamples(nextSamples);
            onSelectPage(nextSamples[0]);
          }}><RefreshCw size={15} aria-hidden /></button>
        </div>
        <div className="pdf-packet-samples">
          {samples.map((sample) => (
            <button
              type="button"
              key={sample}
              className={sample === selectedPage ? 'active' : ''}
              aria-pressed={sample === selectedPage}
              onClick={() => onSelectPage(sample)}
            >
              <PacketThumbnail projectId={projectId} splitId={splitId} page={sample} />
              <span>p {sample}</span>
            </button>
          ))}
        </div>
        <label className="pdf-packet-page-picker">
          <span>Pick a different page</span>
          <span>
            Page
            <input
              type="number"
              min="1"
              max={pageCount}
              value={selectedPage}
              aria-label="Sample page number"
              onChange={(event) => {
                const nextPage = Number(event.target.value);
                if (Number.isInteger(nextPage) && nextPage >= 1 && nextPage <= pageCount) {
                  onSelectPage(nextPage);
                }
              }}
            />
            of {pageCount}
          </span>
        </label>
        {showOcr || nativeCount === 0 ? (
          <div className="pdf-packet-engine">
            <strong>OCR engine</strong>
            <SelectorField
              projectId={projectId}
              label="OCR engine"
              disabled={busy}
              query={{
                schema_version: 'frisket.selector_choices_query.v1',
                subject: { kind: 'action', action_id: 'media.ocr', field: 'engine', params: { engine } },
              }}
              recentNamespace="pdf-packet-ocr"
              testId="pdf-packet-engine"
              onSelect={(choice) => {
                const selected = engineOf(choice);
                if (selected) onEngine(selected);
              }}
              onCurrentChoiceChange={(choice) => {
                const selected = choice ? engineOf(choice) : null;
                if (selected && selected !== engine) onEngine(selected);
              }}
              allowChoice={(choice) => {
                const selected = engineOf(choice);
                return selected !== null;
              }}
            />
            <button type="button" className="btn" disabled={busy} onClick={() => {
              setPreferredSource('ocr');
              onRunSample();
            }}>
              Extract text
            </button>
          </div>
        ) : (
          <button type="button" className="btn" onClick={onShowOcr}>Looks wrong? Run OCR</button>
        )}
      </aside>
      <section className="pdf-packet-text-preview">
        <header>
          <strong>Page {selectedPage}</strong>
          {ocrText && nativeText ? (
            <SegmentedToggle
              fullWidth={false}
              ariaLabel="Text source"
              value={source}
              onValueChange={(value) => setPreferredSource(value as 'native' | 'ocr')}
              options={[{ value: 'native', label: 'Extracted text' }, { value: 'ocr', label: 'OCR' }]}
            />
          ) : null}
        </header>
        <div className="pdf-packet-page-and-text">
          <PacketThumbnail projectId={projectId} splitId={splitId} page={selectedPage} alt={`Page ${selectedPage}`} eager />
          <div className="pdf-packet-text-card">
            <strong>Extracted text</strong>
            {extracting || loading ? (
              <div className="pdf-packet-text-loading" role="status" aria-label={extracting ? 'Extracting text' : 'Reading page'}>
                <LoaderCircle className="spin" size={24} aria-hidden />
                <span>{extracting ? 'Extracting text…' : 'Reading page…'}</span>
              </div>
            ) : shownText ? <pre>{shownText}</pre> : (
              <p>{source === 'ocr' ? 'Run OCR to read this page.' : 'No extracted text on this page.'}</p>
            )}
          </div>
        </div>
        <footer>
          <button type="button" className="btn" onClick={onBack}>Back</button>
          <strong>Does this text look right?</strong>
          <div>
            {nativeText && source === 'native' ? (
              <>
                <button type="button" className="btn" disabled={busy} onClick={onShowOcr}>No, run OCR</button>
                <button type="button" className="btn btn-primary" disabled={busy} onClick={onAcceptNative}>Yes, use extracted text</button>
              </>
            ) : null}
            {ocrText && source === 'ocr' ? (
              <button type="button" className="btn btn-primary" disabled={busy} onClick={onAcceptOcr}>
                Use this OCR and read all {countLabel(pageCount, 'page')}
              </button>
            ) : null}
            {!nativeText && !ocrText ? (
              <button type="button" className="btn btn-primary" disabled={busy} onClick={onAcceptNative}>
                Continue without OCR
              </button>
            ) : null}
          </div>
        </footer>
      </section>
    </div>
  );
}

type PacketDispatch = Dispatch<Parameters<typeof pdfPacketFlowReducer>[1]>;

function PacketImportStep({ state, pageCount, canKeepOcrText, onBack, onSubmit, dispatch }: { state: ReturnType<typeof pdfPacketFlowReducer>; pageCount: number; canKeepOcrText: boolean; onBack(): void; onSubmit(): void; dispatch: PacketDispatch }) {
  const documents = confirmedDocuments(state.confirmedStarts, pageCount);
  const patternError = pdfPacketNamePatternError(state.namePattern);
  const commitBusy = state.committing || state.snapshot?.status === 'committing';
  const nameFor = (document: PdfPacketDocument) => formatPdfPacketDocumentName(
    state.namePattern,
    state.snapshot!.packet.filename,
    document,
  ) ?? 'Invalid file name pattern';
  return <div className="pdf-packet-import-step">
    <header><h2>Import {countLabel(documents.length, 'document')}</h2><p>Each becomes a normal PDF. The original packet stays as it is.</p></header>
    <div className="pdf-packet-import-options">
      <label className="pdf-packet-import-field"><span>New sheet</span>
        <input className="form-input" value={state.destinationName} onChange={(event) => dispatch({ type: 'destinationName', value: event.target.value })} />
      </label>
      <label className="pdf-packet-import-field"><span>File names</span>
        <input className="form-input mono" value={state.namePattern} onChange={(event) => dispatch({ type: 'namePattern', value: event.target.value })} />
        {patternError && <span className="import-field-error" role="alert">{patternError}</span>}
      </label>
      <div className="pdf-packet-import-choice">
        <label className="import-check-inline"><input type="checkbox" disabled={!canKeepOcrText} checked={canKeepOcrText && state.keepOcrText} onChange={(event) => dispatch({ type: 'keepOcrText', value: event.target.checked })} /> Keep OCR text and boxes with each PDF</label>
        {!canKeepOcrText && <p className="form-hint">Finish OCR for every page to keep its text and boxes.</p>}
      </div>
      <div className="pdf-packet-import-choice">
        <label className="import-check-inline"><input type="checkbox" checked={state.rememberOptions} onChange={(event) => dispatch({ type: 'rememberOptions', value: event.target.checked })} /> Remember these options for the next split</label>
      </div>
    </div>
    <div className="pdf-packet-import-table"><table><thead><tr><th>#</th><th>Name</th><th>Source</th><th>Starts with</th></tr></thead><tbody>
      {documents.map((document) => <tr key={document.start}><td>{document.index}</td><td>{nameFor(document)}</td><td>packet pp {document.start}–{document.end} · {document.pages} pp</td><td>{firstLine(state.pages[document.start]?.ocr_text ?? state.pages[document.start]?.native_text)}</td></tr>)}
    </tbody></table></div>
    <footer><button type="button" className="btn" onClick={onBack}>Back</button><span>{documents.length} {documents.length === 1 ? 'PDF' : 'PDFs'} · {countLabel(pageCount, 'page')} · each linked to the packet and its page range</span><button type="button" className="btn btn-primary" disabled={commitBusy || !state.destinationName.trim() || Boolean(patternError)} onClick={onSubmit}>{commitBusy ? 'Importing…' : `Import ${countLabel(documents.length, 'document')}`}</button></footer>
  </div>;
}

function firstLine(text: string | null | undefined): string {
  return text?.split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? 'No text read yet';
}
