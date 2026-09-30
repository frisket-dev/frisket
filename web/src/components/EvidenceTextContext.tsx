import { Download } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { EvidenceArtifact } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { textSegments } from './evidenceTextSegments';

type EvidenceTextContext = NonNullable<EvidenceArtifact['text_context']>;

function PlainTextContext({
  context,
  scopeSpanId,
  highlight,
  emphasizedSpanIds,
}: {
  context: EvidenceTextContext;
  scopeSpanId?: string;
  highlight: boolean;
  emphasizedSpanIds: readonly string[];
}) {
  const ranges = highlight
    ? scopeSpanId === undefined
      ? context.ranges
      : context.ranges.filter((range) => range.span_id === scopeSpanId)
    : [];
  const segments = textSegments(context.text, ranges);
  const emphasizedRanges = context.ranges.filter((range) => emphasizedSpanIds.includes(range.span_id));
  const firstHighlightIndex = segments.findIndex((segment) => segment.highlighted);
  const firstMarkRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (firstHighlightIndex >= 0) firstMarkRef.current?.scrollIntoView({ block: 'nearest' });
  }, [firstHighlightIndex]);

  return (
    <div className="evidence-text-source" data-testid="evidence-text-source" data-source-version="saved">
      <pre className="evidence-text-body" data-testid="evidence-text-body">
        {segments.map((segment, index) => (
          segment.highlighted ? (
            <mark
              key={`${segment.start}:${segment.end}`}
              className={`evidence-text-highlight${emphasizedRanges.some((range) => range.start < segment.end && range.end > segment.start) ? ' evidence-text-highlight-emphasized' : ''}`}
              data-testid="evidence-text-highlight"
              data-emphasized={emphasizedRanges.some((range) => range.start < segment.end && range.end > segment.start) ? 'true' : undefined}
              ref={index === firstHighlightIndex ? firstMarkRef : undefined}
            >
              {segment.text}
            </mark>
          ) : (
            <span key={`${segment.start}:${segment.end}`}>{segment.text}</span>
          )
        ))}
      </pre>
      {highlight && context.ranges.length > 0 && firstHighlightIndex < 0 && (
        <div className="evidence-viewer-warning" data-testid="evidence-text-context-unavailable">
          This citation has no displayable passage in the saved source.
        </div>
      )}
    </div>
  );
}

export function SavedTextContext(props: Parameters<typeof PlainTextContext>[0]) {
  return props.context.transcript
    ? <TimestampedTextContext {...props} />
    : <PlainTextContext {...props} />;
}

function TimestampedTextContext({ context, scopeSpanId, highlight, emphasizedSpanIds }: Parameters<typeof PlainTextContext>[0]) {
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
  const ranges = highlight ? context.ranges.filter((range) => !scopeSpanId || range.span_id === scopeSpanId) : [];
  const mediaProps = { controls: true, preload: 'metadata', src: blob?.url, ref: (node: HTMLMediaElement | null) => { playerRef.current = node; }, 'aria-label': artifact?.title || 'Source recording' };
  return <div className="evidence-annotated-transcript" data-testid="evidence-annotated-transcript">
    <div className="evidence-annotated-player">
      {blob && (artifact?.media_type.startsWith('video/') ? <video {...mediaProps}><track kind="captions" /></video> : <audio {...mediaProps}><track kind="captions" /></audio>)}
      {!blob && <span className="muted">{source?.key === key ? 'Recording unavailable. Saved transcript shown below.' : 'Loading recording…'}</span>}
    </div>
    <div className="evidence-annotated-lines">
      {transcript.segments.map((segment) => {
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
