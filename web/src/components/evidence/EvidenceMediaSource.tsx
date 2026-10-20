import './EvidenceReading.css';
import { Download, ExternalLink } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { EvidenceArtifact, EvidenceCitationRun, EvidenceRegion, EvidenceSpan } from '../../api/open';
import { RegionOverlay } from './EvidenceRegionOverlay';
import { formatMs, jsonRecord, selectorNumber } from './evidenceSourceModel';

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

export function EvidenceMediaSource({ artifact, scopeSpanId, emphasizedSpanIds }: {
  artifact: EvidenceArtifact;
  scopeSpanId?: string;
  emphasizedSpanIds: readonly string[];
}) {
  const blob = artifact.artifact_ref.blob;
  const captionsUrl = mediaCaptionsDataUrl(artifact);
  const label = artifact.title || artifact.filename || artifact.media_type;
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const temporalSpans = temporalSpansSorted(artifact);
  const initialSpan = temporalSpans.find((span) => span.stable_id === scopeSpanId) ?? temporalSpans[0];
  const initialMs = selectorNumber(initialSpan, 'start_ms') ?? 0;

  // Persistently highlight required support spans.

  const citedSpanIds = useMemo(
    () =>
      new Set(
        temporalSpans.flatMap((span) => (span.required ? [span.stable_id] : [])),
      ),
    [temporalSpans],
  );
  const scrollSpanId = temporalSpans.find((span) => span.stable_id === scopeSpanId)?.stable_id
    ?? temporalSpans.find((span) => citedSpanIds.has(span.stable_id))?.stable_id
    ?? null;

  const displayRuns = useMemo(
    () => buildTemporalDisplayRuns(temporalSpans, artifact.runs),
    [temporalSpans, artifact.runs],
  );
  const citedRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (scrollSpanId) citedRef.current?.scrollIntoView({ block: 'nearest' });
  }, [scrollSpanId]);

  const [seekRequest, setSeekRequest] = useState<{ seconds: number } | null>(() =>
    initialSpan ? { seconds: initialMs / 1000 } : null,
  );
  const seekSeconds = seekRequest?.seconds ?? null;
  const [playbackMs, setPlaybackMs] = useState(initialMs);
  const [activeSpanId, setActiveSpanId] = useState<string | null>(() =>
    initialSpan?.stable_id ?? null,
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
  }, [seekRequest, seekSeconds]);

  const setMediaRef = (el: HTMLMediaElement | null) => {
    mediaRef.current = el;
  };

  // Seek after media readiness and remounts.
  const seekAndPlay = (ms: number, spanId: string | null) => {
    setSeekRequest({ seconds: ms / 1000 });
    setPlaybackMs(ms);
    if (spanId) setActiveSpanId(spanId);
    mediaRef.current?.play().catch(() => {
      // Browser autoplay may reject playback.

    });
  };

  const handleTimeUpdate = (event: { currentTarget: HTMLMediaElement }) => {
    const ms = event.currentTarget.currentTime * 1000;
    setPlaybackMs(ms);
    const match = temporalSpans.find((span) => {
      const start = selectorNumber(span, 'start_ms') ?? 0;
      const end = selectorNumber(span, 'end_ms') ?? start;
      return ms >= start && ms < end;
    });
    if (match) setActiveSpanId(match.stable_id);
  };

  if (!blob || !(artifact.media_type.startsWith('audio/') || artifact.media_type.startsWith('video/'))) {
    const sourceUrl = blob?.url ?? [artifact.source_url, artifact.canonical_url, artifact.artifact_ref.source_url]
      .find((url) => typeof url === 'string' && /^https?:\/\//i.test(url));
    return (
      <div className="evidence-page-fallback" data-testid="evidence-artifact-fallback">
        A preview is not available for this source.
        {sourceUrl && <a href={sourceUrl} target="_blank" rel="noopener noreferrer">Open source</a>}
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
    <div className="evidence-video-frame">
      <video {...shared} data-testid="evidence-video" aria-label={`Evidence video: ${label}`}>
        <track kind="captions" src={captionsUrl} srcLang="en" label="Evidence captions" default />
      </video>
      {artifact.spans.filter((span) => {
        const start = selectorNumber(span, 'start_ms');
        const end = selectorNumber(span, 'end_ms');
        return start !== null && end !== null && playbackMs >= start && playbackMs <= end;
      }).map((span) => <RegionOverlay key={span.stable_id}
        region={{ id: span.id, stable_id: span.stable_id, bbox: (jsonRecord(span.selector)?.bbox ?? null) as EvidenceRegion['bbox'], snippet: span.quote || span.snippet, raw: span.raw }}
        emphasized={emphasizedSpanIds.includes(span.stable_id)} coordinateSpace="frame_normalized" />)}
    </div>
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
                  className="evidence-temporal-run-clip evidence-contextual-download"
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
                  <span key={span.stable_id} className="evidence-transcript-line" data-testid="evidence-transcript-line">
                    <button
                      type="button"
                      ref={span.stable_id === scrollSpanId ? citedRef : undefined}
                      className={`evidence-temporal-segment${isActive ? ' evidence-temporal-segment-active' : ''}${isCited ? ' evidence-temporal-segment-cited' : ''}${emphasizedSpanIds.includes(span.stable_id) ? ' evidence-temporal-segment-emphasized' : ''}`}
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
                            className="evidence-temporal-segment-action evidence-contextual-download"
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

// Stable ordering shared by captions and the segment list.
function temporalSpansSorted(artifact: EvidenceArtifact): EvidenceSpan[] {
  return artifact.spans
    .filter((span) => span.span_kind === 'temporal' && (span.quote || span.snippet))
    .slice()
    .sort((a, b) => (selectorNumber(a, 'start_ms') ?? 0) - (selectorNumber(b, 'start_ms') ?? 0));
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
