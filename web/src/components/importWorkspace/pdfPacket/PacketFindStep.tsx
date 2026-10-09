import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type Dispatch,
  type KeyboardEvent,
  type MouseEvent,
} from 'react';
import { Check, ChevronLeft, ChevronRight, Expand, Minus, Plus, X } from 'lucide-react';
import {
  pdfPacketThumbnailUrl,
  type PdfPacketMatchResult,
  type PdfPacketPageMatch,
} from '../../../api/pdfPacketSplits';
import { projectBlobUrl } from '../../../api/raw/projectResources';
import { countLabel } from '../../../format';
import { PdfViewer } from '../../../media/PdfViewer';
import { SegmentedToggle } from '../../PanelPrimitives';
import {
  confirmedDocuments,
  matchForPage,
  pdfPacketFlowReducer,
  type PdfPacketDocument,
} from './model';
import './PacketFindStep.css';

const GRID_SIZES = [44, 57, 80, 110] as const;
const ignorePdfLoaded = () => undefined;

type PacketDispatch = Dispatch<Parameters<typeof pdfPacketFlowReducer>[1]>;
type ReviewTab = 'unsure' | 'suggested';
type GridFilter = 'all' | 'starts';
type PacketMatches4a = PdfPacketMatchResult & { unsure_pages: number[] };

export interface PacketFindStepProps {
  projectId: string;
  splitId: string;
  state: ReturnType<typeof pdfPacketFlowReducer>;
  pageCount: number;
  dispatch: PacketDispatch;
  onCancelOcr(jobId: string): void;
}

// Kept here as the public behavior seam for the Find component's focused tests.
// eslint-disable-next-line react-refresh/only-export-components
export function packetFindQueues(
  matches: (Pick<PdfPacketMatchResult, 'suggested_pages'> & { unsure_pages?: readonly number[] }) | null,
  confirmed: readonly number[],
  rejected: readonly number[],
): { unsure: number[]; suggested: number[] } {
  const excluded = new Set([...confirmed, ...rejected]);
  const clean = (pages: readonly number[] | undefined) => [...new Set(pages ?? [])]
    .filter((page) => !excluded.has(page))
    .sort((left, right) => left - right);
  return {
    unsure: clean(matches?.unsure_pages),
    suggested: clean(matches?.suggested_pages),
  };
}

function PacketThumbnail({
  projectId,
  splitId,
  page,
  alt = '',
  eager = false,
}: {
  projectId: string;
  splitId: string;
  page: number;
  alt?: string;
  eager?: boolean;
}) {
  return (
    <img
      loading={eager ? 'eager' : 'lazy'}
      src={pdfPacketThumbnailUrl(projectId, splitId, page)}
      alt={alt}
    />
  );
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

function candidateCaption(page: number, match: PdfPacketPageMatch | null): string {
  if (match?.visual_score != null) return `Page ${page} · ${Math.round(match.visual_score)}% similar`;
  if (match?.matched_phrase_ids.length) return `Page ${page} · text match`;
  return `Page ${page}`;
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
}: {
  pageCount: number;
  confirmed: readonly number[];
  suggested: readonly number[];
  unsure: readonly number[];
  visibleRange: [number, number];
  onJump(page: number): void;
}) {
  const mapRef = useRef<HTMLDivElement>(null);
  const rows = Math.max(1, Math.ceil(pageCount / 10));
  const confirmedSet = useMemo(() => new Set(confirmed), [confirmed]);
  const suggestedSet = useMemo(() => new Set(suggested), [suggested]);
  const unsureSet = useMemo(() => new Set(unsure), [unsure]);
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
        {Array.from({ length: pageCount }, (_, index) => index + 1).map((page) => (
          <button
            type="button"
            key={page}
            aria-label={`Jump to page ${page}`}
            data-state={confirmedSet.has(page)
              ? 'confirmed'
              : suggestedSet.has(page)
                ? 'suggested'
                : unsureSet.has(page)
                  ? 'unsure'
                  : 'other'}
            onClick={() => onJump(page)}
          />
        ))}
      </div>
      <span>{visibleRange[0]}–{visibleRange[1]} of {pageCount}</span>
    </aside>
  );
}

