import { Download } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { EvidenceArtifact, EvidenceTextPassageTarget } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { TextPassageNavigation } from './TextPassageNavigation';
import { textSegments } from './evidenceTextSegments';

type EvidenceTextContext = NonNullable<EvidenceArtifact['text_context']>;

interface SavedTextContextProps {
  context: EvidenceTextContext;
  artifact?: EvidenceArtifact;
  textPassageTargets?: readonly EvidenceTextPassageTarget[];
  scopeSpanId?: string;
  scopeSpanIds?: readonly string[];
  highlight: boolean;
  emphasizedSpanIds: readonly string[];
  passagePending?: boolean;
  passageMessage?: string;
}

function PlainTextContext({ context, scopeSpanId, scopeSpanIds, highlight, emphasizedSpanIds, passagePending, passageMessage }: SavedTextContextProps) {
  const ranges = highlight ? context.ranges.filter((range) =>
    (!scopeSpanId && !scopeSpanIds?.length) || range.span_id === scopeSpanId || scopeSpanIds?.includes(range.span_id),
  ) : [];
  const segments = textSegments(context.text, ranges);
  const emphasizedRanges = context.ranges.filter((range) => emphasizedSpanIds.includes(range.span_id));
  const primaryRange = context.ranges.find((range) => range.span_id === scopeSpanId);
  const firstHighlightIndex = segments.findIndex((segment) => segment.highlighted);
  const primaryHighlightIndex = primaryRange ? segments.findIndex((segment) => segment.highlighted
    && primaryRange.start < segment.end && primaryRange.end > segment.start) : -1;
  const scrollHighlightIndex = primaryHighlightIndex >= 0 ? primaryHighlightIndex : firstHighlightIndex;
  const firstMarkRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (scrollHighlightIndex >= 0) firstMarkRef.current?.scrollIntoView({ block: 'nearest' });
  }, [scrollHighlightIndex]);

  return (
    <div className="evidence-text-source" data-testid="evidence-text-source" data-source-version="saved">
      {passagePending ? <p role="status">{passageMessage ?? 'Loading passage…'}</p> : <pre className="evidence-text-body" data-testid="evidence-text-body">
        {segments.map((segment, index) => (
          segment.highlighted ? (
            <mark
              key={`${segment.start}:${segment.end}`}
              className={`evidence-text-highlight${emphasizedRanges.some((range) => range.start < segment.end && range.end > segment.start) ? ' evidence-text-highlight-emphasized' : ''}`}
              data-testid="evidence-text-highlight"
              data-emphasized={emphasizedRanges.some((range) => range.start < segment.end && range.end > segment.start) ? 'true' : undefined}
              ref={index === scrollHighlightIndex ? firstMarkRef : undefined}
            >
              {segment.text}
            </mark>
          ) : (
            <span key={`${segment.start}:${segment.end}`}>{segment.text}</span>
          )
        ))}
      </pre>}
      {!passagePending && highlight && context.ranges.length > 0 && firstHighlightIndex < 0 && (
        <div className="evidence-viewer-warning" data-testid="evidence-text-context-unavailable">
          This citation has no displayable passage in the saved source.
        </div>
      )}
    </div>
  );
}

export function SavedTextContext(props: SavedTextContextProps) {
  if (props.context.window) return <WindowedSavedTextContext {...props} />;
  return props.context.transcript
    ? <TimestampedTextContext {...props} />
    : <PlainTextContext {...props} />;
}

interface PassageRequest {
  key: string;
  target: EvidenceTextPassageTarget;
  start?: number;
  spanId?: string;
  revision?: number;
  attempt: number;
}

