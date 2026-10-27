import './EvidenceReading.css';
import { FileText } from 'lucide-react';
import { lazy, Suspense, useEffect, useRef, useState, type CSSProperties, type RefObject } from 'react';
import type { EvidenceArtifact, EvidencePage } from '../../api/open';
import { PanelLoading } from '../PanelPrimitives';
import { RegionOverlay } from './EvidenceRegionOverlay';
import { pageAnchorId, selectorNumber } from './evidenceSourceModel';
import type { PdfPacketSourceCardData } from './pdfPacketSourceCardModel';

const PdfViewer = lazy(async () => {
  const module = await import('../../media/PdfViewer');
  return { default: module.PdfViewer };
});

const ignorePdfPageCount = () => undefined;
const ignorePdfPageChange = () => undefined;

// Document sources share page anchors across native PDFs, rendered images, and detail links.
export function EvidenceDocumentSource({ artifact, initialPage, scopeSpanId, emphasizedSpanIds }: {
  artifact: EvidenceArtifact;
  initialPage?: number;
  scopeSpanId?: string;
  emphasizedSpanIds: readonly string[];
}) {
  const blob = artifact.artifact_ref.blob;
  const selectedPdfPage = initialPage !== undefined && initialPage >= 1
    && (artifact.page_count === null || initialPage <= artifact.page_count)
    ? initialPage : evidencePdfPage(artifact, scopeSpanId, emphasizedSpanIds);
  if (artifact.media_type === 'application/pdf' && blob) {
    return (
      <div
        id={pageAnchorId(artifact.stable_id, selectedPdfPage)}
        data-testid="evidence-pdf"
        role="document"
        aria-label={`Evidence PDF: ${artifact.title || artifact.filename || artifact.media_type}`}
      >
        <Suspense fallback={<PanelLoading label="Loading PDF…" />}>
          <PdfViewer
            key={blob.url}
            url={blob.url}
            layout="continuous"
            fit="width"
            zoom={1}
            textLayer
            currentPage={selectedPdfPage}
            onLoaded={ignorePdfPageCount}
            onCurrentPageChange={ignorePdfPageChange}
            className="evidence-pdf"
            pageId={(page) => page === selectedPdfPage
              ? undefined
              : pageAnchorId(artifact.stable_id, page)}
            renderPageOverlay={(page) => artifact.pages
              .find((item) => item.page === page)
              ?.regions.map((region) => (
                <RegionOverlay
                  key={region.stable_id}
                  region={region}
                  emphasized={emphasizedSpanIds.includes(region.stable_id)}
                />
              ))}
          />
        </Suspense>
      </div>
    );
  }
  return artifact.pages.map((page) => (
    <EvidencePageView key={page.page} page={page} artifactStableId={artifact.stable_id} emphasizedSpanIds={emphasizedSpanIds} />
  ));
}

function evidencePdfPage(
  artifact: EvidenceArtifact,
  scopeSpanId: string | undefined,
  emphasizedSpanIds: readonly string[],
): number {
  const focusedSpan = artifact.spans.find((span) => span.stable_id === scopeSpanId)
    ?? artifact.spans.find((span) => emphasizedSpanIds.includes(span.stable_id));
  const spanPage = focusedSpan
    ? selectorNumber(focusedSpan, 'page_start') ?? selectorNumber(focusedSpan, 'page')
    : null;
  if (spanPage !== null && spanPage >= 1) return Math.floor(spanPage);
  const emphasizedPage = artifact.pages.find((page) =>
    page.regions.some((region) => emphasizedSpanIds.includes(region.stable_id)),
  )?.page;
  if (typeof emphasizedPage === 'number' && emphasizedPage >= 1) return emphasizedPage;
  const firstPage = artifact.pages[0]?.page;
  return typeof firstPage === 'number' && firstPage >= 1 ? firstPage : 1;
}

function EvidencePageView({
  page,
  artifactStableId,
  emphasizedSpanIds,
}: {
  page: EvidencePage;
  artifactStableId: string;
  emphasizedSpanIds: readonly string[];
}) {
  const imageUrl = page.image?.url ?? page.render_url ?? null;
  if (!imageUrl) {
    return (
      <div
        className="evidence-page-fallback"
        data-testid="evidence-page-warning"
        id={pageAnchorId(artifactStableId, page.page)}
      >
        Page {page.page} has no rendered image. Source text is shown instead.
        {page.text && <pre>{page.text}</pre>}
      </div>
    );
  }
  return (
    <div className="evidence-page" data-testid="evidence-page" id={pageAnchorId(artifactStableId, page.page)}>
      <div className="evidence-page-label">Page {page.page}</div>
      <LazyEvidencePageImage
        key={imageUrl}
        imageUrl={imageUrl}
        page={page}
        emphasizedSpanIds={emphasizedSpanIds}
      />
      {page.text && <pre className="evidence-page-text" data-testid="evidence-page-text">{page.text}</pre>}
    </div>
  );
}

