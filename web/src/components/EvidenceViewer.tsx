import { Check, Copy, Download, ExternalLink, FileText, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';
import {
  type EvidenceArtifact,
  type EvidenceCitationRun,
  type EvidencePage,
  type EvidenceRegion,
  type EvidenceSpan,
  type EvidenceViewerPayload,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { readEvidenceTextResource } from '../api/raw/blobText';
import { PanelLoading } from './PanelPrimitives';

export interface EvidenceViewerProps {
  evidenceLinkId: string | number;
  mode?: 'peek' | 'pane';
  onClose(): void;
  onOpenCompanion?(): void;
  /** A no-match row scope falls back to all source artifacts. */
  scopeRowId?: string | null;
  defaultShowDetails?: boolean;
}

type ViewerState =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | { phase: 'done'; payload: EvidenceViewerPayload };

export function EvidenceViewer({
  evidenceLinkId,
  mode = 'peek',
  onClose,
  onOpenCompanion,
  scopeRowId = null,
  defaultShowDetails = true,
}: EvidenceViewerProps) {
  const { projectApi } = useWorkspaceStores();
  const [state, setState] = useState<ViewerState>({ phase: 'loading' });
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    let alive = true;
    projectApi
      .getEvidenceViewer(evidenceLinkId)
      .then((payload) => {
        if (alive) setState({ phase: 'done', payload });
      })
      .catch((error: Error) => {
        if (alive) setState({ phase: 'error', message: error.message });
      });
    return () => {
      alive = false;
    };
  }, [evidenceLinkId]);

  useEffect(() => {
    if (mode !== 'peek') return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, [mode]);

  const content = (
    <>
      <EvidenceViewerHeader
        state={state}
        canOpenCompanion={mode === 'peek' && onOpenCompanion !== undefined}
        onOpenCompanion={onOpenCompanion}
        onClose={onClose}
      />
      {state.phase === 'loading' && (
        <PanelLoading testId="evidence-viewer-loading" label="Loading evidence…" />
      )}
      {state.phase === 'error' && (
        <div className="evidence-viewer-warning" data-testid="evidence-viewer-error">
          Could not load evidence: {state.message}
        </div>
      )}
      {state.phase === 'done' && (
        <EvidencePayloadView
          payload={state.payload}
          scopeRowId={scopeRowId}
          defaultShowDetails={defaultShowDetails}
        />
      )}
    </>
  );

  if (mode === 'pane') {
    return (
      <section className="evidence-viewer-shell evidence-viewer-pane" data-testid="evidence-viewer" aria-label="Evidence viewer">
        {content}
      </section>
    );
  }

  return (
    <div className="evidence-viewer-backdrop" data-testid="evidence-viewer">
      <dialog
        ref={dialogRef}
        className="evidence-viewer-shell"
        aria-label="Evidence viewer"
        onCancel={(event) => {
          event.preventDefault();
          onClose();
        }}
      >
        {content}
      </dialog>
    </div>
  );
}

function EvidenceViewerHeader({
  state,
  canOpenCompanion,
  onOpenCompanion,
  onClose,
}: {
  state: ViewerState;
  canOpenCompanion: boolean;
  onOpenCompanion?: () => void;
  onClose(): void;
}) {
  const sourceProvenance = state.phase === 'done' && state.payload.link.role === 'source_provenance';
  return (
    <header className="evidence-viewer-header">
      <div>
        <h2>{sourceProvenance ? 'Source provenance' : 'Evidence'}</h2>
        {state.phase === 'done' && (
          <div className="evidence-viewer-subtitle">
            {sourceProvenance ? 'derived conversion' : state.payload.link.role} · {state.payload.link.status}
          </div>
        )}
      </div>
      <div className="evidence-viewer-header-actions">
        {canOpenCompanion && (
          <button
            type="button"
            className="icon-btn"
            aria-label="Open evidence beside grid"
            title="Open evidence beside grid"
            onClick={onOpenCompanion}
          >
            <ExternalLink size={16} />
          </button>
        )}
        <button type="button" className="icon-btn" aria-label="Close evidence viewer" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
    </header>
  );
}

function EvidencePayloadView({
  payload,
  scopeRowId = null,
  defaultShowDetails = true,
}: {
  payload: EvidenceViewerPayload;
  scopeRowId?: string | null;
  defaultShowDetails?: boolean;
}) {
  const orderedArtifacts = useMemo(() => {
    if (scopeRowId === null) return payload.artifacts;
    const scoped = payload.artifacts.filter(
      (artifact) => String(artifact.source_cell?.row_id ?? '') === scopeRowId,
    );
    return scoped.length > 0 ? scoped : payload.artifacts;
  }, [payload.artifacts, scopeRowId]);
  const [showDetails, setShowDetails] = useState(defaultShowDetails);
  return (
    <div
      className={`evidence-viewer-content${showDetails ? '' : ' evidence-viewer-content-compact'}`}
    >
      <main className="evidence-viewer-source-pane">
        {payload.link.role === 'source_provenance' && (
          <div className="evidence-viewer-provenance-notice" data-testid="evidence-provenance-notice">
            This is the source document used to create the derived value. It is provenance,
            not a quoted passage supporting the converted text.
          </div>
        )}
        {orderedArtifacts.length === 0 ? (
          <div className="evidence-viewer-warning" data-testid="evidence-viewer-empty">
            This evidence link has no source spans.
          </div>
        ) : (
          orderedArtifacts.map((artifact) => (
            <ArtifactSource key={artifact.stable_id} artifact={artifact} />
          ))
        )}
        <button
          type="button"
          className="evidence-details-toggle"
          data-testid="evidence-details-toggle"
          onClick={() => setShowDetails((value) => !value)}
        >
          {showDetails ? 'Hide details' : 'Show details'}
        </button>
      </main>
      {showDetails && (
        <aside className="evidence-viewer-detail-pane" data-testid="evidence-viewer-detail-pane">
          <LinkSummary payload={payload} />
          {payload.warnings.length > 0 && (
            <div className="evidence-viewer-warning" data-testid="evidence-viewer-warnings">
              {payload.warnings.join(', ')}
            </div>
          )}
          <GroundingDegradedNotice warnings={payload.warnings} />
          {orderedArtifacts.map((artifact) => (
            <ArtifactDetails key={artifact.stable_id} artifact={artifact} />
          ))}
        </aside>
      )}
    </div>
  );
}

const GROUNDING_DEGRADED_COPY: Record<string, string> = {
  no_word_stream:
    'No positioned OCR/text stream for this source; run OCR to enable page highlights.',
  model_bbox_unverified:
    'The model’s highlight box could not be verified against the page text, so this citation shows the page instead of a box.',
};

function GroundingDegradedNotice({ warnings }: { warnings: string[] }) {
  const messages = warnings.flatMap(
    (warning) =>
      warning in GROUNDING_DEGRADED_COPY ? [GROUNDING_DEGRADED_COPY[warning]] : [],
  );
  if (messages.length === 0) {
    return null;
  }
  return (
    <div
      className="evidence-viewer-degraded-notice"
      data-testid="evidence-grounding-degraded"
    >
      {messages.map((message) => (
        <p key={message}>{message}</p>
      ))}
    </div>
  );
}

function LinkSummary({ payload }: { payload: EvidenceViewerPayload }) {
  const producer = payload.link.producer;
  const sourceModel = stringField(producer, 'model') ?? stringField(producer, 'source_model');
  const groundingMethod = stringField(producer, 'grounding_method');
  return (
    <section className="evidence-card evidence-link-summary">
      <div className="evidence-card-head">
        <strong data-testid="evidence-link-status">{payload.link.status}</strong>
        <CopyRefButton text={payload.link.export_ref} />
      </div>
      <dl className="evidence-meta-grid">
        <dt>ref</dt>
        <dd data-testid="evidence-export-ref">{payload.link.export_ref}</dd>
        <dt>role</dt>
        <dd>{payload.link.role}</dd>
        {payload.link.confidence !== null && (
          <>
            <dt>confidence</dt>
            <dd>{Math.round(payload.link.confidence * 100)}%</dd>
          </>
        )}
        {groundingMethod && (
          <>
            <dt>grounding</dt>
            <dd>{groundingMethod}</dd>
          </>
        )}
        {sourceModel && (
          <>
            <dt>model</dt>
            <dd>{sourceModel}</dd>
          </>
        )}
        {payload.link.stale_reason && (
          <>
            <dt>stale reason</dt>
            <dd data-testid="evidence-stale-reason">{payload.link.stale_reason}</dd>
          </>
        )}
      </dl>
    </section>
  );
}

export function ArtifactSource({ artifact }: { artifact: EvidenceArtifact }) {
  const blob = artifact.artifact_ref.blob;
  return (
    <section className="evidence-source-section" data-testid="evidence-artifact">
      <header className="evidence-artifact-title">
        <FileText size={15} />
        <div>
          <strong>{artifact.title || artifact.filename || artifact.media_type}</strong>
          <span>{artifact.media_type}</span>
        </div>
        {blob && (
          <a className="mini-btn" href={blob.url} target="_blank" rel="noopener noreferrer">
            <ExternalLink size={12} /> source
          </a>
        )}
      </header>
      {artifact.pages.length > 0 ? (
        artifact.pages.map((page) => (
          <EvidencePageView key={page.page} page={page} artifactStableId={artifact.stable_id} />
        ))
      ) : isTextRenderableMediaType(artifact.media_type) && textCitedSpans(artifact).length > 0 ? (
        <TextArtifactSource artifact={artifact} />
      ) : artifact.media_type === 'application/pdf' && blob ? (
        <iframe
          className="evidence-pdf"
          data-testid="evidence-pdf"
          src={blob.url}
          title={`Evidence PDF: ${artifact.title || artifact.filename || artifact.media_type}`}
        />
      ) : (
        <MediaOrFallback artifact={artifact} />
      )}
    </section>
  );
}

function pageAnchorId(artifactStableId: string, page: number): string {
  return `evidence-page-${artifactStableId}-${page}`;
}

function EvidencePageView({
  page,
  artifactStableId,
}: {
  page: EvidencePage;
  artifactStableId: string;
}) {
  if (!page.image) {
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
      <div className="evidence-page-image-frame">
        <img
          className="evidence-page-image"
          src={page.image.url}
          alt={`Source page ${page.page}`}
          data-testid="evidence-page-image"
        />
        {page.regions.map((region) => (
          <RegionOverlay key={region.stable_id} region={region} />
        ))}
      </div>
      {page.text && <pre className="evidence-page-text" data-testid="evidence-page-text">{page.text}</pre>}
    </div>
  );
}

function RegionOverlay({ region }: { region: EvidenceRegion }) {
  const normalized = normalizedBBox(region.bbox);
  if (!normalized) return null;
  return (
    <span
      className="evidence-region"
      data-testid="evidence-region-highlight"
      title={region.snippet ?? region.stable_id}
      style={normalizedRegionStyle(normalized)}
    />
  );
}

// Group adjacent temporal spans into display runs.

const CLIENT_RUN_GAP_TOLERANCE_MS = 1500;

interface TemporalDisplayRun {
  start: number;
  end: number;
  spans: EvidenceSpan[];
  clipUrl: string | null;
  openUrl: string | null;
}

function buildTemporalDisplayRuns(
  spans: EvidenceSpan[],
  backendRuns: EvidenceCitationRun[],
): TemporalDisplayRun[] {
  const runs: TemporalDisplayRun[] = [];
  for (const span of spans) {
    const start = selectorNumber(span, 'start_ms') ?? 0;
    const end = selectorNumber(span, 'end_ms') ?? start;
    const last = runs[runs.length - 1];
    if (last && start - last.end <= CLIENT_RUN_GAP_TOLERANCE_MS) {
      last.end = Math.max(last.end, end);
      last.spans.push(span);
    } else {
      runs.push({ start, end, spans: [span], clipUrl: null, openUrl: null });
    }
  }
  const backendRunByIndex = new Map(backendRuns.map((run) => [run.index, run]));
  for (const run of runs) {
    const runIndexes = new Set(
      run.spans
        .map((span) => span.run_index)
        .filter((index): index is number => index !== null && index !== undefined),
    );
    if (runIndexes.size === 1) {
      const [index] = [...runIndexes];
      run.clipUrl = backendRunByIndex.get(index)?.clip_url ?? null;
    }

    run.openUrl = run.spans.find((span) => span.deep_link_url)?.deep_link_url ?? null;
  }
  return runs;
}

function MediaOrFallback({ artifact }: { artifact: EvidenceArtifact }) {
  const blob = artifact.artifact_ref.blob;
  const captionsUrl = mediaCaptionsDataUrl(artifact);
  const label = artifact.title || artifact.filename || artifact.media_type;
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const temporalSpans = temporalSpansSorted(artifact);

  // Persistently highlight required support spans.

  const citedSpanIds = useMemo(
    () =>
      new Set(
        temporalSpans.flatMap((span) => (span.required ? [span.stable_id] : [])),
      ),
    [temporalSpans],
  );
  const firstCitedId = temporalSpans.find((span) => citedSpanIds.has(span.stable_id))?.stable_id ?? null;

  const displayRuns = useMemo(
    () => buildTemporalDisplayRuns(temporalSpans, artifact.runs),
    [temporalSpans, artifact.runs],
  );
  const citedRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (firstCitedId) citedRef.current?.scrollIntoView({ block: 'nearest' });
  }, [firstCitedId]);

  const [seekSeconds, setSeekSeconds] = useState<number | null>(() =>
    temporalSpans.length === 0 ? null : (selectorNumber(temporalSpans[0], 'start_ms') ?? 0) / 1000,
  );
  const [activeSpanId, setActiveSpanId] = useState<string | null>(() =>
    temporalSpans.length === 0 ? null : temporalSpans[0].stable_id,
  );

  useEffect(() => {
    if (seekSeconds === null || !Number.isFinite(seekSeconds)) return;
    const el = mediaRef.current;
    if (!el) return;
    const apply = () => {
      try {
        el.currentTime = seekSeconds;
      } catch {
        // Seeking can fail before media metadata is available.
      }
    };
    if (el.readyState >= 1) {
      apply();
      return undefined;
    }
    el.addEventListener('loadedmetadata', apply, { once: true });
    return () => el.removeEventListener('loadedmetadata', apply);
  }, [seekSeconds]);

  const setMediaRef = (el: HTMLMediaElement | null) => {
    mediaRef.current = el;
  };

  // Seek after media readiness and remounts.
  const seekAndPlay = (ms: number, spanId: string | null) => {
    setSeekSeconds(ms / 1000);
    if (spanId) setActiveSpanId(spanId);
    mediaRef.current?.play().catch(() => {
      // Browser autoplay may reject playback.

    });
  };

  const handleTimeUpdate = (event: { currentTarget: HTMLMediaElement }) => {
    const ms = event.currentTarget.currentTime * 1000;
    const match = temporalSpans.find((span) => {
      const start = selectorNumber(span, 'start_ms') ?? 0;
      const end = selectorNumber(span, 'end_ms') ?? start;
      return ms >= start && ms < end;
    });
    if (match) setActiveSpanId(match.stable_id);
  };

  if (!blob || !(artifact.media_type.startsWith('audio/') || artifact.media_type.startsWith('video/'))) {
    return (
      <div className="evidence-page-fallback" data-testid="evidence-artifact-fallback">
        No first-party renderer is available for this source artifact. The source ref and span selectors remain inspectable.
      </div>
    );
  }

  const isVideo = artifact.media_type.startsWith('video/');
  const seekAttr =
    seekSeconds === null || !Number.isFinite(seekSeconds) ? undefined : String(Math.round(seekSeconds));
  const shared = {
    ref: setMediaRef,
    className: 'evidence-media',
    src: blob.url,
    controls: true,
    preload: 'metadata' as const,
    'data-seek-seconds': seekAttr,
    onTimeUpdate: handleTimeUpdate,
  };

  const player = isVideo ? (
    <video {...shared} data-testid="evidence-video" aria-label={`Evidence video: ${label}`}>
      <track kind="captions" src={captionsUrl} srcLang="en" label="Evidence captions" default />
    </video>
  ) : (
    <audio {...shared} data-testid="evidence-audio" aria-label={`Evidence audio: ${label}`}>
      <track kind="captions" src={captionsUrl} srcLang="en" label="Evidence captions" default />
    </audio>
  );

  if (temporalSpans.length === 0) return player;

  const rangeStart = selectorNumber(temporalSpans[0], 'start_ms') ?? 0;
  const rangeEnd = selectorNumber(temporalSpans[temporalSpans.length - 1], 'end_ms')
    ?? selectorNumber(temporalSpans[temporalSpans.length - 1], 'start_ms')
    ?? rangeStart;

  return (
    <div className="evidence-temporal-layout" data-testid="evidence-temporal-layout">

      <div className="evidence-temporal-player-sticky">
        {player}
        {temporalSpans.length > 1 && (
          <div className="evidence-temporal-range" data-testid="evidence-temporal-range">
            {formatMs(rangeStart)}–{formatMs(rangeEnd)}
          </div>
        )}
      </div>
      <div className="evidence-temporal-segments" data-testid="evidence-temporal-segments">
        {displayRuns.map((run) => (
          <div key={run.spans[0].stable_id} className="evidence-temporal-run" data-testid="evidence-temporal-run">
            <div className="evidence-temporal-run-header">
              <button
                type="button"
                className="evidence-temporal-run-range"
                data-testid="evidence-temporal-run-range"
                onClick={() => seekAndPlay(run.start, run.spans[0].stable_id)}
              >
                {formatMs(run.start)}–{formatMs(run.end)}
              </button>
              {run.clipUrl && (
                <a
                  className="evidence-temporal-run-clip"
                  data-testid="evidence-temporal-run-clip"
                  href={run.clipUrl}
                  download
                  title="Download this run's clip"
                >
                  <Download size={12} aria-hidden="true" /> download
                </a>
              )}
              {run.openUrl && (
                <a
                  className="evidence-temporal-run-open"
                  data-testid="evidence-temporal-run-open"
                  href={run.openUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title="Open at this timestamp on the source platform"
                >
                  <ExternalLink size={12} aria-hidden="true" /> open
                </a>
              )}
            </div>
            <p className="evidence-temporal-run-text" data-testid="evidence-temporal-run-text">
              {run.spans.map((span, i) => {
                const start = selectorNumber(span, 'start_ms') ?? 0;
                const isActive = span.stable_id === activeSpanId;
                const isCited = citedSpanIds.has(span.stable_id);
                return (
                  <span key={span.stable_id}>
                    <button
                      type="button"
                      ref={span.stable_id === firstCitedId ? citedRef : undefined}
                      className={`evidence-temporal-segment${isActive ? ' evidence-temporal-segment-active' : ''}${isCited ? ' evidence-temporal-segment-cited' : ''}`}
                      data-testid="evidence-temporal-segment"
                      data-active={isActive ? 'true' : undefined}
                      data-cited={isCited ? 'true' : undefined}
                      onClick={() => seekAndPlay(start, span.stable_id)}
                    >
                      <span className="evidence-temporal-segment-marker">{formatMs(start)}</span>{' '}
                      {span.quote || span.snippet || '(no transcript text)'}
                    </button>
                    {/* Render deep links only from guarded backend metadata. */}
                    {(span.deep_link_url || span.clip_url) && (
                      <span
                        className="evidence-temporal-segment-actions"
                        data-testid="evidence-temporal-segment-actions"
                      >
                        {span.deep_link_url && (
                          <a
                            className="evidence-temporal-segment-action"
                            data-testid="evidence-temporal-deep-link"
                            href={span.deep_link_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            title="Open at this timestamp on the source platform"
                          >
                            <ExternalLink size={12} aria-hidden="true" />
                          </a>
                        )}
                        {span.clip_url && (
                          <a
                            className="evidence-temporal-segment-action"
                            data-testid="evidence-temporal-download-clip"
                            href={span.clip_url}
                            download
                            title="Download this clip"
                          >
                            <Download size={12} aria-hidden="true" />
                          </a>
                        )}
                      </span>
                    )}
                    {i < run.spans.length - 1 ? ' ' : ''}
                  </span>
                );
              })}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

const TEXT_RENDERABLE_MEDIA_TYPES = new Set(['text/plain', 'application/vnd.frisket.row+json']);

function isTextRenderableMediaType(mediaType: string): boolean {
  return TEXT_RENDERABLE_MEDIA_TYPES.has(mediaType.split(';')[0].trim());
}

function textCitedSpans(artifact: EvidenceArtifact): EvidenceSpan[] {
  const seen = new Set<string>();
  const spans: EvidenceSpan[] = [];
  for (const span of artifact.spans) {
    if (span.span_kind !== 'text' && span.span_kind !== 'whole') continue;
    const text = span.quote || span.snippet;
    if (!text || seen.has(text)) continue;
    seen.add(text);
    spans.push(span);
  }
  return spans;
}

/* Source columns are candidates, not source cell text. */
function sourceColumnNames(artifact: EvidenceArtifact): string[] {
  const raw = jsonRecord(artifact.metadata)?.source_columns;
  if (!Array.isArray(raw)) return [];
  return raw.filter((value): value is string => typeof value === 'string');
}

/* Row artifacts have no blob; cited quotes are the honest surface. */
function TextQuoteBlocks({ artifact, spans }: { artifact: EvidenceArtifact; spans: EvidenceSpan[] }) {
  const firstBlockRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    firstBlockRef.current?.scrollIntoView({ block: 'nearest' });
  }, []);
  const sourceColumns = useMemo(() => sourceColumnNames(artifact), [artifact]);
  const fieldLabel =
    sourceColumns.length === 1
      ? `Field: ${sourceColumns[0]}`
      : sourceColumns.length > 1
        ? `Candidate source columns: ${sourceColumns.join(', ')}`
        : null;
  return (
    <div className="evidence-text-source" data-testid="evidence-text-source">
      {fieldLabel && (
        <div className="evidence-text-field-label" data-testid="evidence-text-field-label">
          {fieldLabel}
        </div>
      )}
      <div className="evidence-text-quotes">
        {spans.map((span, index) => (
          <mark
            key={span.stable_id}
            className="evidence-text-highlight evidence-text-quote-block"
            data-testid="evidence-text-highlight"
            ref={index === 0 ? firstBlockRef : undefined}
          >
            {span.quote || span.snippet}
          </mark>
        ))}
      </div>
    </div>
  );
}

interface TextSegment {
  text: string;
  highlighted: boolean;
}

/* Never fabricate unavailable highlights. */
function highlightQuotes(sourceText: string, spans: EvidenceSpan[]): TextSegment[] {
  const ranges: Array<{ start: number; end: number }> = [];
  for (const span of spans) {
    const quote = span.quote || span.snippet;
    if (!quote) continue;
    const start = sourceText.indexOf(quote);
    if (start === -1) continue;
    ranges.push({ start, end: start + quote.length });
  }
  ranges.sort((a, b) => a.start - b.start);
  const merged: Array<{ start: number; end: number }> = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range.start <= last.end) {
      last.end = Math.max(last.end, range.end);
    } else {
      merged.push({ ...range });
    }
  }
  if (merged.length === 0) return [{ text: sourceText, highlighted: false }];
  const segments: TextSegment[] = [];
  let cursor = 0;
  for (const range of merged) {
    if (range.start > cursor) segments.push({ text: sourceText.slice(cursor, range.start), highlighted: false });
    segments.push({ text: sourceText.slice(range.start, range.end), highlighted: true });
    cursor = range.end;
  }
  if (cursor < sourceText.length) segments.push({ text: sourceText.slice(cursor), highlighted: false });
  return segments;
}

type TextBlobState =
  | { phase: 'loading' }
  | { phase: 'error' }
  | { phase: 'done'; text: string };

function TextBlobSource({
  artifact,
  blobUrl,
  spans,
}: {
  artifact: EvidenceArtifact;
  blobUrl: string;
  spans: EvidenceSpan[];
}) {
  const [state, setState] = useState<TextBlobState>({ phase: 'loading' });
  useEffect(() => {
    let alive = true;

    readEvidenceTextResource(blobUrl)
      .then((text) => {
        if (alive) setState({ phase: 'done', text });
      })
      .catch(() => {
        if (alive) setState({ phase: 'error' });
      });
    return () => {
      alive = false;
    };
  }, [blobUrl]);

  const segments = state.phase === 'done' ? highlightQuotes(state.text, spans) : null;
  const firstHighlightIndex = segments ? segments.findIndex((segment) => segment.highlighted) : -1;
  const firstMarkRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (firstHighlightIndex >= 0) firstMarkRef.current?.scrollIntoView({ block: 'nearest' });
  }, [firstHighlightIndex]);

  if (state.phase === 'loading') {
    return (
      <div className="evidence-text-source" data-testid="evidence-text-source">
        <PanelLoading testId="evidence-text-loading" label="Loading source text…" />
      </div>
    );
  }
  // Never fabricate unavailable highlights.

  if (!segments || firstHighlightIndex < 0) {
    return <TextQuoteBlocks artifact={artifact} spans={spans} />;
  }
  return (
    <div className="evidence-text-source" data-testid="evidence-text-source">
      <pre className="evidence-text-body" data-testid="evidence-text-body">
        {segments.map((segment, index) => {
          const key = `${segment.text}-${segment.highlighted}`;
          return segment.highlighted ? (
            <mark
              key={key}
              className="evidence-text-highlight"
              data-testid="evidence-text-highlight"
              ref={index === firstHighlightIndex ? firstMarkRef : undefined}
            >
              {segment.text}
            </mark>
          ) : (
            <span key={key}>{segment.text}</span>
          );
        })}
      </pre>
    </div>
  );
}

