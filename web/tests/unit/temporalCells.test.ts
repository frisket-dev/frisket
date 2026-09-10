import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';
import { buildCell, typeHeaderIcon, type TimelineCell } from '../../src/grid/cells';
import { presentationFor } from '../../src/grid/typeRegistry';
import { columnDef, row } from '../support/domainFixtures';

const ANCHOR = {
  artifact_stable_id: 'source_artifact:7d976e75-8a2b-4b5a-b084-54e59139a001',
  fingerprint: `sha256:${'b'.repeat(64)}`,
  duration_ms: 120_000,
};

describe('temporal grid presentation', () => {
  it('registers four types with one read-only timeline renderer', () => {
    expect(presentationFor('timeline_point')).toMatchObject({ renderer: 'timeline', geometry: 'point', multiple: false });
    expect(presentationFor('timeline_points')).toMatchObject({ renderer: 'timeline', geometry: 'point', multiple: true });
    expect(presentationFor('timeline_range')).toMatchObject({ renderer: 'timeline', geometry: 'range', multiple: false });
    expect(presentationFor('timeline_ranges')).toMatchObject({ renderer: 'timeline', geometry: 'range', multiple: true });
    expect(typeHeaderIcon('timeline_points')).toBe('typeTimeline');
  });

  it('builds a compact custom cell instead of an editable JSON/text cell', () => {
    const col = columnDef({ id: 'cuts', name: 'cuts', type: 'timeline_points' });
    const value = JSON.stringify({
      schema_version: 'frisket.timeline_points.v1',
      timeline: ANCHOR,
      items: [{ id: 'tp_1', at_ms: 30_000 }, { id: 'tp_2', at_ms: 60_000 }],
    });
    const cell = buildCell(col, row({ cuts: value })) as TimelineCell;

    expect(cell.kind).toBe(GridCellKind.Custom);
    expect(cell.allowOverlay).toBe(false);
    expect(cell.data).toEqual({ kind: 'frisket-timeline', label: '2 timestamps', invalid: false });
    expect(cell.copyData).toBe('0:30\n1:00');
  });

  it('surfaces an invalid canonical value rather than interpreting it as text', () => {
    const col = columnDef({ id: 'range', name: 'range', type: 'timeline_range' });
    const cell = buildCell(col, row({ range: '{"start":5,"end":2}' })) as TimelineCell;
    expect(cell.data).toEqual({
      kind: 'frisket-timeline',
      label: 'Invalid temporal value',
      invalid: true,
    });
  });
});
