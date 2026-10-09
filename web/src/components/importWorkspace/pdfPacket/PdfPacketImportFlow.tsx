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
import { PanelSelect } from '../../PanelSelect';
import { SegmentedToggle } from '../../PanelPrimitives';
import {
  confirmedDocuments,
  initialPdfPacketFlowState,
  matchForPage,
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
  const splitId = state.snapshot?.split_id ?? null;
  const pageCount = pageCountOf(state.snapshot);
  const candidateGeneration = useRef(0);
  const requestId = useRef(crypto.randomUUID());
  const commitRequestId = useRef(crypto.randomUUID());
  const committedSheet = useRef<number | null>(null);
  const latestStatus = useRef(state.snapshot?.status);
  const serverCommitBusy = state.snapshot?.status === 'committing';

  useEffect(() => {
    latestStatus.current = state.snapshot?.status;
  }, [state.snapshot?.status]);

  useEffect(() => {
    onContextChange({
      active: state.snapshot !== null,
      stage: state.stage,
      subtitle: state.snapshot
        ? `${state.snapshot.packet.filename}${pageCount ? ` · ${pageCount} pages` : ''}`
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
    try {
      const snapshot = await getPdfPacketSplit(projectId, splitId, { signal });
      if (signal?.aborted) return;
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
    dispatch({ type: 'pageLoading', page });
    try {
      const loaded = await getPdfPacketPage(projectId, splitId, page, { signal });
      if (!signal?.aborted) dispatch({ type: 'pageLoaded', page: loaded });
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

  const selectedPageOcrJobState = state.snapshot?.jobs
    .filter((job) => job.kind === 'ocr_sample' && job.pages.includes(state.selectedPage))
    .at(-1)?.progress.status;
  useEffect(() => {
    if (!splitId || selectedPageOcrJobState !== 'done') return undefined;
    const controller = new AbortController();
    void loadPage(state.selectedPage, controller.signal);
    return () => controller.abort();
  }, [loadPage, selectedPageOcrJobState, splitId, state.selectedPage]);

  useEffect(() => {
    if (!splitId || state.stage !== 'find' || state.snapshot?.status === 'preparing') return undefined;
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
  }, [projectId, splitId, state.confirmedStarts, state.phrases, state.rejected, state.snapshot?.status, state.stage, state.threshold]);

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
    setUploading(true);
    dispatch({ type: 'error', message: null });
    try {
      const snapshot = await createPdfPacketSplit(projectId, file, requestId.current);
      dispatch({ type: 'prepared', snapshot, remembered: loadRememberedOptions(projectId) });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      dispatch({ type: 'error', message });
      onError(`Import failed: ${message}`);
    } finally {
      setUploading(false);
    }
  };

  const runOcr = async (scope: 'sample' | 'all') => {
    if (!splitId) return;
    dispatch({ type: 'ocrBusy', value: true });
    dispatch({ type: 'error', message: null });
    const pages = scope === 'sample' ? [state.selectedPage] : undefined;
    try {
      const quote = await estimatePdfPacketOcr(projectId, splitId, {
        engine: state.ocrEngine,
        scope,
        pages,
      });
      const promiseHash = typeof quote.estimate.promise_set_hash === 'string'
        ? quote.estimate.promise_set_hash
        : undefined;
      await startPdfPacketOcr(projectId, splitId, {
        engine: state.ocrEngine,
        scope,
        pages,
        confirmation: promiseHash,
      });
      if (scope === 'all') {
        await setPdfPacketTextSource(projectId, splitId, { kind: 'ocr', engine: state.ocrEngine });
        dispatch({ type: 'stage', stage: 'find' });
      }
      await refreshSnapshot();
    } catch (error) {
      dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    } finally {
      dispatch({ type: 'ocrBusy', value: false });
    }
  };

  const acceptNativeText = async () => {
    if (!splitId) return;
    try {
      const snapshot = await setPdfPacketTextSource(projectId, splitId, { kind: 'native' });
      dispatch({ type: 'snapshot', snapshot });
      dispatch({ type: 'stage', stage: 'find' });
    } catch (error) {
      dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  };

  const chooseAnotherPacket = async () => {
    if (!splitId) return;
    try {
      await deletePdfPacketSplit(projectId, splitId);
      latestStatus.current = 'cancelled';
      requestId.current = crypto.randomUUID();
      commitRequestId.current = crypto.randomUUID();
      setShowOcr(false);
      dispatch({ type: 'reset' });
    } catch (error) {
      dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  };

  const cancelOcr = async (jobId: string) => {
    if (!splitId) return;
    try {
      const snapshot = await cancelPdfPacketOcr(projectId, splitId, jobId);
      dispatch({ type: 'snapshot', snapshot });
    } catch (error) {
      dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    }
  };

  const submitImport = async () => {
    if (!splitId || !state.destinationName.trim()) return;
    dispatch({ type: 'committing', value: true });
    try {
      await commitPdfPacketSplit(projectId, splitId, {
        idempotency_key: commitRequestId.current,
        confirmed_starts: state.confirmedStarts,
        destination: { kind: 'new_sheet', name: state.destinationName.trim() },
        name_pattern: state.namePattern,
        keep_ocr_text: state.keepOcrText,
        remember_options: state.rememberOptions,
      });
      if (state.rememberOptions) {
        window.localStorage.setItem(rememberedOptionsKey(projectId), JSON.stringify({
          namePattern: state.namePattern,
          keepOcrText: state.keepOcrText,
        } satisfies PdfPacketRememberedOptions));
      } else {
        window.localStorage.removeItem(rememberedOptionsKey(projectId));
      }
      await refreshSnapshot();
    } catch (error) {
      dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
    } finally {
      dispatch({ type: 'committing', value: false });
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
          onBack={() => dispatch({ type: 'stage', stage: 'find' })}
          onSubmit={() => void submitImport()}
          dispatch={dispatch}
        />
      )}
      {state.error && <div className="pdf-packet-error import-field-error" role="alert">{state.error}</div>}
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
  const samples = samplePages(pageCount);
  const nativeText = page?.native_text ?? '';
  const ocrText = page?.ocr_text ?? '';
  const [preferredSource, setPreferredSource] = useState<'native' | 'ocr'>('native');
  const source = (preferredSource === 'ocr' && ocrText) || (!nativeText && ocrText)
    ? 'ocr'
    : 'native';
  const shownText = source === 'ocr' ? ocrText : nativeText;
  const nativeCount = snapshot.prepare.native_text_pages.length;
  return (
    <div className="pdf-packet-check">
      <aside className="pdf-packet-check-sidebar">
        <h3>Text from the PDF</h3>
        <p>{nativeCount > 0
          ? `Extracted text is available on ${nativeCount} of ${pageCount} pages.`
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
              <img src={pdfPacketThumbnailUrl(projectId, splitId, sample)} alt="" />
              <span>p {sample}</span>
            </button>
          ))}
        </div>
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
                if (selected) onEngine(selected);
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
          <img src={pdfPacketThumbnailUrl(projectId, splitId, selectedPage)} alt={`Page ${selectedPage}`} />
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
                Use this OCR and read all {pageCount} pages
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
  const questionPages = state.matches?.question_pages ?? [];
  const suggested = state.matches?.suggested_pages ?? [];
  const ocrJob = state.snapshot?.jobs.find((job) => job.kind === 'ocr_full');
  const textRead = ocrJob?.progress.done
    ?? (state.snapshot?.text_source === 'native' ? state.snapshot.prepare.native_text_pages.length : 0);
  const documents = confirmedDocuments([...state.confirmedStarts, ...suggested], pageCount);
  const pageToDocument = new Map<number, PdfPacketDocument>();
  for (const document of documents) {
    for (let page = document.start; page <= document.end; page += 1) pageToDocument.set(page, document);
  }
  const focusPage = (page: number) => {
    gridRef.current?.querySelector<HTMLElement>(`[data-packet-page="${page}"]`)?.focus();
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
                  title={`Page ${page}. Click to toggle document start; double-click to inspect the page.`}
                  onClick={() => dispatch({ type: 'toggleStart', page })}
                  onDoubleClick={() => window.open(pdfPacketThumbnailUrl(projectId, splitId, page), '_blank', 'noopener,noreferrer')}
                >
                  <span className="pdf-packet-doc-label">{confirmed ? `Doc ${document.index}` : isSuggested ? `Doc ${document.index}?` : '\u00a0'}</span>
                  <span className="pdf-packet-page-row">
                    {!confirmed && <i className="pdf-packet-connector" />}
                    <img loading="lazy" src={pdfPacketThumbnailUrl(projectId, splitId, page)} alt="" />
                  </span>
                  <span className="pdf-packet-page-meta">
                    <span>{page}</span>
                    <span>{rejected ? 'not a start' : isQuestion ? '?' : isSuggested && candidate?.visual_score != null ? `${Math.round(candidate.visual_score)}%` : ''}</span>
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
            <p>Learning from {state.confirmedStarts.length} starts you marked (and {state.rejected.length} you rejected). Different kinds of start page are grouped automatically.</p>
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
            {questionPages.map((page) => <QuestionRow key={page} page={page} match={matchForPage(state.matches, page)} onYes={() => dispatch({ type: 'confirmStart', page })} onNo={() => dispatch({ type: 'reject', page })} />)}
          </section>
        )}
        <section className="pdf-packet-suggestions">
          <header><strong>{suggested.length} suggested starts in this packet</strong><button type="button" disabled={!suggested.length} onClick={() => dispatch({ type: 'acceptAll' })}>Accept all {suggested.length}</button></header>
          {suggested.slice(0, 8).map((page) => (
            <QuestionRow key={page} page={page} match={matchForPage(state.matches, page)} onYes={() => dispatch({ type: 'confirmStart', page })} onNo={() => dispatch({ type: 'reject', page })} compact />
          ))}
        </section>
      </aside>
      <footer className="pdf-packet-find-footer">
        <span>{state.confirmedStarts.length} marked · {suggested.length} suggestions unaccepted</span>
        <button type="button" className="btn btn-primary" onClick={() => dispatch({ type: 'stage', stage: 'import' })}>Continue to import →</button>
      </footer>
    </div>
  );
}

function QuestionRow({ page, match, onYes, onNo, compact = false }: { page: number; match: PdfPacketPageMatch | null; onYes(): void; onNo(): void; compact?: boolean }) {
  return <div className="pdf-packet-question-row" data-compact={compact || undefined}>
    <span>Page {page}{match?.visual_score != null ? ` · ${Math.round(match.visual_score)}%` : match?.matched_phrase_ids.length ? ' · phrase match' : ''}</span>
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

function PacketImportStep({ state, pageCount, onBack, onSubmit, dispatch }: { state: ReturnType<typeof pdfPacketFlowReducer>; pageCount: number; onBack(): void; onSubmit(): void; dispatch: PacketDispatch }) {
  const documents = confirmedDocuments(state.confirmedStarts, pageCount);
  const packetBase = state.snapshot!.packet.filename.replace(/\.pdf$/i, '');
  const nameFor = (document: PdfPacketDocument) => state.namePattern
    .replaceAll('{packet}', packetBase)
    .replaceAll('{start}', String(document.start))
    .replaceAll('{end}', String(document.end));
  return <div className="pdf-packet-import-step">
    <header><h2>Import {documents.length} documents</h2><p>Each becomes a normal PDF. The original packet stays as it is.</p></header>
    <div className="pdf-packet-import-options">
      <label>Destination
        <PanelSelect className="form-input" value="new" disabled>
          <option value="new">New sheet</option>
        </PanelSelect>
        <input className="form-input" aria-label="New sheet name" value={state.destinationName} onChange={(event) => dispatch({ type: 'destinationName', value: event.target.value })} />
      </label>
      <label>File names<input className="form-input mono" value={state.namePattern} onChange={(event) => dispatch({ type: 'namePattern', value: event.target.value })} /></label>
      <label className="import-check-inline"><input type="checkbox" checked={state.keepOcrText} onChange={(event) => dispatch({ type: 'keepOcrText', value: event.target.checked })} /> Keep OCR text and boxes with each PDF</label>
      <label className="import-check-inline"><input type="checkbox" checked={state.rememberOptions} onChange={(event) => dispatch({ type: 'rememberOptions', value: event.target.checked })} /> Remember these options for the next split</label>
      <p>OCR stays as ordinary Frisket text and positioned boxes. The PDF files are not given a new text layer.</p>
    </div>
    <div className="pdf-packet-import-table"><table><thead><tr><th>#</th><th>Name</th><th>Source</th><th>Starts with</th></tr></thead><tbody>
      {documents.map((document) => <tr key={document.start}><td>{document.index}</td><td>{nameFor(document)}</td><td>packet pp {document.start}–{document.end} · {document.pages} pp</td><td>{firstLine(state.pages[document.start]?.ocr_text ?? state.pages[document.start]?.native_text)}</td></tr>)}
    </tbody></table></div>
    <footer><button type="button" className="btn" onClick={onBack}>Back</button><span>{documents.length} PDFs · {pageCount} pages · each linked to the packet and its page range</span><button type="button" className="btn btn-primary" disabled={state.committing || !state.destinationName.trim()} onClick={onSubmit}>{state.committing ? 'Starting import…' : `Import ${documents.length} documents`}</button></footer>
  </div>;
}
