import { Button } from '../../Button';
import { TextInput } from '../../FormControls';
import {
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type Dispatch,
  type KeyboardEvent,
  type MouseEvent,
} from 'react';
import { Check, Expand, Minus, Plus, X } from 'lucide-react';
import {
  type PdfPacketPageMatch,
} from '../../../api/pdfPacketSplits';
import { countLabel } from '../../../format';
import { SegmentedToggle } from '../../PanelPrimitives';
import { ProgressBar } from '../../ActivityProgress';
import {
  confirmedDocuments,
  matchForPage,
  pdfPacketFlowReducer,
  type PdfPacketDocument,
} from './model';
import './PacketFindStep.css';
import { PacketThumbnail } from './PacketThumbnail';
import { PacketReviewCard, type ReviewTab } from './PacketReviewCard';
import { packetFindQueues } from './packetFindQueues';
import { pageAddress, type ReviewSource } from './reviewSources';

const PacketLargePageView = lazy(() => import('./PacketLargePageView')
  .then((module) => ({ default: module.PacketLargePageView })));

const GRID_SIZES = [44, 57, 80, 110] as const;

type PacketDispatch = Dispatch<Parameters<typeof pdfPacketFlowReducer>[1]>;
type GridFilter = 'all' | 'starts';

export interface PacketFindStepProps {
  projectId: string;
  splitId: string;
  state: ReturnType<typeof pdfPacketFlowReducer>;
  pageCount: number;
  dispatch: PacketDispatch;
  onCancelOcr(jobId: string): void;
  sources?: readonly ReviewSource[];
  onContinue(): void;
  primaryLabel?: string | ((documents: number) => string);
  continueDisabled?: boolean;
  onBack?(): void;
}

function pageMatchLabel(match: PdfPacketPageMatch | null, threshold: number, compact = false): string {
  if (!match) return '';
  const phraseMatch = match.matched_phrase_ids.length > 0;
  const score = match.visual_score;
  const visualMatch = score != null && score >= threshold;
  if (phraseMatch && visualMatch) {
    return compact ? `Text · ${Math.round(score)}%` : `Text match · ${Math.round(score)}% visual`;
  }
  if (phraseMatch) return compact ? 'Text' : 'Text match';
  if (score == null) return '';
  return compact ? `${Math.round(score)}%` : `${Math.round(score)}% visual`;
}

function stopAction(event: MouseEvent<HTMLButtonElement>, action: () => void) {
  event.preventDefault();
  event.stopPropagation();
  action();
}

function PacketMinimap({
  pageCount,
  confirmed,
  suggested,
  unsure,
  visibleRange,
  onJump,
  fixed = [], examples = [], sourceCount,
}: {
  pageCount: number;
  fixed?: readonly number[]; examples?: readonly number[]; sourceCount?: number;
  confirmed: readonly number[];
  suggested: readonly number[];
  unsure: readonly number[];
  visibleRange: [number, number];
  onJump(page: number): void;
}) {
  const mapRef = useRef<HTMLDivElement>(null);
  const rows = Math.max(1, Math.ceil(pageCount / 10));
  const jumpFromPointer = (clientX: number, clientY: number) => {
    const bounds = mapRef.current?.getBoundingClientRect();
    if (!bounds || !bounds.width || !bounds.height) return;
    const column = Math.min(9, Math.max(0, Math.floor((clientX - bounds.left) / bounds.width * 10)));
    const row = Math.min(rows - 1, Math.max(0, Math.floor((clientY - bounds.top) / bounds.height * rows)));
    onJump(Math.min(pageCount, row * 10 + column + 1));
  };
  const firstRow = Math.floor((visibleRange[0] - 1) / 10);
  const lastRow = Math.floor((visibleRange[1] - 1) / 10);

  return (
    <aside className="pdf-packet-minimap" aria-label="Packet minimap">
      <div
        ref={mapRef}
        className="pdf-packet-minimap-map"
        role="slider" tabIndex={0} aria-label="Page map" aria-valuemin={1} aria-valuemax={pageCount} aria-valuenow={visibleRange[0]}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowRight' || event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
            event.preventDefault();
            const delta = event.key === 'ArrowDown' ? 10 : event.key === 'ArrowUp' ? -10 : event.key === 'ArrowRight' ? 1 : -1;
            onJump(Math.max(1, Math.min(pageCount, visibleRange[0] + delta)));
          }
        }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          jumpFromPointer(event.clientX, event.clientY);
        }}
        onPointerMove={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            jumpFromPointer(event.clientX, event.clientY);
          }
        }}
      >
        <i
          className="pdf-packet-minimap-viewport"
          style={{
            top: `${firstRow / rows * 100}%`,
            height: `${Math.max(1, lastRow - firstRow + 1) / rows * 100}%`,
          }}
        />
        {Array.from({ length: Math.min(pageCount, 200) }, (_, index) => {
          const first = Math.floor(index * pageCount / Math.min(pageCount, 200)) + 1;
          const last = Math.floor((index + 1) * pageCount / Math.min(pageCount, 200));
          const has = (items: readonly number[]) => items.some((page) => page >= first && page <= last);
          const tone = has(examples) ? 'confirmed' : has(fixed) ? 'fixed' : has(confirmed) ? 'confirmed' : has(suggested) ? 'suggested' : has(unsure) ? 'unsure' : 'other';
          return <i key={index} data-state={tone} />;
        })}
      </div>
      <span>{visibleRange[0]}–{visibleRange[1]} of {pageCount}{sourceCount ? ` · ${sourceCount} files` : ''}</span>
    </aside>
  );
}

