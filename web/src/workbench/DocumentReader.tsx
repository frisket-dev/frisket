import {
  lazy,
  Suspense,
  useCallback,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from 'react';
import {
  ChevronLeft,
  ChevronRight,
  FileSearch,
  FileText,
  Maximize2,
  Minimize2,
  Settings2,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import type { ResolvedMediaValue } from '../media/resolveMediaValue';
import type { DocumentPageImage } from '../media/PdfViewer';
import type {
  DocumentViewFit,
  DocumentViewLayout,
  DocumentViewVideoFit,
} from '../workspace/useWorkspaceChromeState';
import type { DocumentMediaKind } from './documentMedia';
import { TextFileViewer } from './TextFileViewer';
import { useOptionsPopover } from './useOptionsPopover';
import { PanelHeader, PanelLoading } from '../components/PanelPrimitives';
import { TimedTranscript } from './TimedTranscript';
import {
  hasTimedTranscript,
  type TimedTranscriptDocument,
} from './timedTranscriptModel';

const PdfViewer = lazy(() => import('../media/PdfViewer').then(({ PdfViewer }) => ({ default: PdfViewer })));

const MEDIA_CHIP_LABEL: Record<DocumentMediaKind, string> = {
  pdf: 'PDF',
  image: 'Image',
  video: 'Video',
  audio: 'Audio',
  text: 'Text',
  other: 'File',
};

type OptionsPopoverRenderer = (props: {
  popoverRef: (el: HTMLDivElement | null) => void;
  style: CSSProperties;
}) => ReactNode;

export type { DocumentPageImage } from '../media/PdfViewer';

/** Invokes the `optionsPopover` render-prop from a component that never
 *  itself calls `useRef` — react-hooks/refs' render-time-ref-access check
 *  tracks identifiers a component sources from its OWN `useRef`/`useCallback`
 *  closures; `popoverRef` here is just an opaque prop from this component's
 *  point of view, so calling `render(...)` with it during render doesn't trip
 *  the rule the way calling it directly inside DocumentReader (which DOES own
 *  the ref) would. */
function OptionsPopoverSlot({
  popoverRef,
  style,
  render,
}: {
  popoverRef: (el: HTMLDivElement | null) => void;
  style: CSSProperties;
  render: OptionsPopoverRenderer;
}) {
  return <>{render({ popoverRef, style })}</>;
}

interface DocumentReaderProps {
  media: ResolvedMediaValue | null;
  mediaKind: DocumentMediaKind | null;
  title: string;
  layout: DocumentViewLayout;
  fit: DocumentViewFit;
  /** The video full-size/fit-to-height toggle. Controlled (lifted to the
   *  caller's persisted chrome state) so it survives this component's own
   *  remount on document switch; the OCR compare peek host (never video)
   *  passes 'full' + a no-op setter. */
  videoFit: DocumentViewVideoFit;
  onVideoFitChange(next: DocumentViewVideoFit): void;
  textLayer: boolean;
  /** Report the loaded PDF's real page count up so the list secondary line can
   *  show it — or null when unknown (never fabricated). */
  onPageCount(rowKey: string, count: number | null): void;
  rowKey: string;
  onOpenDetail(): void;
  canOpenDetail: boolean;
  onShowAlongside?(): void;
  alongsideTriggerRef?: RefObject<HTMLButtonElement>;
  optionsOpen: boolean;
  onToggleOptions(): void;
  /** Render-prop (not a pre-built element): the caller (DocumentView.tsx)
   *  builds the `MenuPop` options popover, but the popover ref + top-layer
   *  placement style are owned here, alongside the trigger — this hands them
   *  down so the caller attaches them via ordinary JSX `ref=`/`style=`.
   *  Invoked via `OptionsPopoverSlot` (below), not called directly — see its
   *  doc comment for why. `null` when the host has no options popover at all
   *  (the OCR compare peek). */
  optionsPopover: OptionsPopoverRenderer | null;
  /** Menus that options inside `optionsPopover` portal to <body>. They are not
   *  DOM descendants of the popover, so the dismissal boundary must be told
   *  they count as inside; without this, choosing an option dismisses the
   *  popover it belongs to. */
  optionsMenuRefs?: ReadonlyArray<RefObject<HTMLElement | null>>;
  selectionCount: number;
  /** The active audio/video document. TimedTranscript resolves the transcript
   *  columns from this object instead of making callers unpack each part. */
  timedTranscriptDocument?: TimedTranscriptDocument | null;
  /** Optional starting page. The reader mounts showing this page; callers
   *  remount it (via `key`) to re-target. */
  initialPage?: number | null;
  onPageChange?(page: number): void;
  /** Positioned inside the rendered page, in its CSS-pixel coordinate space. */
  renderPageOverlay?(page: number): ReactNode;
  /** Use server-rendered geometry when extraction coordinates use MediaBox,
   * rather than PDF.js's default CropBox viewport. */
  pageImages?: readonly DocumentPageImage[];
  /** Original document total when pageImages intentionally contains a subset. */
  totalPageCount?: number;
}

/** The center reader. A thin header (title + media-type chip + PDF page
 *  prev/next + zoom + options + open-detail) over the page body. PDFs use
 *  the self-hosted pdf.js reader; images/video/audio render centered with
 *  native controls. */
export function DocumentReader({
  media,
  mediaKind,
  title,
  layout,
  fit,
  videoFit,
  onVideoFitChange,
  textLayer,
  onPageCount,
  rowKey,
  onOpenDetail,
  canOpenDetail,
  onShowAlongside,
  alongsideTriggerRef,
  optionsOpen,
  onToggleOptions,
  optionsPopover,
  optionsMenuRefs,
  selectionCount,
  timedTranscriptDocument = null,
  initialPage,
  onPageChange,
  renderPageOverlay,
  pageImages,
  totalPageCount,
}: DocumentReaderProps) {
  // The view-options popover (Document view only; the OCR compare host passes
  // optionsOpen=false so the hook is inert there, and optionsPopover=null).
  // Top-layer popover: plain ref+toggle default dismissal. `optionsPopover`
  // is a render-prop rather than a pre-built element so the caller
  // (DocumentView.tsx) attaches the popover ref/placement style via ordinary
  // JSX `ref=`/`style=` — keeping the whole dismissal contract local to this
  // trigger without an off-JSX ref write.
  const {
    triggerRef: optionsTriggerRef,
    attachPopover: attachOptionsPopover,
    popoverStyle: optionsPopoverStyle,
  } = useOptionsPopover(
    optionsOpen,
    onToggleOptions,
    '[data-testid="document-options-button"]',
    optionsMenuRefs,
  );

  // page/zoom state is per-document: the reader is remounted (keyed on rowKey by
  // the parent) when the active document changes, so this state starts fresh.
  const [pageCount, setPageCount] = useState(0);
  const [currentPage, setCurrentPageValue] = useState(
    typeof initialPage === 'number' && initialPage > 0 ? initialPage : 1,
  );
  const setCurrentPage = useCallback((page: number) => { setCurrentPageValue(page); onPageChange?.(page); }, [onPageChange]);
  const [zoom, setZoom] = useState(1);
  // `videoFit`/`onVideoFitChange` are lifted to the caller as chrome state —
  // sticky across this component's per-document remount and across reload —
  // instead of resetting like the genuinely per-document page/zoom state
  // above.

  const handleLoaded = useCallback(
    (count: number) => {
      setPageCount(count);
      onPageCount(rowKey, count);
    },
    [onPageCount, rowKey],
  );

  const goToPage = useCallback(
    (next: number) => {
      setCurrentPage(Math.min(Math.max(next, 1), Math.max(pageCount, 1)));
    },
    [pageCount, setCurrentPage],
  );
  const currentPageImageIndex = pageImages?.findIndex(
    (image) => image.page === currentPage,
  );
  const previousPage = pageImages
    ? currentPageImageIndex !== undefined && currentPageImageIndex > 0
      ? pageImages[currentPageImageIndex - 1].page
      : null
    : currentPage > 1
      ? currentPage - 1
      : null;
  const nextPage = pageImages
    ? currentPageImageIndex !== undefined &&
      currentPageImageIndex >= 0 &&
      currentPageImageIndex + 1 < pageImages.length
      ? pageImages[currentPageImageIndex + 1].page
      : null
    : currentPage < pageCount
      ? currentPage + 1
      : null;

  const isPdf = mediaKind === 'pdf' && media !== null;
  const isVideo = mediaKind === 'video' && media !== null;
  const showTimedTranscript = timedTranscriptDocument
    ? hasTimedTranscript(timedTranscriptDocument)
    : false;

  // jsx-no-jsx-as-prop: memoized so PanelHeader's chips prop doesn't get a
  // fresh JSX element every render.
  const headerChips = useMemo(
    () =>
      mediaKind && (
        <span className="document-reader-chip pill-btn" data-testid="document-reader-chip">
          {MEDIA_CHIP_LABEL[mediaKind]}
        </span>
      ),
    [mediaKind],
  );

  return (
    <section className="document-reader" data-testid="document-reader" data-media-kind={mediaKind ?? 'none'}>
      <PanelHeader
        className="panel-frame-header document-reader-header"
        testId="document-reader-header"
        title={
          <span className="document-reader-title" data-testid="document-reader-title" title={title}>
            {title}
          </span>
        }
        chips={headerChips}
        actions={
          <>
            <span
              className="document-reader-selection mono muted"
              data-testid="document-selection"
              data-selection-count={selectionCount}
            >
              {selectionCount > 0 ? `${selectionCount} selected` : 'browsing'}
            </span>
            <span className="document-reader-spacer" />
            {onShowAlongside && <button ref={alongsideTriggerRef} type="button" className="mini-btn" data-testid="document-show-alongside"
              onClick={onShowAlongside}>Show alongside</button>}
            {isPdf && (
              <div className="document-page-controls" role="group" aria-label="Page navigation">
                <button
                  type="button"
                  className="icon-btn"
                  data-testid="document-page-prev"
                  aria-label="Previous page"
                  title="Previous page"
                  disabled={previousPage === null}
                  onClick={() => previousPage !== null && goToPage(previousPage)}
                >
                  <ChevronLeft size={15} />
                </button>
                <span className="document-page-indicator mono" data-testid="document-page-indicator">
                  {currentPage} / {Math.max(pageCount, 1)}
                </span>
                <button
                  type="button"
                  className="icon-btn"
                  data-testid="document-page-next"
                  aria-label="Next page"
                  title="Next page"
                  disabled={nextPage === null}
                  onClick={() => nextPage !== null && goToPage(nextPage)}
                >
                  <ChevronRight size={15} />
                </button>
                <button
                  type="button"
                  className="icon-btn"
                  data-testid="document-zoom-out"
                  aria-label="Zoom out"
                  title="Zoom out"
                  onClick={() => setZoom((z) => Math.max(0.5, Math.round((z - 0.2) * 10) / 10))}
                >
                  <ZoomOut size={15} />
                </button>
                <button
                  type="button"
                  className="icon-btn"
                  data-testid="document-zoom-in"
                  aria-label="Zoom in"
                  title="Zoom in"
                  onClick={() => setZoom((z) => Math.min(3, Math.round((z + 0.2) * 10) / 10))}
                >
                  <ZoomIn size={15} />
                </button>
              </div>
            )}
            {isVideo && (
              <button
                type="button"
                className="icon-btn"
                data-testid="document-video-fit-toggle"
                aria-label={videoFit === 'full' ? 'Fit to height' : 'Full size'}
                aria-pressed={videoFit === 'fit-height'}
                title={videoFit === 'full' ? 'Fit to height' : 'Full size'}
                onClick={() => onVideoFitChange(videoFit === 'full' ? 'fit-height' : 'full')}
              >
                {videoFit === 'full' ? <Maximize2 size={15} /> : <Minimize2 size={15} />}
              </button>
            )}
            <button
              type="button"
              className="icon-btn"
              data-testid="document-open-detail"
              aria-label="Open row detail"
              title="Open row detail"
              disabled={!canOpenDetail}
              onClick={onOpenDetail}
            >
              <FileSearch size={15} />
            </button>
            {optionsPopover !== null && <div className="document-options-anchor">
              <button
                type="button"
                ref={optionsTriggerRef}
                className={`icon-btn${optionsOpen ? ' active' : ''}`}
                data-testid="document-options-button"
                aria-label="View options"
                aria-expanded={optionsOpen}
                title="View options"
                onClick={onToggleOptions}
              >
                <Settings2 size={15} />
              </button>
              {optionsOpen && optionsPopover && (
                <OptionsPopoverSlot
                  popoverRef={attachOptionsPopover}
                  style={optionsPopoverStyle}
                  render={optionsPopover}
                />
              )}
            </div>}
          </>
        }
      />
      <div className="document-reader-body" data-testid="document-reader-body">
        {media === null && (
          <div className="document-empty" data-testid="document-empty">
            <FileText size={28} aria-hidden />
            <p className="document-empty-title">No document</p>
            <p className="muted">This row has no media in the source column.</p>
          </div>
        )}
        {isPdf && media && (
          <Suspense fallback={<PanelLoading label="Loading PDF…" />}>
            <PdfViewer
              key={media.url}
              url={media.url}
              layout={layout}
              fit={fit}
              zoom={zoom}
              textLayer={textLayer}
              currentPage={currentPage}
              onLoaded={handleLoaded}
              onCurrentPageChange={goToPage}
              renderPageOverlay={renderPageOverlay}
              pageImages={pageImages}
              totalPageCount={totalPageCount}
            />
          </Suspense>
        )}
        {media && mediaKind === 'image' && (
          <div className="document-native document-native-image" data-testid="document-image"
            style={{ alignItems: 'safe center', justifyContent: 'safe center' }}>
            <div style={{ position: 'relative', display: 'inline-block', maxWidth: '100%', flexShrink: 0 }}>
              <img src={media.url} alt={title} style={{ display: 'block' }} />
              {renderPageOverlay?.(1)}
            </div>
          </div>
        )}
        {media && mediaKind === 'video' && timedTranscriptDocument && showTimedTranscript && (
          <TimedTranscript document={timedTranscriptDocument} />
        )}
        {media && mediaKind === 'video' && !showTimedTranscript && (
          <div
            className="document-native"
            data-testid="document-video"
            data-video-fit={videoFit}
          >
            <video
              className={`document-video-${videoFit}`}
              src={media.url}
              aria-label={title}
              controls
            >
              <track kind="captions" />
            </video>
          </div>
        )}
        {media && mediaKind === 'audio' && timedTranscriptDocument && showTimedTranscript && (
          <TimedTranscript document={timedTranscriptDocument} />
        )}
        {media && mediaKind === 'audio' && !showTimedTranscript && (
          <div className="document-native" data-testid="document-audio">
            <audio src={media.url} aria-label={title} controls>
              <track kind="captions" />
            </audio>
          </div>
        )}
        {media && mediaKind === 'text' && (
          <TextFileViewer key={media.url} url={media.url} label={media.filename ?? media.label} />
        )}
        {media && mediaKind === 'other' && (
          <div className="document-native document-native-file" data-testid="document-file">
            <FileText size={28} aria-hidden />
            <a href={media.url} target="_blank" rel="noopener noreferrer">
              {media.filename ?? media.label}
            </a>
          </div>
        )}
      </div>
    </section>
  );
}