/** Fetches passages while the existing inline and timestamp renderers own display. */
function WindowedSavedTextContext(props: SavedTextContextProps) {
  const { projectApi } = useWorkspaceStores();
  const { context, artifact, textPassageTargets, scopeSpanId, scopeSpanIds, emphasizedSpanIds } = props;
  const focusSpanId = scopeSpanId ?? emphasizedSpanIds[0] ?? scopeSpanIds?.[0];
  const targets = textPassageTargets ?? artifact?.spans.map((span) => ({
    spanId: span.stable_id, evidenceLinkId: context.evidence_link_id ?? '', artifactId: artifact.id,
  })) ?? [];
  const focusTarget = targets.find((target) => target.spanId === focusSpanId);
  const contextKey = [context.evidence_link_id, context.artifact_stable_id, context.content_hash,
    context.window?.start, context.window?.end, context.window?.retrieval_revision, focusSpanId,
    focusTarget?.evidenceLinkId].join(':');
  const [navigation, setNavigation] = useState<PassageRequest | null>(null);
  const needsFocus = focusTarget && !context.ranges.some((range) => range.span_id === focusSpanId);
  const request: PassageRequest | null = navigation?.key === contextKey ? navigation : needsFocus ? {
    key: contextKey, target: focusTarget, spanId: focusSpanId,
    revision: focusTarget.evidenceLinkId === context.evidence_link_id ? context.window?.retrieval_revision ?? undefined : undefined,
    attempt: 0,
  } : null;
  const requestKey = [contextKey, request?.start, request?.spanId, request?.revision,
    request?.attempt, request?.target.evidenceLinkId].join(':');
  const [settled, setSettled] = useState<{ key: string; context?: EvidenceTextContext; error?: string } | null>(null);
  useEffect(() => {
    if (!request) return;
    const controller = new AbortController();
    void projectApi.getEvidenceViewer(request.target.evidenceLinkId, {
      textArtifactId: request.target.artifactId,
      textStart: request.start, textLimit: 16_384,
      textRevision: request.revision, textSpanId: request.start === undefined ? request.spanId : undefined,
      signal: controller.signal,
    }).then((payload) => {
      if (controller.signal.aborted) return;
      const next = payload.artifacts.find((item) => item.id === request.target.artifactId)?.text_context;
      if (!next) throw new Error('This citation has no available source passage.');
      setSettled({ key: requestKey, context: next });
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setSettled({ key: requestKey, error: typeof error === 'object' && error !== null && 'status' in error && error.status === 409
        ? 'The source text changed. Reload the passage to continue.'
        : error instanceof Error ? error.message : 'Could not load this passage.' });
    });
    return () => controller.abort();
  }, [projectApi, requestKey, request?.target.evidenceLinkId, request?.target.artifactId,
    request?.start, request?.revision, request?.spanId]);
  const current = request && settled?.key === requestKey ? settled : null;
  const loading = request !== null && current === null;
  const displayed = current?.context ?? context;
  const error = current?.error;
  const window = displayed.window;
  const selectedSpan = navigation?.key === contextKey ? navigation.spanId : undefined;
  const rendered = { ...props, context: displayed, passagePending: loading || Boolean(error),
    passageMessage: error ? 'Passage unavailable.' : 'Loading passage…',
    scopeSpanId: selectedSpan ?? scopeSpanId, emphasizedSpanIds: selectedSpan ? [selectedSpan] : emphasizedSpanIds };
  const currentTarget = targets.find((target) => target.spanId === selectedSpan)
    ?? { spanId: focusSpanId ?? '', evidenceLinkId: displayed.evidence_link_id ?? '', artifactId: artifact?.id ?? 0 };
  return <div data-testid="windowed-evidence-text" aria-busy={loading}>
    {window && <TextPassageNavigation window={window} disabled={loading || Boolean(error) || !currentTarget.evidenceLinkId}
      onNavigate={(start) => setNavigation({ key: contextKey, target: currentTarget, start,
        revision: window.retrieval_revision ?? undefined, attempt: (request?.attempt ?? 0) + 1 })} />}
    {targets.length > 1 && <label>Citation passage <select aria-label="Citation passage" value={selectedSpan ?? focusSpanId ?? ''}
      disabled={loading} onChange={(event) => {
        const target = targets.find((item) => item.spanId === event.target.value);
        if (!target) return;
        setNavigation({ key: contextKey, target, spanId: target.spanId,
          revision: target.evidenceLinkId === displayed.evidence_link_id ? window?.retrieval_revision ?? undefined : undefined,
          attempt: (request?.attempt ?? 0) + 1 });
      }}>
      <option value="" disabled>Choose citation</option>
      {targets.map((target, index) => <option key={`${target.evidenceLinkId}:${target.spanId}`} value={target.spanId}>
        {(artifact?.spans.find((span) => span.stable_id === target.spanId)?.quote || `Citation ${index + 1}`).slice(0, 100)}
      </option>)}
    </select></label>}
    {error && <div role="alert">{error}<button type="button" className="mini-btn" onClick={() => setNavigation({
      key: contextKey, target: request?.target ?? currentTarget, spanId: request?.spanId ?? focusSpanId,
      attempt: (request?.attempt ?? 0) + 1,
    })}>Reload passage</button></div>}
    {displayed.transcript ? <TimestampedTextContext {...rendered} /> : <PlainTextContext {...rendered} />}
  </div>;
}

