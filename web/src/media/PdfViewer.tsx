import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
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

/** Shared pdf.js reading surface for document and evidence views. */
export function PdfViewer({
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
  const [pageLayoutRevision, setPageLayoutRevision] = useState(0);
  const programmaticScroll = useRef(false);

  useEffect(() => {
    if (pageImages) {
      onLoaded(totalPageCount ?? pageImages.length);
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
  }, [url, onLoaded, pageImages, totalPageCount]);

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

  const pageNumbers = useMemo(() => {
    if (!doc && !pageImages) return [];
    const all = pageImages?.map((page) => page.page)
      ?? Array.from({ length: doc!.numPages }, (_, index) => index + 1);
    if (layout === 'single') return [currentPage];
    if (layout === 'two-up') {
      const start = currentPage % 2 === 0 ? currentPage - 1 : currentPage;
      return all.slice(start - 1, start + 1);
    }
    return all;
  }, [doc, layout, currentPage, pageImages]);

  useEffect(() => {
    if (layout !== 'continuous') return;
    const target = pageRefs.current!.get(currentPage);
    const container = containerRef.current;
    if (!target || !container) return;
    programmaticScroll.current = true;
    container.scrollTo({ top: Math.max(0, target.offsetTop - 16), behavior: 'auto' });
    const timer = window.setTimeout(() => {
      programmaticScroll.current = false;
    }, 120);
    return () => window.clearTimeout(timer);
  }, [currentPage, layout, doc, viewport.width, pageLayoutRevision]);

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
  const pageReady = useCallback(() => {
    setPageLayoutRevision((revision) => revision + 1);
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
            renderPageOverlay={renderPageOverlay}
            pageImage={pageImages?.find((image) => image.page === pageNumber)}
            pageId={pageId?.(pageNumber)}
            onPageReady={pageReady}
          />
        ))}
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
  registerPage(page: number, node: HTMLDivElement | null): void;
  renderPageOverlay?(page: number): ReactNode;
  pageImage?: DocumentPageImage;
  pageId?: string;
  onPageReady(page: number): void;
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
  renderPageOverlay,
  pageImage,
  pageId,
  onPageReady,
}: PdfPageProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    registerPage(pageNumber, wrapRef.current);
    return () => registerPage(pageNumber, null);
  }, [pageNumber, registerPage]);

  useEffect(() => {
    if (pageImage) onPageReady(pageNumber);
  }, [onPageReady, pageImage, pageNumber]);

  useEffect(() => {
    if (containerWidth <= 0 || !doc || pageImage) return;
    let cancelled = false;
    let page: PdfPageProxy | null = null;
    let renderTask: ReturnType<PdfPageProxy['render']> | null = null;

    void doc.getPage(pageNumber).then((loadedPage) => {
      if (cancelled) return;
      page = loadedPage;
      const base = loadedPage.getViewport({ scale: 1 });
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
          onPageReady(pageNumber);
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
  }, [doc, pageNumber, fit, zoom, textLayer, containerWidth, containerHeight, pageImage, onPageReady]);

  const imageScale = pageImage ? Math.max(0.1, (fit === 'page'
    ? Math.min((containerWidth - 32) / pageImage.width, (containerHeight - 32) / pageImage.height)
    : (containerWidth - 32) / pageImage.width) * zoom) : 1;

  return (
    <div
      className="document-pdf-page"
      data-testid="document-pdf-page"
      data-page={pageNumber}
      id={pageId}
      ref={wrapRef}
    >
      <div className="document-pdf-page-inner">
        {pageImage ? (
          <img
            src={pageImage.url}
            alt={`Page ${pageNumber}`}
            draggable={false}
            style={{
              width: Math.floor(pageImage.width * imageScale),
              height: Math.floor(pageImage.height * imageScale),
              display: 'block',
            }}
          />
        ) : <canvas ref={canvasRef} />}
        <div
          className={`textLayer document-pdf-textlayer${textLayer ? '' : ' document-pdf-textlayer-off'}`}
          data-testid="document-pdf-textlayer"
          ref={textRef}
        />
        {renderPageOverlay?.(pageNumber)}
      </div>
    </div>
  );
}
