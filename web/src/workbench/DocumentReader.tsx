import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
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
import { pdfjsLib, type PdfDocumentProxy, type PdfPageProxy } from '../media/pdfjsSetup';
import type { ResolvedMediaValue } from '../media/resolveMediaValue';
import type {
  DocumentViewFit,
  DocumentViewLayout,
  DocumentViewVideoFit,
} from '../workspace/useWorkspaceChromeState';
import type { DocumentMediaKind } from './documentMedia';
import { TextFileViewer } from './TextFileViewer';
import { useOptionsPopover } from './useOptionsPopover';
import { PanelHeader } from '../components/PanelPrimitives';
import { TimedTranscript } from './TimedTranscript';
import {
  hasTimedTranscript,
  type TimedTranscriptDocument,
} from './timedTranscriptModel';

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
  /** Optional starting page (OCR Compare token→page scroll). The reader mounts
   *  showing this page; callers remount it (via `key`) to re-target. Unused by
   *  the Document view, which drives page nav from its own header. */
  initialPage?: number | null;
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
  optionsOpen,
  onToggleOptions,
  optionsPopover,
  optionsMenuRefs,
  selectionCount,
  timedTranscriptDocument = null,
  initialPage,
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
  const [currentPage, setCurrentPage] = useState(
    typeof initialPage === 'number' && initialPage > 0 ? initialPage : 1,
  );
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
      setCurrentPage((prev) => {
        const clamped = Math.min(Math.max(next, 1), Math.max(pageCount, 1));
        return clamped === prev ? prev : clamped;
      });
    },
    [pageCount],
  );

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
            {isPdf && (
              <div className="document-page-controls" role="group" aria-label="Page navigation">
                <button
                  type="button"
                  className="icon-btn"
                  data-testid="document-page-prev"
                  aria-label="Previous page"
                  title="Previous page"
                  disabled={currentPage <= 1}
                  onClick={() => goToPage(currentPage - 1)}
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
                  disabled={currentPage >= pageCount}
                  onClick={() => goToPage(currentPage + 1)}
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
            <div className="document-options-anchor">
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
            </div>
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
          />
        )}
        {media && mediaKind === 'image' && (
          <div className="document-native document-native-image" data-testid="document-image">
            <img src={media.url} alt={title} />
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

interface PdfViewerProps {
  url: string;
  layout: DocumentViewLayout;
  fit: DocumentViewFit;
  zoom: number;
  textLayer: boolean;
  currentPage: number;
  onLoaded(count: number): void;
  onCurrentPageChange(page: number): void;
}

function PdfViewer({
  url,
  layout,
  fit,
  zoom,
  textLayer,
  currentPage,
  onLoaded,
  onCurrentPageChange,
}: PdfViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement> | null>(null);
  pageRefs.current ??= new Map();
  const [doc, setDoc] = useState<PdfDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [viewport, setViewport] = useState<{ width: number; height: number }>({ width: 0, height: 0 });
  const programmaticScroll = useRef(false);

  // Load the current document lazily (spec: the reader loads the CURRENT doc).
  // PdfViewer is keyed on `url`, so a doc switch remounts it — no synchronous
  // state reset needed here.
  useEffect(() => {
    let cancelled = false;
    const task = pdfjsLib.getDocument({ url });
    task.promise
      .then((loaded) => {
        if (cancelled) {
          void loaded.cleanup();
          return;
        }
        setDoc(loaded);
        onLoaded(loaded.numPages);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Could not load this PDF.');
      });
    return () => {
      cancelled = true;
      void task.destroy();
    };
  }, [url, onLoaded]);

  // Track the container size so pages scale to fit width/page.
  useLayoutEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const measure = () =>
      setViewport({ width: node.clientWidth, height: node.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [doc]);

  const pageNumbers = useMemo(() => {
    if (!doc) return [];
    const all = Array.from({ length: doc.numPages }, (_, index) => index + 1);
    if (layout === 'single') return [currentPage];
    if (layout === 'two-up') {
      const start = currentPage % 2 === 0 ? currentPage - 1 : currentPage;
      return all.slice(start - 1, start + 1);
    }
    return all;
  }, [doc, layout, currentPage]);

  // Continuous layout: nav scrolls the target page to the top; a scroll listener
  // reflects the most-visible page back into the indicator (guarded so a
  // programmatic scroll does not fight the click).
  useEffect(() => {
    if (layout !== 'continuous') return;
    const target = pageRefs.current!.get(currentPage);
    const container = containerRef.current;
    if (!target || !container) return;
    // .document-pdf is position:relative, so each page's offsetTop is measured
    // relative to it directly.
    programmaticScroll.current = true;
    container.scrollTo({ top: Math.max(0, target.offsetTop - 16), behavior: 'auto' });
    const timer = window.setTimeout(() => {
      programmaticScroll.current = false;
    }, 120);
    return () => window.clearTimeout(timer);
  }, [currentPage, layout, doc, viewport.width]);

  const onScroll = useCallback(() => {
    if (layout !== 'continuous' || programmaticScroll.current) return;
    const container = containerRef.current;
    if (!container) return;
    let best = currentPage;
    let bestDelta = Number.POSITIVE_INFINITY;
    for (const [page, node] of pageRefs.current!) {
      const delta = Math.abs(node.offsetTop - 16 - container.scrollTop);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = page;
      }
    }
    if (best !== currentPage) onCurrentPageChange(best);
  }, [layout, currentPage, onCurrentPageChange]);

  const registerPage = useCallback((page: number, node: HTMLDivElement | null) => {
    if (node) pageRefs.current!.set(page, node);
    else pageRefs.current!.delete(page);
  }, []);

  return (
    <div
      className={`document-pdf document-pdf-${layout}`}
      data-testid="document-pdf"
      data-page-count={doc ? String(doc.numPages) : ''}
      data-layout={layout}
      ref={containerRef}
      onScroll={onScroll}
    >
      {error && (
        <div className="document-pdf-error" role="alert" data-testid="document-pdf-error">
          {error}
        </div>
      )}
      {doc &&
        pageNumbers.map((pageNumber) => (
          <PdfPage
            key={pageNumber}
            doc={doc}
            pageNumber={pageNumber}
            fit={fit}
            zoom={zoom}
            textLayer={textLayer}
            containerWidth={viewport.width}
            containerHeight={viewport.height}
            registerPage={registerPage}
          />
        ))}
    </div>
  );
}

interface PdfPageProps {
  doc: PdfDocumentProxy;
  pageNumber: number;
  fit: DocumentViewFit;
  zoom: number;
  textLayer: boolean;
  containerWidth: number;
  containerHeight: number;
  registerPage(page: number, node: HTMLDivElement | null): void;
}

function PdfPage({
  doc,
  pageNumber,
  fit,
  zoom,
  textLayer,
  containerWidth,
  containerHeight,
  registerPage,
}: PdfPageProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    registerPage(pageNumber, wrapRef.current);
    return () => registerPage(pageNumber, null);
  }, [pageNumber, registerPage]);

  useEffect(() => {
    if (containerWidth <= 0) return;
    let cancelled = false;
    let page: PdfPageProxy | null = null;
    let renderTask: ReturnType<PdfPageProxy['render']> | null = null;

    void doc.getPage(pageNumber).then((loadedPage) => {
      if (cancelled) return;
      page = loadedPage;
      const base = loadedPage.getViewport({ scale: 1 });
      // Fit width (minus gutter) or fit whole page into the visible area.
      const pad = 32;
      const widthScale = (containerWidth - pad) / base.width;
      const heightScale = containerHeight > 0 ? (containerHeight - pad) / base.height : widthScale;
      const fitScale = fit === 'page' ? Math.min(widthScale, heightScale) : widthScale;
      const scale = Math.max(0.1, fitScale * zoom);
      const viewport = loadedPage.getViewport({ scale });
      const canvas = canvasRef.current;
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      renderTask = loadedPage.render({
        canvas,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      });
      void renderTask.promise
        .then(async () => {
          if (cancelled) return;
          const textContainer = textRef.current;
          if (!textContainer) return;
          textContainer.replaceChildren();
          if (!textLayer) return;
          textContainer.style.setProperty('--scale-factor', String(scale));
          textContainer.style.width = `${Math.floor(viewport.width)}px`;
          textContainer.style.height = `${Math.floor(viewport.height)}px`;
          const textContent = await loadedPage.getTextContent();
          if (cancelled) return;
          const layer = new pdfjsLib.TextLayer({
            textContentSource: textContent,
            container: textContainer,
            viewport,
          });
          await layer.render();
        })
        .catch(() => {
          // Cancelled renders (page/scale change) throw — safe to ignore.
        });
    });

    return () => {
      cancelled = true;
      renderTask?.cancel();
      page?.cleanup();
    };
  }, [doc, pageNumber, fit, zoom, textLayer, containerWidth, containerHeight]);

  return (
    <div className="document-pdf-page" data-testid="document-pdf-page" data-page={pageNumber} ref={wrapRef}>
      <div className="document-pdf-page-inner">
        <canvas ref={canvasRef} />
        <div
          className={`textLayer document-pdf-textlayer${textLayer ? '' : ' document-pdf-textlayer-off'}`}
          data-testid="document-pdf-textlayer"
          ref={textRef}
        />
      </div>
    </div>
  );
}
