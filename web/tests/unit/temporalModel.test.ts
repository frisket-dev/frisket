import { describe, expect, it } from 'vitest';
import {
  formatTimecode,
  parseTimeInput,
  parseTimelineValue,
  summarizeTemporalCell,
} from '../../src/temporal/model';

const TIMELINE = {
  artifact_stable_id: 'source_artifact:7d976e75-8a2b-4b5a-b084-54e59139a001',
  fingerprint: `sha256:${'a'.repeat(64)}`,
  duration_ms: 300_000,
};

describe('temporal input and display model', () => {
  it('accepts bare seconds, timecodes, and explicit units', () => {
    expect(parseTimeInput('01:30')).toEqual({ ok: true, ms: 90_000 });
    expect(parseTimeInput('00:01:30.250')).toEqual({ ok: true, ms: 90_250 });
    expect(parseTimeInput('1.5m')).toEqual({ ok: true, ms: 90_000 });
    expect(parseTimeInput('90000ms')).toEqual({ ok: true, ms: 90_000 });
    expect(parseTimeInput('60')).toEqual({ ok: true, ms: 60_000 });
    expect(parseTimeInput('60.25')).toEqual({ ok: true, ms: 60_250 });
    expect(parseTimeInput('16.1')).toEqual({ ok: true, ms: 16_100 });
    expect(parseTimeInput('32.2')).toEqual({ ok: true, ms: 32_200 });
    expect(parseTimeInput('1.005')).toEqual({ ok: true, ms: 1_005 });
    expect(parseTimeInput('00:00:01,500')).toEqual({ ok: true, ms: 1_500 });
    expect(parseTimeInput('1.5ms')).toEqual({
      ok: false,
      error: 'That value does not resolve to a whole millisecond.',
    });
  });

  it('formats canonical milliseconds without implying a frame rate', () => {
    expect(formatTimecode(0)).toBe('0:00');
    expect(formatTimecode(62_305)).toBe('1:02.305');
    expect(formatTimecode(3_661_000)).toBe('1:01:01');
  });

  it('summarizes all four canonical temporal values', () => {
    const point = JSON.stringify({
      schema_version: 'frisket.timeline_point.v1',
      timeline: TIMELINE,
      item: { id: 'tp_1', at_ms: 62_000 },
    });
    const points = JSON.stringify({
      schema_version: 'frisket.timeline_points.v1',
      timeline: TIMELINE,
      items: [{ id: 'tp_1', at_ms: 10_000 }, { id: 'tp_2', at_ms: 20_000 }],
    });
    const range = JSON.stringify({
      schema_version: 'frisket.timeline_range.v1',
      timeline: TIMELINE,
      item: { id: 'tr_1', start_ms: 10_000, end_ms: 20_000 },
    });
    const ranges = JSON.stringify({
      schema_version: 'frisket.timeline_ranges.v1',
      timeline: TIMELINE,
      items: [
        { id: 'tr_1', start_ms: 10_000, end_ms: 20_000 },
        { id: 'tr_2', start_ms: 30_000, end_ms: 40_000 },
      ],
    });

    expect(summarizeTemporalCell(point, 'timeline_point').label).toBe('1:02');
    expect(summarizeTemporalCell(points, 'timeline_points').label).toBe('2 timestamps');
    expect(summarizeTemporalCell(range, 'timeline_range').label).toBe('0:10 – 0:20');
    expect(summarizeTemporalCell(ranges, 'timeline_ranges').label).toBe('2 time ranges');
  });

  it('accepts canonical anchors with an explicitly unknown duration', () => {
    const point = JSON.stringify({
      schema_version: 'frisket.timeline_point.v1',
      timeline: { ...TIMELINE, duration_ms: null },
      item: { id: 'tp_1', at_ms: 62_000 },
    });

    expect(parseTimelineValue(point, 'timeline_point')).not.toBeNull();
    expect(summarizeTemporalCell(point, 'timeline_point')).toMatchObject({
      label: '1:02',
      invalid: false,
    });
  });

  it('rejects mismatched schemas, duplicate ids, reversed ranges, and out-of-duration values', () => {
    const duplicateIds = JSON.stringify({
      schema_version: 'frisket.timeline_points.v1',
      timeline: TIMELINE,
      items: [{ id: 'same', at_ms: 10_000 }, { id: 'same', at_ms: 20_000 }],
    });
    const reversed = JSON.stringify({
      schema_version: 'frisket.timeline_range.v1',
      timeline: TIMELINE,
      item: { id: 'tr_1', start_ms: 20_000, end_ms: 10_000 },
    });
    const pastDuration = JSON.stringify({
      schema_version: 'frisket.timeline_point.v1',
      timeline: TIMELINE,
      item: { id: 'tp_1', at_ms: 300_001 },
    });

    expect(parseTimelineValue(duplicateIds, 'timeline_points')).toBeNull();
    expect(parseTimelineValue(reversed, 'timeline_range')).toBeNull();
    expect(parseTimelineValue(pastDuration, 'timeline_point')).toBeNull();
    expect(summarizeTemporalCell(duplicateIds, 'timeline_points')).toMatchObject({ invalid: true });
  });
});