export function TextArtifactSource({ artifact }: { artifact: EvidenceArtifact }) {
  const spans = useMemo(() => textCitedSpans(artifact), [artifact]);
  if (spans.length === 0) return null;
  const blob = artifact.artifact_ref.blob;
  if (blob) return <TextBlobSource artifact={artifact} blobUrl={blob.url} spans={spans} />;
  return <TextQuoteBlocks artifact={artifact} spans={spans} />;
}

// Stable ordering shared by captions and the segment list.
function temporalSpansSorted(artifact: EvidenceArtifact): EvidenceSpan[] {
  return artifact.spans
    .filter((span) => span.span_kind === 'temporal' && (span.quote || span.snippet))
    .slice()
    .sort((a, b) => (selectorNumber(a, 'start_ms') ?? 0) - (selectorNumber(b, 'start_ms') ?? 0));
}

function ArtifactDetails({ artifact }: { artifact: EvidenceArtifact }) {
  return (
    <section className="evidence-card" data-testid="evidence-artifact-details">
      <div className="evidence-card-head">
        <strong>{artifact.title || artifact.filename || artifact.stable_id}</strong>
        <span>{artifact.spans.length} span{artifact.spans.length === 1 ? '' : 's'}</span>
      </div>
      <dl className="evidence-meta-grid">
        <dt>artifact ref</dt>
        <dd>{artifact.export_ref}</dd>
        {artifact.source_url && (
          <>
            <dt>url</dt>
            <dd>
              <a href={artifact.source_url} target="_blank" rel="noopener noreferrer">
                {artifact.source_url}
              </a>
            </dd>
          </>
        )}
        {artifact.duration_ms !== null && (
          <>
            <dt>duration</dt>
            <dd>{formatMs(artifact.duration_ms)}</dd>
          </>
        )}
      </dl>
      <div className="evidence-span-list">
        {artifact.spans.map((span) => (
          <SpanCard
            key={span.stable_id}
            span={span}
            mediaType={artifact.media_type}
            artifactStableId={artifact.stable_id}
          />
        ))}
      </div>
    </section>
  );
}

