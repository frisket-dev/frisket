import './EvidenceReading.css';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { EvidenceArtifact, EvidenceSpan, EvidenceTextPassageTarget } from '../../api/open';
import { readEvidenceTextResource } from '../../api/raw/blobText';
import { Button } from '../Button';
import { PanelLoading } from '../PanelPrimitives';
import { SavedTextContext } from '../EvidenceTextContext';
import { textSegments, type TextSegment } from '../evidenceTextSegments';
import { isTextRenderableMediaType, jsonRecord, textCitedSpans } from './evidenceSourceModel';

export type RecoveryState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'error'; message: string };

export function TextPassageRecovery({
  artifact,
  recovery,
  onLocateCurrent,
}: {
  artifact: EvidenceArtifact;
  recovery: RecoveryState;
  onLocateCurrent?: () => void;
}) {
  if (
    artifact.text_context_status !== 'stale'
    || !isTextRenderableMediaType(artifact.media_type)
  ) {
    return null;
  }

  const notice = artifact.recovery_notice?.trim();
  if (artifact.recovery_status === 'located') {
    return (
      <div className="panel-notice" data-tone="warning" data-testid="evidence-text-recovery-located">
        {notice || 'Found this passage in the current source. The original citation remains marked stale.'}
      </div>
    );
  }
  if (artifact.recovery_status === 'incomplete') {
    return <div className="panel-notice" data-tone="warning" data-testid="evidence-text-recovery-incomplete" role="status">
      {notice || 'The source was only partly searched. This passage may exist beyond the searched portion.'}
      {typeof artifact.recovery_scanned_characters === 'number' && <span> Searched {artifact.recovery_scanned_characters.toLocaleString()} characters.</span>}
    </div>;
  }
  if (artifact.recovery_status === 'not_found') {
    return (
      <div className="panel-notice" data-tone="warning" data-testid="evidence-text-recovery-not-found">
        {notice || 'Could not find this passage in the current source.'}
      </div>
    );
  }
  if (artifact.recovery_status === 'unsupported') {
    return (
      <div className="panel-notice" data-tone="warning" data-testid="evidence-text-recovery-unsupported">
        {notice || 'This source cannot be searched for the passage.'}
      </div>
    );
  }

  return (
    <div className="evidence-text-recovery" data-testid="evidence-text-recovery-stale">
      <div>The source text has changed since this citation was created.</div>
      {recovery.phase === 'error' && (
        <div className="evidence-text-recovery-error" role="status">
          Could not search the current source: {recovery.message}
        </div>
      )}
      {onLocateCurrent && (
        <Button
          type="button"

          disabled={recovery.phase === 'loading'}
          onClick={onLocateCurrent}
        >
          {recovery.phase === 'loading' ? 'Looking for passage…' : 'Try to find this passage'}
        </Button>
      )}
    </div>
  );
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
            {(span.quote_truncated || span.snippet_truncated) && <small className="muted"> … [excerpt]</small>}
          </mark>
        ))}
      </div>
    </div>
  );
}

/* Legacy quote-only records have no persisted coordinates. */
function highlightQuotes(sourceText: string, spans: EvidenceSpan[]): TextSegment[] {
  const ranges: Array<{ start: number; end: number }> = [];
  for (const span of spans) {
    const quote = span.quote || span.snippet;
    if (!quote) continue;
    const start = sourceText.indexOf(quote);
    if (start === -1) continue;
    ranges.push({ start, end: start + quote.length });
  }
  return textSegments(sourceText, ranges);
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

    const controller = new AbortController();
    readEvidenceTextResource(blobUrl, controller.signal)
      .then((text) => {
        if (alive) setState({ phase: 'done', text });
      })
      .catch(() => {
        if (alive) setState({ phase: 'error' });
      });
    return () => {
      alive = false;
      controller.abort();
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
          const key = `${segment.start}:${segment.end}`;
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

export function TextArtifactSource({
  artifact,
  textPassageTargets,
  playFocusedPassage,
  scopeSpanId,
  scopeSpanIds,
  highlight = true,
  emphasizedSpanIds = [],
}: {
  artifact: EvidenceArtifact;
  textPassageTargets?: readonly EvidenceTextPassageTarget[];
  playFocusedPassage?: boolean;
  scopeSpanId?: string;
  /** Matching spans to highlight alongside the primary focused span. */
  scopeSpanIds?: readonly string[];
  highlight?: boolean;
  emphasizedSpanIds?: readonly string[];
}) {
  const context = artifact.text_context ?? null;
  if (context !== null) {
    return <SavedTextContext context={context} artifact={artifact} textPassageTargets={textPassageTargets} playFocusedPassage={playFocusedPassage} scopeSpanId={scopeSpanId} scopeSpanIds={scopeSpanIds} highlight={highlight} emphasizedSpanIds={emphasizedSpanIds} />;
  }
  return <LegacyTextArtifactSource artifact={artifact} />;
}

function LegacyTextArtifactSource({ artifact }: { artifact: EvidenceArtifact }) {
  const spans = useMemo(() => textCitedSpans(artifact), [artifact]);
  if (spans.length === 0) return null;
  const blob = artifact.artifact_ref.blob;
  if (blob) return <TextBlobSource artifact={artifact} blobUrl={blob.url} spans={spans} />;
  return <TextQuoteBlocks artifact={artifact} spans={spans} />;
}