export function PacketFindStep({
  projectId,
  splitId,
  state,
  pageCount,
  dispatch,
  onCancelOcr,
  sources, onContinue, primaryLabel, onBack, continueDisabled,
}: PacketFindStepProps) {
  const gridRef = useRef<HTMLDivElement>(null);
  const liveMatches = state.matches;
  const [gridSizeIndex, setGridSizeIndex] = useState(1);
  const [gridFilter, setGridFilter] = useState<GridFilter>('all');
  const [reviewTab, setReviewTab] = useState<ReviewTab>('unsure');
  const [reviewIndex, setReviewIndex] = useState(0);
  const [largeViewPage, setLargeViewPage] = useState<number | null>(null);
  const [reviewFeedback, setReviewFeedback] = useState('');
  const [viewport, setViewport] = useState({ width: 0, height: 0, scrollTop: 0 });

  const effectiveMatches = liveMatches;
  const { unsure, suggested } = packetFindQueues(
    effectiveMatches,
    state.confirmedStarts,
    state.rejected,
  );
  const ocrJob = state.snapshot?.jobs.filter((job) => job.kind === 'ocr_full').at(-1);
  const prepared = state.snapshot?.prepare as (NonNullable<typeof state.snapshot>['prepare'] & { text_pages_read?: number[] }) | undefined;
  const textRead = prepared?.text_pages_read?.length ?? (state.snapshot
    ? new Set([...state.snapshot.prepare.native_text_pages, ...state.snapshot.ocr_pages]).size
    : 0);
  const documents = confirmedDocuments([...state.confirmedStarts, ...suggested], pageCount);
  const pageToDocument = new Map<number, PdfPacketDocument>();
  for (const document of documents) {
    for (let page = document.start; page <= document.end; page += 1) pageToDocument.set(page, document);
  }
  const startPages = [...new Set([...state.confirmedStarts, ...suggested, ...unsure])].sort((a, b) => a - b);
  const displayedPages = gridFilter === 'all'
    ? Array.from({ length: pageCount }, (_, index) => index + 1)
    : startPages;

  const rowHeight = Math.ceil(GRID_SIZES[gridSizeIndex] * 1.55 + 26);
  const columns = viewport.width ? Math.max(1, Math.floor(viewport.width / (GRID_SIZES[gridSizeIndex] + 18))) : 8;
  const firstRow = Math.max(0, Math.floor(viewport.scrollTop / rowHeight) - 2);
  const rowCount = Math.ceil(displayedPages.length / columns);
  const lastRow = Math.min(rowCount, firstRow + (viewport.height ? Math.ceil(viewport.height / rowHeight) + 4 : 12));
  const windowPages = displayedPages.slice(firstRow * columns, lastRow * columns);

  const visibleRange: [number, number] = windowPages.length ? [windowPages[0], windowPages[windowPages.length - 1]] : [1, Math.min(1, pageCount)];
  const focusPage = (page: number) => {
    const grid = gridRef.current;
    if (!grid) return;
    const position = displayedPages.indexOf(page);
    if (position < 0) return;
    grid.scrollTop = Math.floor(position / columns) * rowHeight;
    setViewport({ width: grid.clientWidth, height: grid.clientHeight, scrollTop: grid.scrollTop });
    window.requestAnimationFrame(() => grid.querySelector<HTMLElement>(`[data-packet-page="${page}"] .pdf-packet-page-toggle`)?.focus());
  };

  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return undefined;
    const update = () => setViewport({ width: grid.clientWidth, height: grid.clientHeight, scrollTop: grid.scrollTop });
    update();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(grid);
    grid.addEventListener('scroll', update, { passive: true });
    return () => { observer?.disconnect(); grid.removeEventListener('scroll', update); };
  }, [gridFilter, gridSizeIndex]);
  const onGridKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const cell = (event.target as HTMLElement).closest<HTMLElement>('[data-packet-page]');
    const page = Number(cell?.dataset.packetPage);
    if (!page) return;
    const position = displayedPages.indexOf(page);
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      const nextPosition = Math.max(0, Math.min(displayedPages.length - 1, position + (event.key === 'ArrowRight' ? 1 : -1)));
      focusPage(displayedPages[nextPosition]);
    } else if (event.key.toLowerCase() === 's' && !event.repeat
      && !event.ctrlKey && !event.metaKey && !event.altKey
      && (event.target as HTMLElement).classList.contains('pdf-packet-page-toggle')) {
      event.preventDefault();
      dispatch({ type: 'toggleStart', page });
    }
  };

  const clearCandidatesAndDispatch = (action: Parameters<typeof dispatch>[0]) => {
    dispatch(action);
  };
  const confirmPage = (page: number) => {
    setReviewFeedback(`Page ${page} marked as a document start.`);
    dispatch({ type: 'confirmStart', page });
  };
  const rejectPage = (page: number) => {
    setReviewFeedback(`Page ${page} marked not a start.`);
    dispatch({ type: 'reject', page });
  };
  const acceptAllSuggestions = () => {
    if (!state.matches) return;
    const pages = [...suggested];
    for (const page of pages) dispatch({ type: 'confirmStart', page });
    setReviewFeedback(`${pages.length} suggested ${pages.length === 1 ? 'start' : 'starts'} accepted.`);
  };

  return (
    <div
      className="pdf-packet-find packet-find-step"
      data-grid-size={gridSizeIndex}
      style={{ '--packet-thumb-width': `${GRID_SIZES[gridSizeIndex]}px` } as CSSProperties}
    >
      <section className="pdf-packet-find-main">
        <header className="pdf-packet-grid-toolbar">
          {sources && sources.length > 1 && <span><i className="fixed" /> File start</span>}
          <span><i className="confirmed" /> Marked</span>
          <span><i className="suggested" /> Suggested</span>
          <span><i className="unsure" /> Unsure</span>
          <SegmentedToggle
            ariaLabel="Pages shown"
            size="compact"
            value={gridFilter}
            onValueChange={(value) => { setGridFilter(value as GridFilter); if (gridRef.current) gridRef.current.scrollTop = 0; setViewport((current) => ({ ...current, scrollTop: 0 })); }}
            options={[
              { value: 'all', label: `All pages ${pageCount}` },
              { value: 'starts', label: `Starts only ${startPages.length}` },
            ]}
            fullWidth={false}
            className="segmented-toolbar packet-grid-filter"
          />
          <span className="spacer" />
          <span className="packet-text-read">Text: {textRead} of {pageCount} read</span>
          <div className="packet-grid-size-control">
            <button
              type="button"
              aria-label="Zoom out"
              disabled={gridSizeIndex === 0}
              onClick={() => setGridSizeIndex((size) => Math.max(0, size - 1))}
            >
              <Minus size={13} />
            </button>
            <input
              type="range"
              min="0"
              max="3"
              step="1"
              value={gridSizeIndex}
              aria-label="Page size"
              onChange={(event) => setGridSizeIndex(Number(event.target.value))}
            />
            <button
              type="button"
              aria-label="Zoom in"
              disabled={gridSizeIndex === GRID_SIZES.length - 1}
              onClick={() => setGridSizeIndex((size) => Math.min(GRID_SIZES.length - 1, size + 1))}
            >
              <Plus size={13} />
            </button>
          </div>
        </header>
        <div className="pdf-packet-grid-with-map">
          <div className="pdf-packet-page-grid" ref={gridRef} onKeyDown={onGridKeyDown} style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`, gridAutoRows: rowHeight, rowGap: 0 }}>
            {firstRow > 0 && <div aria-hidden style={{ gridColumn: '1 / -1', gridRow: `span ${firstRow}` }} />}
            {windowPages.map((page) => {
              const document = pageToDocument.get(page) ?? { index: 1, start: 1, end: pageCount, pages: pageCount };
              const address = sources ? pageAddress(sources, page) : null;
              const fixed = state.fixedStarts.includes(page);
              const example = state.examplePages.includes(page);
              const localPage = address?.localPage ?? page;
              const confirmed = state.confirmedStarts.includes(page);
              const rejected = state.rejected.includes(page);
              const candidate = matchForPage(effectiveMatches, page);
              const isSuggested = suggested.includes(page);
              const isUnsure = unsure.includes(page);
              const matchLabel = pageMatchLabel(candidate, state.threshold, true);
              const matchDescription = pageMatchLabel(candidate, state.threshold);
              const showReject = (confirmed && !fixed) || (!confirmed && (isSuggested || isUnsure));
              return (
                <div
                  key={page}
                  className="pdf-packet-page-cell"
                  data-packet-page={page}
                  data-document-tone={document.index % 2 ? 'indigo' : 'sand'}
                  data-document-kind={suggested.includes(document.start) ? 'suggested' : 'confirmed'}
                  data-fixed={fixed || undefined}
                  data-example={example || undefined}
                  data-confirmed={(confirmed && !fixed) || undefined}
                  data-suggested={isSuggested || undefined}
                  data-unsure={(isUnsure && !isSuggested) || undefined}
                >
                  <button
                    type="button"
                    className="pdf-packet-page-toggle"
                    aria-label={`${address?.filename ?? ''} Page ${localPage}${fixed ? example ? ', file start, example' : ', file start' : confirmed ? ', document start' : isSuggested ? ', suggested start' : isUnsure ? ', unsure start' : ''}`}
                    aria-pressed={fixed ? example : confirmed}
                    title={fixed ? example ? 'Don’t use as example' : 'Use as example' : `Page ${localPage}${matchDescription ? `. ${matchDescription}.` : '.'} Click to toggle document start.`}
                    onClick={() => dispatch({ type: 'toggleStart', page })}
                  >
                    <span className="pdf-packet-page-row">
                      {!confirmed && <i className="pdf-packet-connector" />}
                      <PacketThumbnail projectId={projectId} splitId={splitId} page={page} />
                    </span>
                    <span className="pdf-packet-page-meta">
                      <span>{gridFilter === 'starts' ? `p ${localPage}` : localPage}</span>
                      <span title={(isSuggested || isUnsure) && matchLabel ? matchDescription : undefined}>
                        {fixed && example ? 'example' : gridFilter === 'starts' && confirmed ? `${document.pages} pp` : rejected ? 'not a start' : isSuggested ? matchLabel : isUnsure ? 'unsure' : ''}
                      </span>
                    </span>
                  </button>
                  <span className="packet-page-hover-actions">
                    <button
                      type="button"
                      className="packet-page-view-action"
                      aria-label={`View page ${page} large`}
                      title="View large"
                      onClick={(event) => stopAction(event, () => setLargeViewPage(page))}
                    >
                      <Expand size={12} />
                    </button>
                    {(!confirmed || (fixed && !example && sources && sources.length > 1)) ? (
                      <button
                        type="button"
                        className="packet-page-start-action"
                        aria-label={`Mark page ${page} as start`}
                        title="Mark as start"
                        onClick={(event) => stopAction(event, () => confirmPage(page))}
                      >
                        <Check size={12} />
                      </button>
                    ) : null}
                    {fixed && example && sources && sources.length > 1 && <button type="button" className="packet-page-reject-action" aria-label={`Don’t use page ${localPage} as example`} title="Don’t use as example" onClick={(event) => stopAction(event, () => dispatch({ type: 'toggleStart', page }))}><X size={12} /></button>}
                    {showReject ? (
                      <button
                        type="button"
                        className="packet-page-reject-action"
                        aria-label={confirmed ? `Remove page ${page} start` : `Mark page ${page} not a start`}
                        title={confirmed ? 'Remove start' : 'Not a start'}
                        onClick={(event) => stopAction(event, () => confirmed
                          ? dispatch({ type: 'toggleStart', page })
                          : rejectPage(page))}
                      >
                        <X size={12} />
                      </button>
                    ) : null}
                  </span>
                </div>
              );
            })}
            {lastRow < rowCount && <div aria-hidden style={{ gridColumn: '1 / -1', gridRow: `span ${rowCount - lastRow}` }} />}
          </div>
          <PacketMinimap
            pageCount={pageCount}
            confirmed={state.confirmedStarts}
            fixed={state.fixedStarts} examples={state.examplePages} sourceCount={sources?.length}
            suggested={suggested}
            unsure={unsure}
            visibleRange={visibleRange}
            onJump={(page) => {
              if (gridFilter === 'starts' && !startPages.includes(page)) {
                setGridFilter('all');
                window.requestAnimationFrame(() => {
                  const grid = gridRef.current;
                  if (grid) { grid.scrollTop = Math.floor((page - 1) / columns) * rowHeight; setViewport({ width: grid.clientWidth, height: grid.clientHeight, scrollTop: grid.scrollTop }); }
                });
              } else focusPage(page);
            }}
          />
        </div>
      </section>
      <aside className="pdf-packet-method-panel">
        {ocrJob && (ocrJob.progress.status === 'queued' || ocrJob.progress.status === 'running') ? (
          <div className="pdf-packet-ocr-progress" role="status">
            <span>Reading text · {ocrJob.progress.done} of {ocrJob.progress.total ?? pageCount}</span>
            <ProgressBar label="Reading text" completed={ocrJob.progress.done} total={ocrJob.progress.total ?? pageCount} state="active" />
            <Button type="button" onClick={() => onCancelOcr(ocrJob.job_id)}>Stop OCR</Button>
          </div>
        ) : null}
        <SegmentedToggle
          ariaLabel="How to find document starts"
          value={state.method}
          onValueChange={(value) => dispatch({ type: 'method', method: value as 'visual' | 'text' | 'manual' })}
          options={[
            { value: 'visual', label: 'Visual' },
            { value: 'text', label: 'Text' },
            { value: 'manual', label: 'Manual' },
          ]}
          className="segmented-toolbar packet-method-tabs"
        />
        {state.method === 'visual' ? (
          <div className="pdf-packet-method-copy">
            <label>
              <span><strong>How similar</strong><strong>{state.threshold}%</strong></span>
              <input
                type="range"
                min="50"
                max="99"
                value={state.threshold}
                onChange={(event) => clearCandidatesAndDispatch({ type: 'threshold', value: Number(event.target.value) })}
              />
            </label>
            {sources && sources.length > 1 && <label className="import-check-inline"><input type="checkbox" checked={state.fixedStarts.every((page) => state.examplePages.includes(page))} onChange={(event) => clearCandidatesAndDispatch({ type: 'toggleFileExamples', enabled: event.target.checked })} /> Also learn from each file’s first page</label>}
            <p>Learning from {countLabel(state.confirmedStarts.length - state.fixedStarts.length, 'start')} you marked{state.examplePages.filter((page) => state.fixedStarts.includes(page)).length ? ` and ${state.examplePages.filter((page) => state.fixedStarts.includes(page)).length} file first pages` : ''} (and {state.rejected.length} you rejected).</p>
            {sources && sources.length > 1 && state.examplePages.length === 0 && state.confirmedStarts.length === state.fixedStarts.length && <p>Mark a start or use a file first page as an example to find visual suggestions.</p>}
          </div>
        ) : state.method === 'text' ? (
          <div className="pdf-packet-method-copy">
            <h3>Phrases on first pages</h3>
            {state.phrases.map((phrase) => (
              <div className="pdf-packet-phrase" key={phrase.id}>
                <input
                  type="checkbox"
                  checked={phrase.enabled}
                  aria-label={`Enable ${phrase.text || 'phrase'}`}
                  onChange={(event) => clearCandidatesAndDispatch({ type: 'phrase', id: phrase.id, patch: { enabled: event.target.checked } })}
                />
                <TextInput
                  className="mono"
                  density="compact"
                  value={phrase.text}
                  placeholder="First-page phrase"
                  onChange={(event) => clearCandidatesAndDispatch({ type: 'phrase', id: phrase.id, patch: { text: event.target.value } })}
                />
                <span>{effectiveMatches?.phrase_counts[phrase.id] ?? 0}</span>
                <button
                  type="button"
                  className="icon-btn"
                  aria-label="Remove phrase"
                  onClick={() => clearCandidatesAndDispatch({ type: 'removePhrase', id: phrase.id })}
                >
                  <X size={13} />
                </button>
              </div>
            ))}
            <Button type="button" onClick={() => clearCandidatesAndDispatch({ type: 'addPhrase' })}>
              <Plus size={13} /> Add a phrase
            </Button>
            <label className="import-check-inline">
              <input
                type="checkbox"
                checked={state.fuzzy}
                onChange={(event) => clearCandidatesAndDispatch({ type: 'fuzzy', value: event.target.checked })}
              /> Allow small OCR errors
            </label>
            <p>You can mark starts while OCR runs. Text matches update when it finishes.</p>
          </div>
        ) : (
          <div className="pdf-packet-method-copy">
            <h3>Mark starts yourself</h3>
            <p>Click any page to mark it as the first page of a document. Click again to remove it.</p>
          </div>
        )}
        <PacketReviewCard
          projectId={projectId}
          splitId={splitId}
          sources={sources}
          emptyMessage={sources && sources.length > 1 && state.method === 'visual' && state.examplePages.length === 0 && state.confirmedStarts.length === state.fixedStarts.length ? 'No suggestions yet.' : undefined}
          tab={reviewTab}
          index={reviewIndex}
          unsure={unsure}
          suggested={suggested}
          matches={effectiveMatches}
          onTab={(tab) => {
            setReviewTab(tab);
            setReviewIndex(0);
          }}
          onIndex={setReviewIndex}
          onAccept={confirmPage}
          onReject={rejectPage}
          onOpenLarge={setLargeViewPage}
        />
        {state.method !== 'manual' && state.matching && !state.error ? <span className="packet-match-pending" role="status">Updating suggestions…</span> : null}
        <span className="sr-only" role="status" aria-live="polite">{reviewFeedback}</span>
      </aside>
      <footer className="pdf-packet-find-footer">
        {onBack && <Button type="button" onClick={onBack}>Back</Button>}
        <span>
          {state.confirmedStarts.length} documents · {suggested.length} suggested · {Math.max(0, unsure.length - suggested.length)} unsure
        </span>
        <Button type="button" disabled={!state.matches || !suggested.length} onClick={acceptAllSuggestions}>
          Accept all {suggested.length} suggested
        </Button>
        <Button type="button" className="btn-primary" disabled={continueDisabled} onClick={onContinue}>
          {typeof primaryLabel === 'function' ? primaryLabel(state.confirmedStarts.length) : primaryLabel ?? 'Continue to import →'}
        </Button>
      </footer>
      {largeViewPage != null && state.snapshot ? (
        <Suspense fallback={null}>
          <PacketLargePageView
            projectId={projectId}
            blobHash={state.snapshot.packet.blob_hash}
            splitId={splitId}
            page={largeViewPage}
            pageCount={pageCount}
            sources={sources} examples={state.examplePages} onExample={(page) => dispatch({ type: 'toggleStart', page })}
            confirmed={state.confirmedStarts}
            rejected={state.rejected}
            unsure={unsure}
            suggested={suggested}
            matches={effectiveMatches}
            onPage={setLargeViewPage}
            onConfirm={confirmPage}
            onReject={rejectPage}
            onRemove={(page) => dispatch({ type: 'toggleStart', page })}
            onClose={() => setLargeViewPage(null)}
          />
        </Suspense>
      ) : null}
    </div>
  );
}
