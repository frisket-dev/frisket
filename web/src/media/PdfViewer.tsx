import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from 'react';
import { pdfjsLib, type PdfDocumentProxy, type PdfPageProxy } from './pdfjsSetup';

export interface DocumentPageImage {
  page: number;
  width: number;
  height: number;
  url: string;
}

export interface PdfViewerProps {
  url: string;
  layout: 'single' | 'two-up' | 'continuous';
  fit: 'width' | 'page';
  zoom: number;
  textLayer: boolean;
  currentPage: number;
  onLoaded(count: number): void;
  onCurrentPageChange(page: number): void;
  renderPageOverlay?(page: number): ReactNode;
  pageImages?: readonly DocumentPageImage[];
  totalPageCount?: number;
  pageId?(page: number): string | undefined;
  className?: string;
}

interface PageSize {
  width: number;
  height: number;
}

// Unvisited native pages use a fixed estimate; measured sizes survive eviction.
const DEFAULT_PAGE_SIZE: PageSize = { width: 612, height: 792 };

function renderedPageSize(
  page: PageSize,
  fit: PdfViewerProps['fit'],
  zoom: number,
  containerWidth: number,
  containerHeight: number,
): PageSize {
  if (containerWidth <= 0) {
    return {
      width: Math.max(1, Math.floor(page.width * zoom)),
      height: Math.max(1, Math.floor(page.height * zoom)),
    };
  }
  const widthScale = Math.max(1, containerWidth - 32) / page.width;
  const heightScale = containerHeight > 0
    ? Math.max(1, containerHeight - 32) / page.height
    : widthScale;
  const fitScale = fit === 'page' ? Math.min(widthScale, heightScale) : widthScale;
  const scale = Math.max(0.1, fitScale * zoom);
  return {
    width: Math.max(1, Math.floor(page.width * scale)),
    height: Math.max(1, Math.floor(page.height * scale)),
  };
}

/** Shared pdf.js reading surface for document and evidence views. */
export function PdfViewer(props: PdfViewerProps) {
  const { url, pageImages, totalPageCount } = props;
  const [source, setSource] = useState({ url, pageImages, totalPageCount, revision: 0 });
  if (source.url !== url || source.pageImages !== pageImages || source.totalPageCount !== totalPageCount) {
    setSource({ url, pageImages, totalPageCount, revision: source.revision + 1 });
  }
  // A new source owns new loading, geometry, and navigation state.
  return <PdfViewerSession key={source.revision} {...props} />;
}