function ReviewCard({
  projectId,
  splitId,
  tab,
  index,
  unsure,
  suggested,
  matches,
  onTab,
  onIndex,
  onAccept,
  onReject,
  onAcceptAll,
  onOpenLarge,
}: {
  projectId: string;
  splitId: string;
  tab: ReviewTab;
  index: number;
  unsure: readonly number[];
  suggested: readonly number[];
  matches: PdfPacketMatchResult | null;
  onTab(tab: ReviewTab): void;
  onIndex(index: number): void;
  onAccept(page: number): void;
  onReject(page: number): void;
  onAcceptAll(): void;
  onOpenLarge(page: number): void;
}) {
  const queue = tab === 'unsure' ? unsure : suggested;
  const currentIndex = Math.min(index, Math.max(0, queue.length - 1));
  const page = queue[currentIndex] ?? null;
  const match = page == null ? null : matchForPage(matches, page);
  const move = (delta: number) => {
    if (!queue.length) return;
    onIndex((currentIndex + delta + queue.length) % queue.length);
  };
  const answer = (accept: boolean) => {
    if (page == null) return;
    if (accept) onAccept(page);
    else onReject(page);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const key = event.key.toLowerCase();
    if (key === 'y' || key === 'n' || event.key === '[' || event.key === ']') {
      event.preventDefault();
      event.stopPropagation();
    }
    if (key === 'y') answer(true);
    else if (key === 'n') answer(false);
    else if (event.key === '[') move(-1);
    else if (event.key === ']') move(1);
  };

  return (
    <section
      className="packet-review-card"
      data-review-tab={tab}
      aria-label={`${tab === 'unsure' ? 'Unsure' : 'Suggested'} pages to review`}
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      <div className="packet-review-tabs" role="group" aria-label="Review queue">
        <button type="button" aria-pressed={tab === 'unsure'} onClick={() => onTab('unsure')}>
          Unsure <strong>{unsure.length}</strong>
        </button>
        <button type="button" aria-pressed={tab === 'suggested'} onClick={() => onTab('suggested')}>
          Suggested <strong>{suggested.length}</strong>
        </button>
      </div>
      {page == null ? (
        <p className="packet-review-empty">
          {tab === 'unsure' ? 'No unsure pages left.' : 'No suggestions left.'}
        </p>
      ) : (
        <>
          <header className="packet-review-heading">
            <strong>{tab === 'unsure' ? 'Does a document start here?' : 'Likely a start'}</strong>
            <span>{currentIndex + 1} of {queue.length}</span>
          </header>
          <div className="packet-review-pages">
            {page > 1 ? (
              <figure className="packet-review-before">
                <PacketThumbnail projectId={projectId} splitId={splitId} page={page - 1} alt={`Page ${page - 1}`} />
                <figcaption>p {page - 1} · before</figcaption>
              </figure>
            ) : <span className="packet-review-no-before">First page</span>}
            <button
              type="button"
              className="packet-review-candidate"
              aria-label={`View page ${page} large`}
              onClick={() => onOpenLarge(page)}
            >
              <PacketThumbnail projectId={projectId} splitId={splitId} page={page} alt={`Page ${page}`} eager />
              <span>{candidateCaption(page, match)}</span>
            </button>
          </div>
          <div className="packet-review-actions">
            <button
              type="button"
              className="packet-review-step"
              aria-label={`Previous ${tab} page`}
              onClick={() => move(-1)}
            >
              <ChevronLeft size={14} />
            </button>
            <button type="button" className="btn btn-primary" onClick={() => answer(true)}>
              {tab === 'unsure' ? 'Yes, starts here' : 'Accept'}
            </button>
            <button type="button" className="btn" onClick={() => answer(false)}>
              {tab === 'unsure' ? 'No' : 'Not a start'}
            </button>
            <button
              type="button"
              className="packet-review-step"
              aria-label={`Skip to next ${tab} page`}
              onClick={() => move(1)}
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </>
      )}
      {tab === 'suggested' && suggested.length > 0 ? (
        <button type="button" className="packet-review-accept-all" onClick={onAcceptAll}>
          Accept all {suggested.length} suggestions
        </button>
      ) : null}
    </section>
  );
}

