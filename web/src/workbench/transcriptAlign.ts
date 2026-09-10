// Timestamp-segment alignment for the Transcription Compare bake-off — PURE
// functions, no React/DOM.
//
// Strategy: engines that emit timestamped segments are aligned by SEGMENT
// INDEX (whisper-class engines segment on the same VAD-ish boundaries closely
// enough for an anecdote tool; no cross-engine DTW). Each aligned unit carries
// the first available engine's start time, which is the seek anchor — clicking
// a diff token seeks the player to that unit's start (the analog of OCR's
// page-click-to-scroll). When NO engine emits segments, the whole transcript
// falls back to ONE paragraph unit with a null start — the seek affordance
// hides (the honest no-timestamps case).
//
// HELD-shell note: the compare shell extraction is paused pending OCR Compare
// v2; this module is the alignment parameter that instance will plug in.

import type { TranscribeCompareSegment } from '../api/types';

export interface AlignedTranscriptUnit {
  key: string;
  /** Marker label for the unit ("0:12"); null when timestamps are absent. */
  marker: string | null;
  /** Seek anchor in seconds; null hides the seek affordance. */
  startSeconds: number | null;
  /** engineId → visible unit text, including speaker attribution when
   *  declared ('' when that engine lacks the segment). */
  textByEngine: Record<string, string>;
}

/** Preserve diarization in the flat strings consumed by the shared
 * diff/survey renderer. Without this prefix, alignment retained the segment
 * boundary but silently discarded the speaker identity at the UI boundary. */
function visibleSegmentText(segment: TranscribeCompareSegment | undefined): string {
  if (!segment) return '';
  const speaker = typeof segment.speaker === 'string' ? segment.speaker.trim() : '';
  if (!speaker) return segment.text;
  return `[${speaker}]${segment.text ? ` ${segment.text}` : ''}`;
}

/** mm:ss for a segment start (unit markers + the seek label). */
function formatClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const mm = Math.floor(total / 60);
  const ss = total % 60;
  return `${mm}:${String(ss).padStart(2, '0')}`;
}

export function alignTranscriptSegments(
  engineIds: string[],
  segmentsByEngine: Record<string, TranscribeCompareSegment[] | undefined>,
  fullTextByEngine: Record<string, string | undefined>,
): AlignedTranscriptUnit[] {
  const anySegments = engineIds.some((engineId) => (segmentsByEngine[engineId]?.length ?? 0) > 0);
  if (!anySegments) {
    // Paragraph fallback: one unit, no marker, no seek.
    const textByEngine: Record<string, string> = {};
    for (const engineId of engineIds) {
      textByEngine[engineId] = fullTextByEngine[engineId] ?? '';
    }
    return [{ key: 'transcript', marker: null, startSeconds: null, textByEngine }];
  }
  const unitCount = Math.max(
    ...engineIds.map((engineId) => segmentsByEngine[engineId]?.length ?? 0),
  );
  const units: AlignedTranscriptUnit[] = [];
  for (let index = 0; index < unitCount; index += 1) {
    let start: number | null = null;
    const textByEngine: Record<string, string> = {};
    for (const engineId of engineIds) {
      const segment = segmentsByEngine[engineId]?.[index];
      textByEngine[engineId] = visibleSegmentText(segment);
      if (start === null && segment && Number.isFinite(segment.start)) {
        start = segment.start;
      }
    }
    units.push({
      key: `seg-${index}`,
      marker: start === null ? null : formatClock(start),
      startSeconds: start,
      textByEngine,
    });
  }
  return units;
}
