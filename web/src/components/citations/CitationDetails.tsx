import '../PanelPrimitives.css';
import '../FormControls.css';
import './CitationDetails.css';
import { useMemo, type KeyboardEvent } from 'react';
import type { CitationArtifact, CitationSpan, CitationViewerPayload } from '../../api/open';
import { safeHttpUrl } from '../../core/safeUrl';
import { CopyButton } from '../CopyButton';
import { finiteNumber, formatMs, jsonRecord, pageAnchorId, selectorNumber } from './citationSourceModel';

const GROUNDING_DEGRADED_COPY: Record<string, string> = {
  no_word_stream:
    'The model’s quote could not be checked against positioned source text. No verified highlight is available.',
  below_threshold:
    "The model’s quote did not match the positioned source text. No verified highlight is available.",
  citation_page_out_of_range:
    'The model cited a page outside this input; no page location is shown.',
  model_bbox_unverified:
    'The model’s highlight box could not be verified against the page text, so this citation shows the page instead of a box.',
  temporal_span_clamped_to_artifact_duration_ms:
    'This segment was shortened to fit the available recording.',
};

function warningMessages(warnings: readonly string[], hasSavedTextContext = false): string[] {
  return warnings.flatMap((warning) => {
    if (warning === 'no_word_stream' && hasSavedTextContext) return [];
    return warning in GROUNDING_DEGRADED_COPY ? [GROUNDING_DEGRADED_COPY[warning]] : [];
  });
}

export function GroundingDegradedNotice({
  warnings,
  hasSavedTextContext,
}: {
  warnings: string[];
  hasSavedTextContext: boolean;
}) {
  const messages = warningMessages(warnings, hasSavedTextContext);
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

export function LinkSummary({ payload }: { payload: CitationViewerPayload }) {
  const producer = payload.link.producer;
  const sourceModel = stringField(producer, 'model') ?? stringField(producer, 'source_model');
  const groundingMethod = stringField(producer, 'grounding_method');
  return (
    <section className="panel-card">
      <div className="control-row evidence-card-head">
        <strong data-testid="evidence-link-status">{payload.link.status}</strong>
        <CopyButton text={payload.link.export_ref} testId="evidence-copy-ref" label="copy ref" />
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

export function ArtifactDetails({ artifact }: { artifact: CitationArtifact }) {
  const sourceHref = safeHttpUrl(artifact.source_url);
  return (
    <section className="panel-card" data-testid="evidence-artifact-details">
      <div className="control-row evidence-card-head">
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
              {sourceHref ? (
                <a href={sourceHref} target="_blank" rel="noopener noreferrer">
                  {artifact.source_url}
                </a>
              ) : artifact.source_url}
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
      <div className="panel-list">
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
  span: CitationSpan;
  mediaType: string;
  artifactStableId: string;
}) {
  const selectorSummary = useMemo(() => selectorLabel(span), [span]);
  const warningCopy = warningMessages(span.warnings);
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
      {span.snippet && <blockquote data-testid="evidence-snippet">{span.snippet}{span.snippet_truncated && <small className="muted"> … [excerpt]</small>}</blockquote>}
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
      {warningCopy.length > 0 && (
        <div className="panel-notice" data-tone="warning">
          {warningCopy.map((message) => <p key={message}>{message}</p>)}
        </div>
      )}
    </article>
  );
}

function stringField(record: unknown, field: string): string | null {
  const value = jsonRecord(record)?.[field];
  return typeof value === 'string' && value.trim() ? value : null;
}

function isDisplayableJsonValue(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (Array.isArray(value)) return value.length > 0;
  const record = jsonRecord(value);
  return record === null || Object.keys(record).length > 0;
}

function selectorLabel(span: CitationSpan): string {
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

function temporalLabel(span: CitationSpan): string {
  const start = selectorNumber(span, 'start_ms') ?? 0;
  const end = selectorNumber(span, 'end_ms') ?? start;
  return `${formatMs(start)}-${formatMs(end)}`;
}

function compactJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