function LazyEvidencePageImage({
  imageUrl,
  page,
  emphasizedSpanIds,
}: {
  imageUrl: string;
  page: EvidencePage;
  emphasizedSpanIds: readonly string[];
}) {
  const { frameRef, state, measuredHeight, onLoad, onError } = useVisiblePageImage();
  const frameStyle: CSSProperties | undefined = measuredHeight > 0
    ? { minHeight: `${measuredHeight}px` }
    : undefined;
  return (
    <div className="evidence-page-image-frame" ref={frameRef} style={frameStyle}>
      {(state === 'loading' || state === 'loaded') && (
        <img
          className="evidence-page-image"
          src={imageUrl}
          alt={`Source page ${page.page}`}
          data-testid="evidence-page-image"
          onLoad={(event) => onLoad(event.currentTarget.getBoundingClientRect().height)}
          onError={onError}
        />
      )}
      {state === 'waiting' && (
        <div className="evidence-page-image-pending" data-testid="evidence-page-image-pending">
          Page image loads when visible.
        </div>
      )}
      {state === 'error' && (
        <div className="evidence-page-image-pending" data-testid="evidence-page-image-error">
          Could not render page {page.page}.
        </div>
      )}
      {(state === 'loading' || state === 'loaded') && page.regions.map((region) => (
        <RegionOverlay key={region.stable_id} region={region} emphasized={emphasizedSpanIds.includes(region.stable_id)} />
      ))}
    </div>
  );
}

function useVisiblePageImage(): {
  frameRef: RefObject<HTMLDivElement>;
  state: 'waiting' | 'loading' | 'loaded' | 'error';
  measuredHeight: number;
  onLoad(height: number): void;
  onError(): void;
} {
  const frameRef = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'waiting' | 'loading' | 'loaded' | 'error'>(
    typeof IntersectionObserver === 'undefined' ? 'loading' : 'waiting',
  );
  const [measuredHeight, setMeasuredHeight] = useState(0);
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame || typeof IntersectionObserver === 'undefined') return;
    let alive = true;
    const observer = new IntersectionObserver((entries) => {
      const entry = entries.find((candidate) => candidate.target === frame);
      if (!alive || !entry) return;
      setState((current) => entry.isIntersecting
        ? (current === 'loaded' || current === 'error' ? current : 'loading')
        : 'waiting');
    }, { rootMargin: '240px 0px' });
    observer.observe(frame);
    return () => {
      alive = false;
      observer.disconnect();
    };
  }, []);
  return {
    frameRef,
    state,
    measuredHeight,
    onLoad: (height) => {
      if (height > 0) setMeasuredHeight(height);
      setState('loaded');
    },
    onError: () => setState('error'),
  };
}

export function PdfPacketSourceCard({
  data,
  splitAt,
  onOpenSiblingSheet,
}: {
  data: PdfPacketSourceCardData;
  splitAt: string;
  onOpenSiblingSheet(sheetId: number): void;
}) {
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourcePage, setSourcePage] = useState(data.pageStart);
  const splitDate = new Date(splitAt);
  const dateLabel = Number.isNaN(splitDate.getTime())
    ? null
    : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(splitDate);
  return (
    <section className="pdf-packet-source-card" data-testid="pdf-packet-source-card">
      <div className="pdf-packet-source-thumb" aria-label={`Page ${data.pageStart} of source packet`}>
        <Suspense fallback={<FileText size={24} aria-hidden />}>
          <PdfViewer
            url={data.blobUrl}
            layout="single"
            fit="page"
            zoom={1}
            textLayer={false}
            currentPage={data.pageStart}
            onLoaded={ignorePdfPageCount}
            onCurrentPageChange={ignorePdfPageChange}
          />
        </Suspense>
      </div>
      <div className="pdf-packet-source-summary">
        <span className="pdf-packet-source-kicker">Source packet</span>
        <strong>{data.filename}</strong>
        <span>
          {data.pageStart === data.pageEnd ? `Page ${data.pageStart}` : `Pages ${data.pageStart}–${data.pageEnd}`} of {data.pageCount}
          {dateLabel ? ` · split on ${dateLabel}` : ''}
        </span>
        <div className="pdf-packet-source-strip" aria-label={`Pages ${data.pageStart} through ${data.pageEnd} selected from ${data.pageCount}`}>
          {Array.from({ length: data.pageCount }, (_, index) => index + 1).map((page) => (
            <i key={page} data-selected={page >= data.pageStart && page <= data.pageEnd ? 'true' : undefined} />
          ))}
        </div>
        <div className="pdf-packet-source-actions">
          <button
            type="button"
            aria-expanded={sourceOpen}
            onClick={() => {
              setSourcePage(data.pageStart);
              setSourceOpen((open) => !open);
            }}
          >
            {sourceOpen ? 'Close source packet' : 'Open in source packet'}
          </button>
          <button
            type="button"
            disabled={data.sourceSheetId === null}
            onClick={() => {
              if (data.sourceSheetId !== null) onOpenSiblingSheet(data.sourceSheetId);
            }}
          >
            Other documents from this packet ({data.otherDocumentCount})
          </button>
        </div>
      </div>
      {sourceOpen ? (
        <div className="pdf-packet-source-reader" data-testid="pdf-packet-source-reader">
          <header>
            <button type="button" disabled={sourcePage <= 1} onClick={() => setSourcePage((page) => Math.max(1, page - 1))}>← Previous</button>
            <strong>Page {sourcePage} of {data.pageCount}</strong>
            <button type="button" disabled={sourcePage >= data.pageCount} onClick={() => setSourcePage((page) => Math.min(data.pageCount, page + 1))}>Next →</button>
          </header>
          <Suspense fallback={<FileText size={24} aria-hidden />}>
            <PdfViewer
              url={data.blobUrl}
              layout="single"
              fit="page"
              zoom={1}
              textLayer={false}
              currentPage={sourcePage}
              onLoaded={ignorePdfPageCount}
              onCurrentPageChange={setSourcePage}
            />
          </Suspense>
        </div>
      ) : null}
    </section>
  );
}