function PdfViewerSession({
  url,
  layout,
  fit,
  zoom,
  textLayer,
  currentPage,
  onLoaded,
  onCurrentPageChange,
  renderPageOverlay,
  pageImages,
  totalPageCount,
  pageId,
  className,
}: PdfViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement> | null>(null);
  pageRefs.current ??= new Map();
  const [doc, setDoc] = useState<PdfDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [viewport, setViewport] = useState<{ width: number; height: number }>({ width: 0, height: 0 });
  const [pageSizes, setPageSizes] = useState<ReadonlyMap<number, PageSize>>(() => new Map());
  const [visibility, setVisibility] = useState<{ layout: PdfViewerProps['layout']; pages: ReadonlySet<number> }>(
    () => ({ layout, pages: new Set() }),
  );
  if (visibility.layout !== layout) {
    setVisibility({ layout, pages: new Set() });
  }
  const [pageLayoutRevision, setPageLayoutRevision] = useState(0);
  const programmaticScroll = useRef(false);
  const programmaticScrollTimer = useRef<number | null>(null);
  const scrollReportedPage = useRef<number | null>(null);
  const requestedPage = useRef<number | null>(layout === 'continuous' ? currentPage : null);
  const readyPages = useRef<Set<number>>(new Set());
  const currentPageValue = useRef(currentPage);
  const layoutValue = useRef(layout);
  const onLoadedValue = useRef(onLoaded);
  useLayoutEffect(() => {
    currentPageValue.current = currentPage;
    layoutValue.current = layout;
    onLoadedValue.current = onLoaded;
  }, [currentPage, layout, onLoaded]);

  useEffect(() => {
    if (pageImages) {
      onLoadedValue.current(totalPageCount ?? pageImages.length);
      return;
    }
    let cancelled = false;
    const task = pdfjsLib.getDocument({ url });
    task.promise
      .then((loaded) => {
        if (cancelled) {
          void loaded.cleanup();
          return;
        }
        setDoc(loaded);
        onLoadedValue.current(loaded.numPages);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Could not load this PDF.');
      });
    return () => {
      cancelled = true;
      void task.destroy();
    };
  }, [url, pageImages, totalPageCount]);

  useLayoutEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const measure = () =>
      setViewport({ width: node.clientWidth, height: node.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [doc, pageImages]);

  const allPageNumbers = useMemo(() => pageImages?.map((page) => page.page)
    ?? Array.from({ length: doc?.numPages ?? 0 }, (_, index) => index + 1), [doc, pageImages]);
  const pageNumbers = useMemo(() => {
    if (allPageNumbers.length === 0) return [];
    if (layout === 'single') return [currentPage];
    if (layout === 'two-up') {
      const start = currentPage % 2 === 0 ? currentPage - 1 : currentPage;
      return allPageNumbers.slice(start - 1, start + 1);
    }
    return allPageNumbers;
  }, [allPageNumbers, layout, currentPage]);
  const pageImagesByNumber = useMemo(
    () => new Map(pageImages?.map((image) => [image.page, image]) ?? []),
    [pageImages],
  );

  useEffect(() => {
    if (layout !== 'continuous') return;
    const root = containerRef.current;
    if (!root || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      setVisibility((previous) => {
        if (previous.layout !== layout) return previous;
        let changed = false;
        const next = new Set(previous.pages);
        for (const entry of entries) {
          const page = Number((entry.target as HTMLElement).dataset.page);
          if (!Number.isInteger(page)) continue;
          if (entry.isIntersecting) {
            if (!next.has(page)) {
              next.add(page);
              changed = true;
            }
          } else if (next.delete(page)) {
            changed = true;
          }
        }
        return changed ? { layout, pages: next } : previous;
      });
    }, { root, rootMargin: `${viewport.height}px 0px` });
    for (const node of pageRefs.current!.values()) observer.observe(node);
    return () => observer.disconnect();
  }, [layout, pageNumbers, viewport.height]);

  useLayoutEffect(() => {
    if (layout !== 'continuous') return;
    const reportedByScroll = scrollReportedPage.current === currentPage;
    scrollReportedPage.current = null;
    if (reportedByScroll) {
      requestedPage.current = null;
      return;
    }
    requestedPage.current = currentPage;
  }, [currentPage, layout, doc, pageImages]);

  // Height changes do not alter width-fit page geometry.
  const fitHeight = fit === 'page' ? viewport.height : 0;
  useLayoutEffect(() => {
    if (layoutValue.current === 'continuous') requestedPage.current = currentPageValue.current;
  }, [fit, zoom, viewport.width, fitHeight]);

  useLayoutEffect(() => {
    if (layout !== 'continuous' || requestedPage.current !== currentPage) return;
    const target = pageRefs.current!.get(currentPage);
    const container = containerRef.current;
    if (!target || !container) return;
    programmaticScroll.current = true;
    container.scrollTo({ top: Math.max(0, target.offsetTop - 16), behavior: 'auto' });
    if (programmaticScrollTimer.current !== null) {
      window.clearTimeout(programmaticScrollTimer.current);
    }
    programmaticScrollTimer.current = window.setTimeout(() => {
      programmaticScroll.current = false;
      programmaticScrollTimer.current = null;
    }, 120);
    if (readyPages.current.has(currentPage)) requestedPage.current = null;
  }, [currentPage, layout, doc, pageImages, fit, zoom, viewport.width, fitHeight, pageSizes, pageLayoutRevision]);

  useEffect(() => () => {
    if (programmaticScrollTimer.current !== null) {
      window.clearTimeout(programmaticScrollTimer.current);
    }
  }, []);

  const onScroll = useCallback(() => {
    if (layout !== 'continuous' || programmaticScroll.current) return;
    const container = containerRef.current;
    if (!container || pageNumbers.length === 0) return;
    const target = container.scrollTop + 16;
    let low = 0;
    let high = pageNumbers.length - 1;
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      const node = pageRefs.current!.get(pageNumbers[middle]);
      if (!node || node.offsetTop > target) high = middle - 1;
      else low = middle + 1;
    }
    const candidates = [high, low]
      .filter((index) => index >= 0 && index < pageNumbers.length)
      .map((index) => pageNumbers[index]);
    let best = currentPage;
    let bestDelta = Number.POSITIVE_INFINITY;
    for (const page of candidates) {
      const node = pageRefs.current!.get(page);
      if (!node) continue;
      const delta = Math.abs(node.offsetTop - target);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = page;
      }
    }
    if (best !== currentPage) {
      scrollReportedPage.current = best;
      onCurrentPageChange(best);
    }
  }, [layout, currentPage, onCurrentPageChange, pageNumbers]);

  const registerPage = useCallback((page: number, node: HTMLDivElement | null) => {
    if (node) pageRefs.current!.set(page, node);
    else pageRefs.current!.delete(page);
  }, []);
  const pageReady = useCallback((page: number) => {
    readyPages.current.add(page);
    if (requestedPage.current === page) setPageLayoutRevision((revision) => revision + 1);
  }, []);
  const recordPageSize = useCallback((page: number, size: PageSize) => {
    setPageSizes((previous) => {
      const existing = previous.get(page);
      if (existing?.width === size.width && existing.height === size.height) return previous;
      const next = new Map(previous);
      next.set(page, size);
      return next;
    });
  }, []);

  return (
    <div
      className={`document-pdf document-pdf-${layout}${className ? ` ${className}` : ''}`}
      data-testid="document-pdf"
      data-page-count={pageImages ? String(totalPageCount ?? pageImages.length) : doc ? String(doc.numPages) : ''}
      data-layout={layout}
      ref={containerRef}
      onScroll={onScroll}
    >
      {error && (
        <div className="document-pdf-error" role="alert" data-testid="document-pdf-error">
          {error}
        </div>
      )}
      {(doc || pageImages) &&
        pageNumbers.map((pageNumber) => {
          const pageImage = pageImagesByNumber.get(pageNumber);
          const intrinsicSize = pageImage ?? pageSizes.get(pageNumber) ?? DEFAULT_PAGE_SIZE;
          return (
            <PdfPage
              key={pageNumber}
              doc={doc}
              pageNumber={pageNumber}
              fit={fit}
              zoom={zoom}
              textLayer={textLayer}
              containerWidth={viewport.width}
              containerHeight={fitHeight}
              active={layout !== 'continuous' || pageNumber === currentPage || visibility.pages.has(pageNumber)}
              intrinsicSize={intrinsicSize}
              registerPage={registerPage}
              renderPageOverlay={renderPageOverlay}
              pageImage={pageImage}
              pageId={pageId?.(pageNumber)}
              onPageReady={pageReady}
              onPageSize={recordPageSize}
            />
          );
        })}
    </div>
  );
}