function pageStatus(
  page: number,
  confirmed: readonly number[],
  rejected: readonly number[],
  unsure: readonly number[],
  suggested: readonly number[],
  match: PdfPacketPageMatch | null,
): { kind: 'confirmed' | 'suggested' | 'unsure' | 'rejected' | 'other'; label: string } {
  if (confirmed.includes(page)) return { kind: 'confirmed', label: 'Start of a document' };
  if (rejected.includes(page)) return { kind: 'rejected', label: 'Marked not a start' };
  if (suggested.includes(page)) {
    return {
      kind: 'suggested',
      label: `Suggested start${match?.visual_score == null ? '' : ` · ${Math.round(match.visual_score)}%`}`,
    };
  }
  if (unsure.includes(page)) {
    return {
      kind: 'unsure',
      label: `Unsure${match?.visual_score == null ? '' : ` · ${Math.round(match.visual_score)}%`}`,
    };
  }
  return { kind: 'other', label: 'Continuation page' };
}

function PacketLargePageView({
  projectId,
  blobHash,
  splitId,
  page,
  pageCount,
  confirmed,
  rejected,
  unsure,
  suggested,
  matches,
  onPage,
  onConfirm,
  onReject,
  onRemove,
  onClose,
}: {
  projectId: string;
  blobHash: string;
  splitId: string;
  page: number;
  pageCount: number;
  confirmed: readonly number[];
  rejected: readonly number[];
  unsure: readonly number[];
  suggested: readonly number[];
  matches: PdfPacketMatchResult | null;
  onPage(page: number): void;
  onConfirm(page: number): void;
  onReject(page: number): void;
  onRemove(page: number): void;
  onClose(): void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const match = matchForPage(matches, page);
  const status = pageStatus(page, confirmed, rejected, unsure, suggested, match);
  const isConfirmed = confirmed.includes(page);
  const canReject = suggested.includes(page) || unsure.includes(page);
  const go = useCallback((delta: number) => {
    onPage(Math.max(1, Math.min(pageCount, page + delta)));
  }, [onPage, page, pageCount]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    dialog.focus();
    return () => {
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
    };
  }, []);

  const secondaryAction = () => {
    if (isConfirmed && page !== 1) onRemove(page);
    else if (canReject) onReject(page);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    const key = event.key.toLowerCase();
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' || key === 's' || key === 'x' || event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
    }
    if (event.key === 'ArrowLeft') go(-1);
    else if (event.key === 'ArrowRight') go(1);
    else if (key === 's' && !isConfirmed) onConfirm(page);
    else if (key === 'x') secondaryAction();
    else if (event.key === 'Escape') onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="packet-large-view"
      data-testid="pdf-packet-large-view"
      aria-label={`Page ${page}`}
      aria-modal="true"
      onKeyDown={onKeyDown}
      onCancel={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="packet-large-card">
        <header>
          <h2>Page {page}</h2>
          <span className="packet-page-status" data-status={status.kind}>{status.label}</span>
          <button type="button" className="icon-btn" aria-label="Close large page view" onClick={onClose}>
            <X size={17} />
          </button>
        </header>
        <div className="packet-large-pages" data-status={status.kind}>
          <button type="button" aria-label="Previous page" disabled={page === 1} onClick={() => go(-1)}>
            <ChevronLeft size={18} />
          </button>
          {page > 1 ? (
            <figure className="packet-large-before">
              <PacketThumbnail projectId={projectId} splitId={splitId} page={page - 1} alt={`Page ${page - 1}`} eager />
              <figcaption>p {page - 1} · before</figcaption>
            </figure>
          ) : <span className="packet-large-first">First page</span>}
          <div className="packet-large-current">
            <PdfViewer
              url={projectBlobUrl(projectId, blobHash)}
              layout="single"
              fit="page"
              zoom={1}
              textLayer={false}
              currentPage={page}
              onLoaded={ignorePdfLoaded}
              onCurrentPageChange={onPage}
              className="packet-large-pdf"
            />
            <span>Page {page}</span>
          </div>
          <button type="button" aria-label="Next page" disabled={page === pageCount} onClick={() => go(1)}>
            <ChevronRight size={18} />
          </button>
        </div>
        <footer>
          <div className="packet-large-actions">
            {!isConfirmed ? (
              <button type="button" className="btn btn-primary" onClick={() => onConfirm(page)}>
                <Check size={13} /> Starts a document <kbd>S</kbd>
              </button>
            ) : null}
            {isConfirmed && page !== 1 ? (
              <button type="button" className="btn" onClick={() => onRemove(page)}>
                <X size={13} /> Remove start <kbd>X</kbd>
              </button>
            ) : canReject ? (
              <button type="button" className="btn" onClick={() => onReject(page)}>
                <X size={13} /> Not a start <kbd>X</kbd>
              </button>
            ) : null}
          </div>
          <span>← → pages · Esc close</span>
        </footer>
      </section>
    </dialog>
  );
}

