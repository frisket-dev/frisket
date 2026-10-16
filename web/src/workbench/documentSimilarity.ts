import type { ProjectSearchAnchorWire } from '../api/projectSearch';

type CellAnchor = Extract<ProjectSearchAnchorWire, { kind: 'cell' }>;
type CellLocation = NonNullable<CellAnchor['location']>;

export function documentSimilarityLocation(target: {
  kind: string;
  page?: number;
  timeSeconds?: number;
}): { label: string; location: CellLocation } {
  const seconds = target.timeSeconds;
  const isAudio = target.kind === 'audio' && seconds !== undefined;
  const audioStart = isAudio ? Math.max(0, seconds - 2.5) : null;
  const time = seconds === undefined
    ? null
    : {
        start_seconds: isAudio ? audioStart ?? 0 : seconds,
        end_seconds: isAudio ? (audioStart ?? 0) + 5 : seconds,
      };
  const pages = target.page === undefined
    ? null
    : { start: target.page, end: target.page };
  const locationKind = target.kind === 'pdf'
    ? 'document_page'
    : target.kind === 'audio'
      ? 'audio_window'
      : target.kind === 'video'
        ? 'video_frame'
        : target.kind;
  const label = pages
    ? `page ${pages.start}`
    : time
      ? `${target.kind} ${time.start_seconds.toFixed(1)}–${time.end_seconds.toFixed(1)}s`
      : target.kind;
  return {
    label,
    location: {
      kind: locationKind,
      precision: pages || time ? 'exact' : 'source_unit',
      text: null,
      pages,
      time,
      source_span_ids: [],
    },
  };
}