function TimestampedTextContext({ context, scopeSpanId, scopeSpanIds, highlight, emphasizedSpanIds, passagePending, passageMessage }: SavedTextContextProps) {
  const { projectApi } = useWorkspaceStores();
  const transcript = context.transcript!;
  const [source, setSource] = useState<{ key: string; artifact: EvidenceArtifact | null } | null>(null);
  const playerRef = useRef<HTMLMediaElement | null>(null);
  const key = `${transcript.evidence_link_stable_id}:${transcript.artifact_stable_id}`;
  useEffect(() => {
    let active = true;
    void projectApi.getEvidenceViewer(transcript.evidence_link_stable_id).then((payload) => {
      const artifact = payload.artifacts.find((item) => item.stable_id === transcript.artifact_stable_id) ?? null;
      if (active) setSource({ key, artifact });
    }, () => { if (active) setSource({ key, artifact: null }); });
    return () => { active = false; };
  }, [projectApi, key, transcript.evidence_link_stable_id, transcript.artifact_stable_id]);
  const artifact = source?.key === key ? source.artifact : null;
  const blob = artifact?.artifact_ref.blob;
  const [seekRequest, setSeekRequest] = useState<{ ms: number } | null>(null);
  useEffect(() => {
    const player = playerRef.current;
    if (!player || seekRequest === null) return;
    const seek = () => { player.currentTime = seekRequest.ms / 1000; void player.play().catch(() => {}); };
    if (player.readyState >= 1) { seek(); return; }
    player.addEventListener('loadedmetadata', seek, { once: true });
    return () => player.removeEventListener('loadedmetadata', seek);
  }, [seekRequest, blob?.url]);
  const sourceSpans = new Map(artifact?.spans.map((span) => [span.stable_id, span]) ?? []);
  const ranges = highlight ? context.ranges.filter((range) =>
    (!scopeSpanId && !scopeSpanIds?.length) || range.span_id === scopeSpanId || scopeSpanIds?.includes(range.span_id),
  ) : [];
  const mediaProps = { controls: true, preload: 'metadata', src: blob?.url, ref: (node: HTMLMediaElement | null) => { playerRef.current = node; }, 'aria-label': artifact?.title || 'Source recording' };
  return <div className="evidence-annotated-transcript" data-testid="evidence-annotated-transcript">
    <div className="evidence-annotated-player">
      {blob && (artifact?.media_type.startsWith('video/') ? <video {...mediaProps}><track kind="captions" /></video> : <audio {...mediaProps}><track kind="captions" /></audio>)}
      {!blob && <span className="muted">{source?.key === key ? 'Recording unavailable. Saved transcript shown below.' : 'Loading recording…'}</span>}
    </div>
    <div className="evidence-annotated-lines">
      {passagePending ? <p role="status">{passageMessage ?? 'Loading passage…'}</p> : transcript.segments.map((segment) => {
        const localRanges = ranges.filter((range) => range.start < segment.end && range.end > segment.start);
        const chunks = textSegments(context.text.slice(segment.start, segment.end), localRanges.map((range) => ({
          start: Math.max(0, range.start - segment.start), end: Math.min(segment.end, range.end) - segment.start,
        })));
        return <div className="evidence-transcript-line" data-testid="evidence-transcript-line" key={segment.span_id}>
          <button type="button" className="evidence-annotated-timestamp" disabled={!blob} onClick={() => setSeekRequest({ ms: segment.start_ms })}
            aria-label={`Play at ${formatTimestamp(segment.start_ms)}`}>{formatTimestamp(segment.start_ms)}</button>
          <span className="evidence-annotated-text">
            {segment.speaker && <strong>{segment.speaker}: </strong>}
            {chunks.map((chunk) => chunk.highlighted ? <mark key={chunk.start} className={`evidence-text-highlight${localRanges.some((range) => emphasizedSpanIds.includes(range.span_id)
                && range.start < segment.start + chunk.end && range.end > segment.start + chunk.start) ? ' evidence-text-highlight-emphasized' : ''}`} data-testid="evidence-text-highlight"
              data-emphasized={localRanges.some((range) => emphasizedSpanIds.includes(range.span_id)
                && range.start < segment.start + chunk.end && range.end > segment.start + chunk.start) ? 'true' : undefined}>{chunk.text}</mark>
              : <span key={chunk.start}>{chunk.text}</span>)}
          </span>
          {sourceSpans.get(segment.span_id)?.clip_url && <a className="evidence-contextual-download" href={sourceSpans.get(segment.span_id)!.clip_url!}
            download title="Download this clip" aria-label={`Download clip at ${formatTimestamp(segment.start_ms)}`}><Download size={14} /></a>}
        </div>;
      })}
    </div>
  </div>;
}

function formatTimestamp(ms: number) {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