export function PacketFindStep({
  projectId,
  splitId,
  state,
  pageCount,
  dispatch,
  onCancelOcr,
}: PacketFindStepProps) {
  const gridRef = useRef<HTMLDivElement>(null);
  const liveMatches = state.matches as PacketMatches4a | null;
  const [cachedMatches, setCachedMatches] = useState<PacketMatches4a | null>(liveMatches);
  const [seenLiveMatches, setSeenLiveMatches] = useState<PacketMatches4a | null>(liveMatches);
  const [visibleRange, setVisibleRange] = useState<[number, number]>([1, Math.min(40, pageCount)]);
  const [gridSizeIndex, setGridSizeIndex] = useState(1);
  const [gridFilter, setGridFilter] = useState<GridFilter>('all');
  const [hoveredPage, setHoveredPage] = useState<number | null>(null);
  const [reviewTab, setReviewTab] = useState<ReviewTab>('unsure');
  const [reviewIndex, setReviewIndex] = useState(0);
  const [largeViewPage, setLargeViewPage] = useState<number | null>(null);
  const [reviewFeedback, setReviewFeedback] = useState('');

  if (liveMatches && liveMatches !== seenLiveMatches) {
    setSeenLiveMatches(liveMatches);
    setCachedMatches(liveMatches);
  }

  const effectiveMatches = liveMatches ?? cachedMatches;
  const { unsure, suggested } = packetFindQueues(
    effectiveMatches,
    state.confirmedStarts,
    state.rejected,
  );
  const ocrJob = state.snapshot?.jobs.filter((job) => job.kind === 'ocr_full').at(-1);
  const textRead = state.snapshot?.text_source === 'ocr'
    ? state.snapshot.ocr_pages.length
    : (state.snapshot?.text_source === 'native' ? state.snapshot.prepare.native_text_pages.length : 0);
  const documents = confirmedDocuments([...state.confirmedStarts, ...suggested], pageCount);
  const pageToDocument = new Map<number, PdfPacketDocument>();
  for (const document of documents) {
    for (let page = document.start; page <= document.end; page += 1) pageToDocument.set(page, document);
  }
  const startPages = [...new Set([...state.confirmedStarts, ...suggested, ...unsure])].sort((a, b) => a - b);
  const displayedPages = gridFilter === 'all'
    ? Array.from({ length: pageCount }, (_, index) => index + 1)
    : startPages;

  const focusPage = useCallback((page: number) => {
    const cell = gridRef.current?.querySelector<HTMLElement>(`[data-packet-page="${page}"]`);
    cell?.scrollIntoView?.({ block: 'center' });
    cell?.querySelector<HTMLElement>('.pdf-packet-page-toggle')?.focus();
  }, []);

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
    const observer = new ResizeObserver(updateVisibleRange);
    observer.observe(grid);
    grid.addEventListener('scroll', updateVisibleRange, { passive: true });
    return () => {
      observer.disconnect();
      grid.removeEventListener('scroll', updateVisibleRange);
    };
  }, [displayedPages.length, gridFilter, gridSizeIndex, pageCount]);

  const onGridKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const cell = (event.target as HTMLElement).closest<HTMLElement>('[data-packet-page]');
    const page = Number(cell?.dataset.packetPage);
    if (!page) return;
    const position = displayedPages.indexOf(page);
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      const nextPosition = Math.max(0, Math.min(displayedPages.length - 1, position + (event.key === 'ArrowRight' ? 1 : -1)));
      focusPage(displayedPages[nextPosition]);
    } else if (event.key.toLowerCase() === 's' && (event.target as HTMLElement).classList.contains('pdf-packet-page-toggle')) {
      event.preventDefault();
      dispatch({ type: 'toggleStart', page });
    }
  };

  const clearCandidatesAndDispatch = (action: Parameters<typeof dispatch>[0]) => {
    setCachedMatches(null);
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
          <span><i className="confirmed" /> Marked</span>
          <span><i className="suggested" /> Suggested</span>
          <span><i className="unsure" /> Unsure</span>
          <SegmentedToggle
            ariaLabel="Pages shown"
            value={gridFilter}
            onValueChange={(value) => setGridFilter(value as GridFilter)}
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
          <div className="pdf-packet-page-grid" ref={gridRef} onKeyDown={onGridKeyDown}>
            {displayedPages.map((page) => {
              const document = pageToDocument.get(page) ?? { index: 1, start: 1, end: pageCount, pages: pageCount };
              const confirmed = state.confirmedStarts.includes(page);
              const rejected = state.rejected.includes(page);
              const candidate = matchForPage(effectiveMatches, page);
              const isSuggested = suggested.includes(page);
              const isUnsure = unsure.includes(page);
              const matchLabel = pageMatchLabel(candidate, state.threshold, true);
              const matchDescription = pageMatchLabel(candidate, state.threshold);
              const showReject = (confirmed && page !== 1) || (!confirmed && (isSuggested || isUnsure));
              return (
                <div
                  key={page}
                  className="pdf-packet-page-cell"
                  data-packet-page={page}
                  data-document-tone={document.index % 2 ? 'indigo' : 'sand'}
                  data-document-kind={suggested.includes(document.start) ? 'suggested' : 'confirmed'}
                  data-confirmed={confirmed || undefined}
                  data-suggested={isSuggested || undefined}
                  data-unsure={isUnsure || undefined}
                  data-hovered={hoveredPage === page || undefined}
                  onMouseEnter={() => setHoveredPage(page)}
                  onMouseLeave={() => setHoveredPage((hovered) => hovered === page ? null : hovered)}
                >
                  <button
                    type="button"
                    className="pdf-packet-page-toggle"
                    aria-label={`Page ${page}${confirmed ? ', document start' : isSuggested ? ', suggested start' : isUnsure ? ', unsure start' : ''}`}
                    aria-pressed={confirmed}
                    title={`Page ${page}${matchDescription ? `. ${matchDescription}.` : '.'} Click to toggle document start.`}
                    onClick={() => dispatch({ type: 'toggleStart', page })}
                  >
                    <span className="pdf-packet-doc-label">
                      {confirmed ? `Doc ${document.index}` : isSuggested ? `Doc ${document.index}?` : '\u00a0'}
                    </span>
                    <span className="pdf-packet-page-row">
                      {!confirmed && <i className="pdf-packet-connector" />}
                      <PacketThumbnail projectId={projectId} splitId={splitId} page={page} />
                    </span>
                    <span className="pdf-packet-page-meta">
                      <span>{page}</span>
                      <span title={(isSuggested || isUnsure) && matchLabel ? matchDescription : undefined}>
                        {rejected ? 'not a start' : isUnsure ? 'unsure' : isSuggested ? matchLabel : ''}
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
                    {!confirmed ? (
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
          </div>
          <PacketMinimap
            pageCount={pageCount}
            confirmed={state.confirmedStarts}
            suggested={suggested}
            unsure={unsure}
            visibleRange={visibleRange}
            onJump={(page) => {
              if (gridFilter === 'starts' && !startPages.includes(page)) setGridFilter('all');
              window.requestAnimationFrame(() => focusPage(page));
            }}
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
            <p>Learning from {countLabel(state.confirmedStarts.length, 'start')} you marked (and {state.rejected.length} you rejected).</p>
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
                <input
                  className="form-input mono"
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
            <button type="button" className="btn" onClick={() => clearCandidatesAndDispatch({ type: 'addPhrase' })}>
              <Plus size={13} /> Add a phrase
            </button>
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
        <ReviewCard
          projectId={projectId}
          splitId={splitId}
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
          onAcceptAll={acceptAllSuggestions}
          onOpenLarge={setLargeViewPage}
        />
        <span className="sr-only" role="status" aria-live="polite">{reviewFeedback}</span>
      </aside>
      <footer className="pdf-packet-find-footer">
        <span>{state.confirmedStarts.length} marked · {suggested.length} suggested · {unsure.length} unsure</span>
        <button type="button" className="btn" disabled={!suggested.length} onClick={acceptAllSuggestions}>
          Accept all {suggested.length} suggested
        </button>
        <button type="button" className="btn btn-primary" onClick={() => dispatch({ type: 'stage', stage: 'import' })}>
          Continue to import →
        </button>
      </footer>
      {largeViewPage != null && state.snapshot ? (
        <PacketLargePageView
          projectId={projectId}
          blobHash={state.snapshot.packet.blob_hash}
          splitId={splitId}
          page={largeViewPage}
          pageCount={pageCount}
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
      ) : null}
    </div>
  );
}
