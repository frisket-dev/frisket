import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
} from 'react';
import { Check, FileText, Plus, X } from 'lucide-react';
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
  pdfPacketThumbnailUrl,
  setPdfPacketTextSource,
  startPdfPacketOcr,
  type PdfPacketPageMatch,
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
  matchForPage,
  pdfPacketNamePatternError,
  pdfPacketFlowReducer,
  samplePages,
  type PdfPacketDocument,
  type PdfPacketRememberedOptions,
  type PdfPacketStage,
} from './model';

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

function firstLine(text: string | null | undefined): string {
  return text?.split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? 'No text read yet';
}

function pageMatchLabel(
  match: PdfPacketPageMatch | null,
  threshold: number,
  compact: boolean,
): string {
  if (!match) return '';
  const phraseMatch = match.matched_phrase_ids.length > 0;
  const score = match.visual_score;
  const visualMatch = score != null && score >= threshold;
  if (phraseMatch && visualMatch) {
    return compact
      ? `Text · ${Math.round(score)}%`
      : `Text match · ${Math.round(score)}% visual`;
  }
  if (phraseMatch) return compact ? 'Text' : 'Text match';
  return score == null
    ? ''
    : compact
      ? `${Math.round(score)}%`
      : `${Math.round(score)}% visual`;
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
  const [browsing, setBrowsing] = useState(false);
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
    committedSheet.current = null;
    latestStatus.current = undefined;
    setUploading(false);
    setBrowsing(false);
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
      if (snapshot.status === 'error') {
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
  useEffect(() => {
    if (!splitId || selectedPageOcrJob?.progress.status !== 'done' || !selectedPageOcrJob.engine) return undefined;
    const controller = new AbortController();
    const engine = selectedPageOcrJob.engine;
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
  }, [loadPage, projectId, selectedPageOcrJob?.engine, selectedPageOcrJob?.job_id, selectedPageOcrJob?.progress.status, splitId, state.selectedPage, state.snapshot?.ocr_engine, state.snapshot?.text_source]);

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
      setBrowsing(true);
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
      setBrowsing(false);
      requestId.current = crypto.randomUUID();
      commitRequestId.current = crypto.randomUUID();
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
    if (!splitId || !state.destinationName.trim() || pdfPacketNamePatternError(state.namePattern)) return;
    const { controller, epoch } = beginAction();
    const canKeepOcrText = canKeepPdfPacketOcr(state.snapshot);
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
      {browsing ? (
        <PacketBrowseStep
          projectId={projectId}
          splitId={state.snapshot.split_id}
          snapshot={state.snapshot}
          selectedPage={state.selectedPage}
          onSelectPage={(page) => dispatch({ type: 'selectPage', page })}
          onChooseAnother={() => void chooseAnotherPacket()}
          onContinue={() => setBrowsing(false)}
        />
      ) : state.stage === 'check' ? (
        <PacketTextStep
          projectId={projectId}
          splitId={state.snapshot.split_id}
          snapshot={state.snapshot}
          page={state.pages[state.selectedPage] ?? null}
          selectedPage={state.selectedPage}
          pageCount={pageCount}
          engine={state.ocrEngine}
          showOcr={showOcr}
          busy={state.ocrBusy || state.pageLoading === state.selectedPage}
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

function PacketThumbnail({ projectId, splitId, page, alt = '', eager = false }: { projectId: string; splitId: string; page: number; alt?: string; eager?: boolean }) {
  return <img loading={eager ? 'eager' : 'lazy'} src={pdfPacketThumbnailUrl(projectId, splitId, page)} alt={alt} />;
}

function PacketBrowseStep({
  projectId,
  splitId,
  snapshot,
  selectedPage,
  onSelectPage,
  onChooseAnother,
  onContinue,
}: {
  projectId: string;
  splitId: string;
  snapshot: PdfPacketSplitSnapshot;
  selectedPage: number;
  onSelectPage(page: number): void;
  onChooseAnother(): void;
  onContinue(): void;
}) {
  const pageCount = pageCountOf(snapshot);
  const nativeCount = snapshot.prepare.native_text_pages.length;
  return (
    <div className="pdf-packet-browse">
      <section className="pdf-packet-file-card">
        <PacketThumbnail projectId={projectId} splitId={splitId} page={selectedPage} eager />
        <div>
          <strong>{snapshot.packet.filename}</strong>
          <span>{countLabel(pageCount, 'page')} · {formatBytes(snapshot.packet.size)} · {nativeCount ? `extracted text on ${countLabel(nativeCount, 'page')}` : 'no extracted text found'}</span>
        </div>
        <button type="button" className="btn" onClick={onChooseAnother}>Choose another file</button>
      </section>
      <header className="pdf-packet-browse-heading">
        <strong>Pages</strong>
        <span>Browse the packet before checking its text. The original file is not changed.</span>
      </header>
      <section className="pdf-packet-browse-grid" aria-label="Packet pages">
        {Array.from({ length: pageCount }, (_, index) => index + 1).map((page) => (
          <button
            type="button"
            key={page}
            aria-label={`Select page ${page} for text check`}
            aria-pressed={page === selectedPage}
            onClick={() => onSelectPage(page)}
          >
            <PacketThumbnail projectId={projectId} splitId={splitId} page={page} />
            <span>{page}</span>
          </button>
        ))}
      </section>
      <footer>
        <span>{countLabel(pageCount, 'page')} ready · page {selectedPage} selected for text check</span>
        <button type="button" className="btn btn-primary" onClick={onContinue}>Continue to check text →</button>
      </footer>
    </div>
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
  return (
    <div className="pdf-packet-chooser" data-testid="pdf-packet-chooser">
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
          if (file) onChoose(file);
        }}
      />
      <button type="button" className="btn btn-primary" disabled={uploading} onClick={() => inputRef.current?.click()}>
        {uploading ? 'Reading packet…' : 'Choose PDF packet'}
      </button>
      {error && <div className="import-field-error" role="alert">{error}</div>}
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
  onSelectPage(page: number): void;
  onBack(): void;
  onEngine(engine: string): void;
  onShowOcr(): void;
  onRunSample(): void;
  onAcceptNative(): void;
  onAcceptOcr(): void;
}) {
  const defaultSamples = samplePages(pageCount);
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
        <h3>Sample pages</h3>
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
            <button type="button" className="btn" disabled={busy} onClick={onRunSample}>
              {busy ? 'Reading sample…' : `Run ${engine} on this sample`}
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
            <strong>{source === 'ocr' ? `Text read by ${page?.ocr_engine ?? engine}` : 'Text extracted from this PDF'}</strong>
            {busy ? <p>Reading page…</p> : shownText ? <pre>{shownText}</pre> : (
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
                <button type="button" className="btn" onClick={onShowOcr}>No, run OCR</button>
                <button type="button" className="btn btn-primary" onClick={onAcceptNative}>Yes, use extracted text</button>
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

function PacketFindStep({
  projectId,
  splitId,
  state,
  pageCount,
  dispatch,
  onCancelOcr,
}: {
  projectId: string;
  splitId: string;
  state: ReturnType<typeof pdfPacketFlowReducer>;
  pageCount: number;
  dispatch: PacketDispatch;
  onCancelOcr(jobId: string): void;
}) {
  const gridRef = useRef<HTMLDivElement>(null);
  const [visibleRange, setVisibleRange] = useState<[number, number]>([1, Math.min(40, pageCount)]);
  const questionPages = (state.matches?.question_pages ?? []).filter((page) => (
    !state.confirmedStarts.includes(page) && !state.rejected.includes(page)
  ));
  const suggested = (state.matches?.suggested_pages ?? []).filter((page) => (
    !state.confirmedStarts.includes(page) && !state.rejected.includes(page)
  ));
  const ocrJob = state.snapshot?.jobs.filter((job) => job.kind === 'ocr_full').at(-1);
  const textRead = state.snapshot?.text_source === 'ocr'
    ? state.snapshot.ocr_pages.length
    : (state.snapshot?.text_source === 'native' ? state.snapshot.prepare.native_text_pages.length : 0);
  const documents = confirmedDocuments([...state.confirmedStarts, ...suggested], pageCount);
  const pageToDocument = new Map<number, PdfPacketDocument>();
  for (const document of documents) {
    for (let page = document.start; page <= document.end; page += 1) pageToDocument.set(page, document);
  }
  const focusPage = (page: number) => {
    const cell = gridRef.current?.querySelector<HTMLElement>(`[data-packet-page="${page}"]`);
    cell?.scrollIntoView?.({ block: 'center' });
    cell?.focus();
  };
  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return undefined;
    const updateVisibleRange = () => {
      const gridBounds = grid.getBoundingClientRect();
      const visible = Array.from(grid.querySelectorAll<HTMLElement>('[data-packet-page]'))
        .filter((cell) => {
          const bounds = cell.getBoundingClientRect();
          return bounds.bottom >= gridBounds.top && bounds.top <= gridBounds.bottom;
        })
        .map((cell) => Number(cell.dataset.packetPage));
      if (visible.length) setVisibleRange([Math.min(...visible), Math.max(...visible)]);
    };
    updateVisibleRange();
    grid.addEventListener('scroll', updateVisibleRange, { passive: true });
    window.addEventListener('resize', updateVisibleRange);
    return () => {
      grid.removeEventListener('scroll', updateVisibleRange);
      window.removeEventListener('resize', updateVisibleRange);
    };
  }, [pageCount]);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const page = Number((event.target as HTMLElement).closest<HTMLElement>('[data-packet-page]')?.dataset.packetPage);
    if (!page) return;
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      focusPage(Math.max(1, Math.min(pageCount, page + (event.key === 'ArrowRight' ? 1 : -1))));
    } else if (event.key.toLowerCase() === 's') {
      event.preventDefault();
      dispatch({ type: 'toggleStart', page });
    } else if (event.key.toLowerCase() === 'y') {
      const top = questionPages[0];
      if (top) dispatch({ type: 'confirmStart', page: top });
    } else if (event.key.toLowerCase() === 'n') {
      const top = questionPages[0];
      if (top) dispatch({ type: 'reject', page: top });
    } else if (event.key.toLowerCase() === 'a' && suggested.includes(page)) {
      dispatch({ type: 'confirmStart', page });
    }
  };
  return (
    <div className="pdf-packet-find">
      <section className="pdf-packet-find-main">
        <header className="pdf-packet-grid-toolbar">
          <span><i className="confirmed" /> Start you marked</span>
          <span><i className="suggested" /> Suggested</span>
          <span><i className="question" /> Question</span>
          <span className="spacer" />
          <span>Text: {textRead} of {pageCount} read</span>
        </header>
        <div className="pdf-packet-grid-with-map">
          <div className="pdf-packet-page-grid" ref={gridRef} onKeyDown={onKeyDown}>
            {Array.from({ length: pageCount }, (_, index) => index + 1).map((page) => {
              const document = pageToDocument.get(page)!;
              const confirmed = state.confirmedStarts.includes(page);
              const rejected = state.rejected.includes(page);
              const candidate = matchForPage(state.matches, page);
              const isSuggested = suggested.includes(page) && !confirmed;
              const isQuestion = questionPages.includes(page) && !confirmed;
              const matchLabel = pageMatchLabel(candidate, state.threshold, true);
              const matchDescription = pageMatchLabel(candidate, state.threshold, false);
              return (
                <button
                  type="button"
                  key={page}
                  className="pdf-packet-page-cell"
                  data-packet-page={page}
                  data-document-tone={document.index % 2 ? 'indigo' : 'sand'}
                  data-document-kind={suggested.includes(document.start) ? 'suggested' : 'confirmed'}
                  data-confirmed={confirmed || undefined}
                  data-suggested={isSuggested || undefined}
                  data-question={isQuestion || undefined}
                  aria-label={`Page ${page}${confirmed ? ', document start' : isSuggested ? ', suggested start' : ''}`}
                  aria-pressed={confirmed}
                  title={`Page ${page}${matchDescription ? `. ${matchDescription}.` : '.'} Click to toggle document start; double-click to inspect the page.`}
                  onClick={() => dispatch({ type: 'toggleStart', page })}
                  onDoubleClick={() => window.open(pdfPacketThumbnailUrl(projectId, splitId, page), '_blank', 'noopener,noreferrer')}
                >
                  <span className="pdf-packet-doc-label">{confirmed ? `Doc ${document.index}` : isSuggested ? `Doc ${document.index}?` : '\u00a0'}</span>
                  <span className="pdf-packet-page-row">
                    {!confirmed && <i className="pdf-packet-connector" />}
                    <PacketThumbnail projectId={projectId} splitId={splitId} page={page} />
                  </span>
                  <span className="pdf-packet-page-meta">
                    <span>{page}</span>
                    <span title={isSuggested && matchLabel ? matchDescription : undefined}>
                      {rejected ? 'not a start' : isQuestion ? '?' : isSuggested ? matchLabel : ''}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
          <PacketMinimap
            pageCount={pageCount}
            confirmed={state.confirmedStarts}
            suggested={suggested}
            questions={questionPages}
            visibleRange={visibleRange}
            onJump={(page) => gridRef.current?.querySelector<HTMLElement>(`[data-packet-page="${page}"]`)?.scrollIntoView({ block: 'start' })}
          />
        </div>
      </section>
      <aside className="pdf-packet-method-panel">
        {ocrJob && (ocrJob.progress.status === 'queued' || ocrJob.progress.status === 'running') ? (
          <div className="pdf-packet-ocr-progress" role="status">
            <span>Reading text · {ocrJob.progress.done} of {ocrJob.progress.total ?? pageCount}</span>
            <progress max={ocrJob.progress.total ?? pageCount} value={ocrJob.progress.done} />
            <button type="button" className="mini-btn" onClick={() => onCancelOcr(ocrJob.job_id)}>Stop OCR</button>
          </div>
        ) : null}
        <SegmentedToggle
          ariaLabel="How to find document starts"
          value={state.method}
          onValueChange={(value) => dispatch({ type: 'method', method: value as 'visual' | 'text' | 'manual' })}
          options={[{ value: 'visual', label: 'Visual' }, { value: 'text', label: 'Text' }, { value: 'manual', label: 'Manual' }]}
        />
        {state.method === 'visual' ? (
          <div className="pdf-packet-method-copy">
            <h3>Pages that look like your starts</h3>
            <p>Learning from {countLabel(state.confirmedStarts.length, 'start')} you marked (and {state.rejected.length} you rejected). Different kinds of start page are grouped automatically.</p>
            <label>
              <span><strong>How similar</strong><strong>{state.threshold}%</strong></span>
              <input type="range" min="50" max="99" value={state.threshold} onChange={(event) => dispatch({ type: 'threshold', value: Number(event.target.value) })} />
              <small><span>More suggestions</span><span>Fewer, closer</span></small>
            </label>
          </div>
        ) : state.method === 'text' ? (
          <div className="pdf-packet-method-copy">
            <h3>Phrases on first pages</h3>
            {state.phrases.map((phrase) => (
              <div className="pdf-packet-phrase" key={phrase.id}>
                <input type="checkbox" checked={phrase.enabled} aria-label={`Enable ${phrase.text || 'phrase'}`} onChange={(event) => dispatch({ type: 'phrase', id: phrase.id, patch: { enabled: event.target.checked } })} />
                <input className="form-input mono" value={phrase.text} placeholder="First-page phrase" onChange={(event) => dispatch({ type: 'phrase', id: phrase.id, patch: { text: event.target.value } })} />
                <span>{state.matches?.phrase_counts[phrase.id] ?? 0}</span>
                <button type="button" className="icon-btn" aria-label="Remove phrase" onClick={() => dispatch({ type: 'removePhrase', id: phrase.id })}><X size={13} /></button>
              </div>
            ))}
            <button type="button" className="btn" onClick={() => dispatch({ type: 'addPhrase' })}><Plus size={13} /> Add a phrase</button>
            <label className="import-check-inline"><input type="checkbox" checked={state.fuzzy} onChange={(event) => dispatch({ type: 'fuzzy', value: event.target.checked })} /> Allow small OCR errors</label>
            <p>Pages still being read are checked as their text arrives.</p>
          </div>
        ) : (
          <div className="pdf-packet-method-copy"><h3>Mark starts yourself</h3><p>Click any page to mark it as the first page of a document. Click again to remove it.</p></div>
        )}
        {questionPages.length > 0 && (
          <section className="pdf-packet-questions">
            <header><strong>Does a document start here?</strong><span>{questionPages.length} to check</span></header>
            {questionPages.map((page) => <QuestionRow key={page} projectId={projectId} splitId={splitId} page={page} match={matchForPage(state.matches, page)} threshold={state.threshold} onFocus={() => focusPage(page)} onYes={() => dispatch({ type: 'confirmStart', page })} onNo={() => dispatch({ type: 'reject', page })} />)}
          </section>
        )}
        <section className="pdf-packet-suggestions">
          <header><strong>{countLabel(suggested.length, 'suggested start')} in this packet</strong><button type="button" disabled={!suggested.length} onClick={() => dispatch({ type: 'acceptAll' })}>Accept all {suggested.length}</button></header>
          {suggested.slice(0, 8).map((page) => (
            <QuestionRow key={page} projectId={projectId} splitId={splitId} page={page} match={matchForPage(state.matches, page)} threshold={state.threshold} onFocus={() => focusPage(page)} onYes={() => dispatch({ type: 'confirmStart', page })} onNo={() => dispatch({ type: 'reject', page })} compact />
          ))}
        </section>
      </aside>
      <footer className="pdf-packet-find-footer">
        <span>{state.confirmedStarts.length} marked {state.confirmedStarts.length === 1 ? 'start' : 'starts'} will be imported · {suggested.length} {suggested.length === 1 ? 'suggestion remains' : 'suggestions remain'} unaccepted</span>
        <button type="button" className="btn btn-primary" onClick={() => dispatch({ type: 'stage', stage: 'import' })}>Continue with {state.confirmedStarts.length} marked {state.confirmedStarts.length === 1 ? 'start' : 'starts'} →</button>
      </footer>
    </div>
  );
}

function QuestionRow({ projectId, splitId, page, match, threshold, onFocus, onYes, onNo, compact = false }: { projectId: string; splitId: string; page: number; match: PdfPacketPageMatch | null; threshold: number; onFocus(): void; onYes(): void; onNo(): void; compact?: boolean }) {
  const matchLabel = pageMatchLabel(match, threshold, false);
  return <div className="pdf-packet-question-row" data-compact={compact || undefined}>
    <button type="button" className="pdf-packet-question-thumb" aria-label={`View page ${page} in grid`} onClick={onFocus}>
      <PacketThumbnail projectId={projectId} splitId={splitId} page={page} />
    </button>
    <button type="button" className="pdf-packet-question-page" onClick={onFocus}>Page {page}{matchLabel ? ` · ${matchLabel}` : ''}</button>
    <button type="button" className="mini-btn pdf-packet-yes" aria-label={`Yes, page ${page} starts a document`} onClick={onYes}><Check size={12} /> Yes</button>
    <button type="button" className="mini-btn" aria-label={`No, page ${page} does not start a document`} onClick={onNo}><X size={12} /> No</button>
  </div>;
}

function PacketMinimap({ pageCount, confirmed, suggested, questions, visibleRange, onJump }: { pageCount: number; confirmed: number[]; suggested: number[]; questions: number[]; visibleRange: [number, number]; onJump(page: number): void }) {
  const mapRef = useRef<HTMLDivElement>(null);
  const rows = Math.ceil(pageCount / 10);
  const jumpFromPointer = (clientX: number, clientY: number) => {
    const bounds = mapRef.current?.getBoundingClientRect();
    if (!bounds) return;
    const column = Math.min(9, Math.max(0, Math.floor((clientX - bounds.left) / bounds.width * 10)));
    const row = Math.min(rows - 1, Math.max(0, Math.floor((clientY - bounds.top) / bounds.height * rows)));
    onJump(Math.min(pageCount, row * 10 + column + 1));
  };
  const firstRow = Math.floor((visibleRange[0] - 1) / 10);
  const lastRow = Math.floor((visibleRange[1] - 1) / 10);
  return <aside className="pdf-packet-minimap" aria-label="Packet minimap">
    <div
      ref={mapRef}
      className="pdf-packet-minimap-map"
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        jumpFromPointer(event.clientX, event.clientY);
      }}
      onPointerMove={(event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) jumpFromPointer(event.clientX, event.clientY);
      }}
    >
      <i
        className="pdf-packet-minimap-viewport"
        style={{ top: `${firstRow / rows * 100}%`, height: `${Math.max(1, lastRow - firstRow + 1) / rows * 100}%` }}
      />
      {Array.from({ length: pageCount }, (_, index) => index + 1).map((page) => (
      <button
        type="button"
        key={page}
        aria-label={`Jump to page ${page}`}
        data-state={confirmed.includes(page) ? 'confirmed' : suggested.includes(page) ? 'suggested' : questions.includes(page) ? 'question' : 'other'}
        onClick={() => onJump(page)}
      />
    ))}</div>
    <span>{visibleRange[0]}–{visibleRange[1]} of {pageCount}</span>
  </aside>;
}

function PacketImportStep({ state, pageCount, canKeepOcrText, onBack, onSubmit, dispatch }: { state: ReturnType<typeof pdfPacketFlowReducer>; pageCount: number; canKeepOcrText: boolean; onBack(): void; onSubmit(): void; dispatch: PacketDispatch }) {
  const documents = confirmedDocuments(state.confirmedStarts, pageCount);
  const patternError = pdfPacketNamePatternError(state.namePattern);
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
    <footer><button type="button" className="btn" onClick={onBack}>Back</button><span>{documents.length} {documents.length === 1 ? 'PDF' : 'PDFs'} · {countLabel(pageCount, 'page')} · each linked to the packet and its page range</span><button type="button" className="btn btn-primary" disabled={state.committing || !state.destinationName.trim() || Boolean(patternError)} onClick={onSubmit}>{state.committing ? 'Starting import…' : `Import ${countLabel(documents.length, 'document')}`}</button></footer>
  </div>;
}
