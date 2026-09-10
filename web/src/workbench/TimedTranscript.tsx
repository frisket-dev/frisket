import { useMemo, useRef, useState } from 'react';
import {
  parseTimedTranscriptSegments,
  timedTranscriptValue,
  type TimedTranscriptDocument,
  type TimedTranscriptSegment,
} from './timedTranscriptModel';

function formatSeconds(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
    : `${minutes}:${String(remainder).padStart(2, '0')}`;
}

export function TimedTranscript({
  document,
}: {
  document: TimedTranscriptDocument;
}) {
  const transcript = useMemo(() => timedTranscriptValue(document), [document]);
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const segments = useMemo(
    () => parseTimedTranscriptSegments(transcript?.segments),
    [transcript?.segments],
  );
  const [activeIndex, setActiveIndex] = useState<number | null>(segments[0]?.index ?? null);

  const seek = (segment: TimedTranscriptSegment) => {
    const media = mediaRef.current;
    if (media) {
      media.currentTime = segment.start;
      void media.play().catch(() => undefined);
    }
    setActiveIndex(segment.index);
  };

  const onTimeUpdate = () => {
    const seconds = mediaRef.current?.currentTime;
    if (seconds === undefined) return;
    const active = segments.find((segment) => seconds >= segment.start && seconds < segment.end);
    if (active) setActiveIndex(active.index);
  };

  const player = document.kind === 'video' ? (
    <video
      ref={(node) => { mediaRef.current = node; }}
      className={document.videoClassName}
      src={document.media.url}
      aria-label={document.title}
      controls
      onTimeUpdate={onTimeUpdate}
    >
      <track kind="captions" />
    </video>
  ) : (
    <audio
      ref={(node) => { mediaRef.current = node; }}
      src={document.media.url}
      aria-label={document.title}
      controls
      onTimeUpdate={onTimeUpdate}
    >
      <track kind="captions" />
    </audio>
  );

  return (
    <div className="evidence-temporal-layout document-temporal-layout" data-testid="document-temporal-transcript">
      <div className="evidence-temporal-player-sticky">{player}</div>
      <div className="evidence-temporal-segments" data-testid="document-transcript-segments">
        {segments.length > 0 ? segments.map((segment) => (
          <button
            type="button"
            key={`${segment.index}:${segment.start}`}
            className={`evidence-temporal-segment${segment.index === activeIndex ? ' evidence-temporal-segment-active' : ''}`}
            data-testid="document-transcript-segment"
            data-active={segment.index === activeIndex ? 'true' : undefined}
            onClick={() => seek(segment)}
          >
            <span className="evidence-temporal-segment-marker">{formatSeconds(segment.start)}</span>{' '}
            {segment.speaker && <strong>{segment.speaker}: </strong>}
            {segment.text}
          </button>
        )) : transcript?.text ? (
          <p className="evidence-temporal-run-text" data-testid="document-transcript-text">{transcript.text}</p>
        ) : (
          <p className="muted" data-testid="document-transcript-empty">No transcript is available for this row.</p>
        )}
      </div>
    </div>
  );
}
