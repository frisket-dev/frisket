import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';
import type { PreviewOverlayColumn } from '../../src/api/types';
import { previewColumnDef, previewGridCell } from '../../src/grid/previewCells';

const column: PreviewOverlayColumn = {
  name: 'Cuts', columnType: 'timeline_points', format: null, hidden: false, overwritesColumnId: null,
};
const timeline = {
  schema_version: 'frisket.timeline_points.v1',
  timeline: { artifact_stable_id: 'source_artifact:preview-only', fingerprint: `sha256:${'a'.repeat(64)}`, duration_ms: 30_000 },
  items: [{ id: 'cut', at_ms: 12_500 }],
};

describe('preview cell presentation', () => {
  it.each([null, 'existing-text-column'])('uses the sampled descriptor for target %s', (overwritesColumnId) => {
    const descriptor = { ...column, overwritesColumnId, hidden: true };
    expect(previewColumnDef(descriptor)).toEqual({
      id: '__preview::Cuts', name: 'Cuts', type: 'timeline_points', format: null, defaultHidden: true,
    });
    expect(previewGridCell(descriptor, { value: timeline }, {})).toMatchObject({
      kind: GridCellKind.Custom,
      data: { kind: 'frisket-timeline', label: '0:12.500', invalid: false },
      allowOverlay: false, readonly: true,
    });
  });

  it.each([
    ['text', 'Sample'], ['number', 42], ['boolean', false],
  ])('keeps %s samples read-only even with editable options', (columnType, value) => {
    expect(previewGridCell({ ...column, columnType: String(columnType) }, { value }, { editable: true }))
      .toMatchObject({ readonly: true, allowOverlay: false });
  });

  it('retains scalar formatting and distinguishes sampled null and error', () => {
    expect(previewGridCell({ ...column, columnType: 'number', format: 'currency' }, { value: 12.5 }, {}))
      .toMatchObject({ kind: GridCellKind.Number, data: 12.5, displayData: '$12.50' });
    expect(previewGridCell(column, { value: null }, {})).toMatchObject({ readonly: true });
    expect(previewGridCell(column, { value: null, error: 'Detector failed' }, { wrap: true }))
      .toMatchObject({ displayData: '⚠ Detector failed', allowWrapping: true, readonly: true });
    expect(previewGridCell(column, undefined, {})).toMatchObject({ displayData: '', readonly: true });
  });
});