function SpanCard({
  span,
  mediaType,
  artifactStableId,
}: {
  span: EvidenceSpan;
  mediaType: string;
  artifactStableId: string;
}) {
  const selectorSummary = useMemo(() => selectorLabel(span), [span]);
  const pageTarget = selectorNumber(span, 'page_start');
  const jumpToPage = pageTarget === null
    ? undefined
    : () => {
        document
          .getElementById(pageAnchorId(artifactStableId, pageTarget))
          ?.scrollIntoView({ behavior: 'auto', block: 'start' });
      };
  return (
    <article
      className={`evidence-span${jumpToPage ? ' evidence-span-navigable' : ''}`}
      data-testid={`evidence-span-${span.span_kind}`}
      {...(jumpToPage
        ? {
            role: 'button',
            tabIndex: 0,
            onClick: jumpToPage,
            onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                jumpToPage();
              }
            },
          }
        : {})}
    >
      <header>
        <strong>{span.span_kind}</strong>
        {span.required && <span className="evidence-required">required</span>}
        {jumpToPage && (
          <span className="evidence-span-goto" data-testid="evidence-span-goto-page">
            go to page {pageTarget}
          </span>
        )}
      </header>
      {span.snippet && <blockquote data-testid="evidence-snippet">{span.snippet}</blockquote>}
      <dl className="evidence-meta-grid evidence-span-meta">
        <dt>span ref</dt>
        <dd>{span.export_ref}</dd>
        <dt>selector</dt>
        <dd data-testid="evidence-selector-summary">{selectorSummary}</dd>
        {span.text_layer_hash && (
          <>
            <dt>text layer</dt>
            <dd>{span.text_layer_hash}</dd>
          </>
        )}
        {isDisplayableJsonValue(span.raw) && (
          <>
            <dt>raw</dt>
            <dd>{compactJson(span.raw)}</dd>
          </>
        )}
        {mediaType.startsWith('audio/') || mediaType.startsWith('video/') ? (
          <>
            <dt>clip</dt>
            <dd data-testid="evidence-temporal-clip">{temporalLabel(span)}</dd>
          </>
        ) : null}
      </dl>
      {span.warnings.length > 0 && (
        <div className="evidence-viewer-warning">{span.warnings.join(', ')}</div>
      )}
    </article>
  );
}

function CopyRefButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="mini-btn"
      data-testid="evidence-copy-ref"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />} {copied ? 'copied' : 'copy ref'}
    </button>
  );
}

function stringField(record: unknown, field: string): string | null {
  const value = jsonRecord(record)?.[field];
  return typeof value === 'string' && value.trim() ? value : null;
}

function normalizedBBox(boxes: unknown): {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
} | null {
  if (!Array.isArray(boxes)) return null;
  for (const value of boxes) {
    const box = jsonRecord(value);
    if (!box) continue;
    if (box.space !== 'page_normalized' && box.space !== 'frame_normalized') continue;
    const x0 = finiteNumber(box.x0);
    const y0 = finiteNumber(box.y0);
    const x1 = finiteNumber(box.x1);
    const y1 = finiteNumber(box.y1);
    if (x0 === null || y0 === null || x1 === null || y1 === null) continue;
    return {
      x0: clamp01(Math.min(x0, x1)),
      y0: clamp01(Math.min(y0, y1)),
      x1: clamp01(Math.max(x0, x1)),
      y1: clamp01(Math.max(y0, y1)),
    };
  }
  return null;
}

function normalizedRegionStyle(box: { x0: number; y0: number; x1: number; y1: number }): CSSProperties {
  return {
    left: `${box.x0 * 100}%`,
    top: `${box.y0 * 100}%`,
    width: `${Math.max(0.01, box.x1 - box.x0) * 100}%`,
    height: `${Math.max(0.01, box.y1 - box.y0) * 100}%`,
  };
}