interface PdfPageProps {
  doc: PdfDocumentProxy | null;
  pageNumber: number;
  fit: 'width' | 'page';
  zoom: number;
  textLayer: boolean;
  containerWidth: number;
  containerHeight: number;
  active: boolean;
  intrinsicSize: PageSize;
  registerPage(page: number, node: HTMLDivElement | null): void;
  renderPageOverlay?(page: number): ReactNode;
  pageImage?: DocumentPageImage;
  pageId?: string;
  onPageReady(page: number): void;
  onPageSize(page: number, size: PageSize): void;
}

function PdfPage({
  doc,
  pageNumber,
  fit,
  zoom,
  textLayer,
  containerWidth,
  containerHeight,
  active,
  intrinsicSize,
  registerPage,
  renderPageOverlay,
  pageImage,
  pageId,
  onPageReady,
  onPageSize,
}: PdfPageProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    registerPage(pageNumber, wrapRef.current);
    return () => registerPage(pageNumber, null);
  }, [pageNumber, registerPage]);

  useEffect(() => {
    if (active && pageImage) onPageReady(pageNumber);
  }, [active, onPageReady, pageImage, pageNumber]);

  useEffect(() => {
    if (!active || containerWidth <= 0 || !doc || pageImage) return;
    let cancelled = false;
    let page: PdfPageProxy | null = null;
    let renderTask: ReturnType<PdfPageProxy['render']> | null = null;
    let canvas: HTMLCanvasElement | null = null;
    let textContainer: HTMLDivElement | null = null;
    let layer: InstanceType<typeof pdfjsLib.TextLayer> | null = null;

    void doc.getPage(pageNumber).then((loadedPage) => {
      if (cancelled) {
        loadedPage.cleanup();
        return;
      }
      page = loadedPage;
      const base = loadedPage.getViewport({ scale: 1 });
      onPageSize(pageNumber, { width: base.width, height: base.height });
      const size = renderedPageSize(base, fit, zoom, containerWidth, containerHeight);
      const scale = size.width / base.width;
      const viewport = loadedPage.getViewport({ scale });
      canvas = canvasRef.current;
      if (!canvas) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      canvas.style.width = `${size.width}px`;
      canvas.style.height = `${size.height}px`;
      renderTask = loadedPage.render({
        canvas,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      });
      void renderTask.promise
        .then(async () => {
          if (cancelled) return;
          onPageReady(pageNumber);
          textContainer = textRef.current;
          if (!textContainer) return;
          textContainer.replaceChildren();
          if (!textLayer) return;
          textContainer.style.setProperty('--scale-factor', String(scale));
          textContainer.style.width = `${size.width}px`;
          textContainer.style.height = `${size.height}px`;
          const textContent = await loadedPage.getTextContent();
          if (cancelled) return;
          layer = new pdfjsLib.TextLayer({
            textContentSource: textContent,
            container: textContainer,
            viewport,
          });
          await layer.render();
        })
        .catch(() => {
          // Cancelled renders (page/scale/window change) throw — safe to ignore.
        });
    }).catch(() => {
      // A document may be destroyed while getPage is still pending.
    });

    return () => {
      cancelled = true;
      layer?.cancel();
      renderTask?.cancel();
      page?.cleanup();
      textContainer?.replaceChildren();
      if (canvas) {
        canvas.width = 0;
        canvas.height = 0;
      }
    };
  }, [active, doc, pageNumber, fit, zoom, textLayer, containerWidth, containerHeight, pageImage, onPageReady, onPageSize]);

  const pageSize = renderedPageSize(intrinsicSize, fit, zoom, containerWidth, containerHeight);
  const geometry: CSSProperties = { width: pageSize.width, height: pageSize.height, flexShrink: 0 };

  return (
    <div
      className="document-pdf-page"
      data-testid="document-pdf-page"
      data-page={pageNumber}
      data-rendered={active ? 'true' : 'false'}
      id={pageId}
      ref={wrapRef}
      style={geometry}
    >
      <div className="document-pdf-page-inner" style={geometry}>
        {active && (pageImage ? (
          <img
            src={pageImage.url}
            alt={`Page ${pageNumber}`}
            draggable={false}
            style={{ ...geometry, display: 'block' }}
          />
        ) : <canvas ref={canvasRef} />)}
        {active && (
          <div
            className={`textLayer document-pdf-textlayer${textLayer ? '' : ' document-pdf-textlayer-off'}`}
            data-testid="document-pdf-textlayer"
            ref={textRef}
          />
        )}
        {active && renderPageOverlay?.(pageNumber)}
      </div>
    </div>
  );
}
