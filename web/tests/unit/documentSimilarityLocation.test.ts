import { describe, expect, it } from 'vitest';

import { documentSimilarityLocation } from '../../src/workbench/documentSimilarity';

describe('document similarity locations', () => {
  it('uses an explicit five-second audio window and shows its effective bounds', () => {
    expect(documentSimilarityLocation({ kind: 'audio', timeSeconds: 1 })).toEqual({
      label: 'audio 0.0–5.0s',
      location: {
        kind: 'audio_window',
        precision: 'exact',
        text: null,
        pages: null,
        time: { start_seconds: 0, end_seconds: 5 },
        source_span_ids: [],
      },
    });
    expect(documentSimilarityLocation({ kind: 'audio', timeSeconds: 12.5 }).location.time)
      .toEqual({ start_seconds: 10, end_seconds: 15 });
  });

  it('keeps video on the exact current frame and PDF on the exact page', () => {
    expect(documentSimilarityLocation({ kind: 'video', timeSeconds: 12.5 }).location)
      .toMatchObject({
        kind: 'video_frame',
        time: { start_seconds: 12.5, end_seconds: 12.5 },
      });
    expect(documentSimilarityLocation({ kind: 'pdf', page: 4 }).location)
      .toMatchObject({
        kind: 'document_page',
        pages: { start: 4, end: 4 },
      });
  });
});