function finiteNumber(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function jsonRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function isDisplayableJsonValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return value.length > 0;
  const record = jsonRecord(value);
  return record === null || Object.keys(record).length > 0;
}

function selectorNumber(span: EvidenceSpan | undefined, field: string): number | null {
  return finiteNumber(span === undefined ? undefined : jsonRecord(span.selector)?.[field]);
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function selectorLabel(span: EvidenceSpan): string {
  const selector = span.selector;
  const record = jsonRecord(selector);
  const pageStart = finiteNumber(record?.page_start);
  const pageEnd = finiteNumber(record?.page_end);
  if (pageStart !== null && pageEnd !== null && pageStart !== pageEnd) {
    return `pages ${pageStart}-${pageEnd}`;
  }
  if (pageStart !== null) return `page ${pageStart}`;
  const start = finiteNumber(record?.start_ms);
  const end = finiteNumber(record?.end_ms);
  if (start !== null || end !== null) return `${formatMs(start ?? 0)}-${formatMs(end ?? start ?? 0)}`;
  if (span.span_kind === 'html') return compactJson(record?.html ?? selector);
  if (span.span_kind === 'table') return compactJson(record?.table ?? selector);
  return compactJson(selector);
}

function temporalLabel(span: EvidenceSpan): string {
  const start = selectorNumber(span, 'start_ms') ?? 0;
  const end = selectorNumber(span, 'end_ms') ?? start;
  return `${formatMs(start)}-${formatMs(end)}`;
}

function mediaCaptionsDataUrl(artifact: EvidenceArtifact): string {
  const temporalSpans = artifact.spans.filter((span) => (
    span.span_kind === 'temporal' && (span.quote || span.snippet)
  ));
  const cues = temporalSpans.map((span, index) => {
    const start = selectorNumber(span, 'start_ms') ?? 0;
    const end = selectorNumber(span, 'end_ms') ?? start + 5000;
    const text = (span.quote || span.snippet || '').replace(/\r?\n+/g, ' ').trim();
    return `${index + 1}\n${formatVttTime(start)} --> ${formatVttTime(Math.max(end, start + 1000))}\n${text}`;
  });
  const vtt = `WEBVTT\n\n${cues.join('\n\n')}`;
  return `data:text/vtt;charset=utf-8,${encodeURIComponent(vtt)}`;
}

function formatVttTime(ms: number): string {
  const whole = Math.max(0, Math.round(ms));
  const hours = Math.floor(whole / 3_600_000);
  const minutes = Math.floor((whole % 3_600_000) / 60_000);
  const seconds = Math.floor((whole % 60_000) / 1000);
  const millis = whole % 1000;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`;
}

function formatMs(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function compactJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
