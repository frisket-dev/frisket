import { describe, expect, it } from 'vitest';
import {
  parsePointDraftText,
  parseRangeDraftText,
  replaceTimelineItemsFromDraft,
  timelineItemsDraftText,
} from '../../src/temporal/draft';

const TIMELINE = {
  artifact_stable_id: 'source_artifact:interview-video',
  fingerprint: `sha256:${'a'.repeat(64)}`,
  duration_ms: 90_000,
};

describe('paste-friendly temporal cell drafts', () => {
  it('formats points without exposing canonical JSON, then replaces only items', () => {
    const value = {
      schema_version: 'frisket.timeline_points.v1',
      timeline: TIMELINE,
      items: [
        { id: 'old-1', at_ms: 10_000, label: 'Opening, claim', metadata: { score: 0.9 } },
      ],
    };

    expect(timelineItemsDraftText(value, 'timeline_points')).toBe('0:10,"Opening, claim"');
    const replaced = replaceTimelineItemsFromDraft(
      value,
      'timeline_points',
      '0:15,First change\n0:45\tResponse',
    );

    expect(replaced.ok).toBe(true);
    if (!replaced.ok) return;
    const parsed = replaced.value;
    expect(parsed.schema_version).toBe('frisket.timeline_points.v1');
    expect(parsed.timeline).toEqual(TIMELINE);
    expect('items' in parsed ? parsed.items : null).toEqual([
      { id: 'manual-1', at_ms: 15_000, label: 'First change' },
      { id: 'manual-2', at_ms: 45_000, label: 'Response' },
    ]);
  });

  it('uses the same range syntax, allows clearing items, and validates the source duration', () => {
    const value = {
      schema_version: 'frisket.timeline_ranges.v1',
      timeline: TIMELINE,
      items: [{ id: 'old-1', start_ms: 5_000, end_ms: 20_000 }],
    };

    expect(timelineItemsDraftText(value, 'timeline_ranges')).toBe('0:05,0:20');
    expect(replaceTimelineItemsFromDraft(value, 'timeline_ranges', '1:00,1:31')).toMatchObject({
      ok: false,
      error: expect.stringContaining('after the source ends'),
    });

    const cleared = replaceTimelineItemsFromDraft(value, 'timeline_ranges', '');
    expect(cleared.ok).toBe(true);
    if (cleared.ok) {
      expect(cleared.value).toEqual({
        schema_version: 'frisket.timeline_ranges.v1',
        timeline: TIMELINE,
        items: [],
      });
    }
  });

  it('keeps ids and detector metadata for unchanged lines while replacing an edited cut', () => {
    const value = {
      schema_version: 'frisket.timeline_points.v1' as const,
      timeline: TIMELINE,
      items: [
        {
          id: 'scene-1',
          at_ms: 10_000,
          label: 'Opening',
          metadata: { detector: 'pyscenedetect', frame: 240 },
        },
        {
          id: 'scene-2',
          at_ms: 20_000,
          label: 'Response',
          metadata: { detector: 'pyscenedetect', frame: 480 },
        },
      ],
    };

    const replaced = replaceTimelineItemsFromDraft(
      value,
      'timeline_points',
      '0:11,Opening\n0:20,Response',
    );
    expect(replaced.ok).toBe(true);
    if (!replaced.ok || replaced.value.schema_version !== 'frisket.timeline_points.v1') return;
    expect(replaced.value.items).toEqual([
      { id: 'manual-1', at_ms: 11_000, label: 'Opening' },
      {
        id: 'scene-2',
        at_ms: 20_000,
        label: 'Response',
        metadata: { detector: 'pyscenedetect', frame: 480 },
      },
    ]);
  });

  it('edits singular values but requires exactly one observation', () => {
    const point = {
      schema_version: 'frisket.timeline_point.v1' as const,
      timeline: TIMELINE,
      item: { id: 'quote', at_ms: 10_000, metadata: { source: 'manual review' } },
    };
    const range = {
      schema_version: 'frisket.timeline_range.v1' as const,
      timeline: TIMELINE,
      item: { id: 'excerpt', start_ms: 5_000, end_ms: 20_000 },
    };

    expect(timelineItemsDraftText(point, 'timeline_point')).toBe('0:10');
    expect(replaceTimelineItemsFromDraft(point, 'timeline_point', '0:10')).toEqual({
      ok: true,
      value: point,
    });
    expect(replaceTimelineItemsFromDraft(point, 'timeline_point', '0:10\n0:20')).toEqual({
      ok: false,
      error: 'Use exactly one timestamp.',
    });

    expect(timelineItemsDraftText(range, 'timeline_range')).toBe('0:05,0:20');
    const edited = replaceTimelineItemsFromDraft(range, 'timeline_range', '0:06,0:21');
    expect(edited.ok).toBe(true);
    if (edited.ok) {
      expect(edited.value).toEqual({
        schema_version: 'frisket.timeline_range.v1',
        timeline: TIMELINE,
        item: { id: 'manual-1', start_ms: 6_000, end_ms: 21_000 },
      });
    }
  });

  it('treats an SRT decimal comma as part of the timestamp, not a CSV delimiter', () => {
    expect(parsePointDraftText('00:00:01,500')).toEqual({
      ok: true,
      items: [{ id: 1, at_ms: 1_500 }],
    });
    expect(parsePointDraftText('00:00:01,500,Opening')).toEqual({
      ok: true,
      items: [{ id: 1, at_ms: 1_500, label: 'Opening' }],
    });
    expect(parseRangeDraftText('00:00:01,500\t00:00:02,750')).toEqual({
      ok: true,
      items: [{ id: 1, start_ms: 1_500, end_ms: 2_750 }],
    });
    expect(parseRangeDraftText('00:00:01,500,00:00:02,750')).toEqual({
      ok: true,
      items: [{ id: 1, start_ms: 1_500, end_ms: 2_750 }],
    });
  });
});
