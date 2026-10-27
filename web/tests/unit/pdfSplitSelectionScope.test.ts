import { describe, expect, it } from 'vitest';
import { selectionScope } from '../../src/components/pdfSplit/selectionScope';

describe('PDF split selected scope', () => {
  it('passes complete stable IDs without a range lookup', () => {
    expect(selectionScope({ sheetId: '1', rowIds: ['9', '12'], rowIndexes: [2, 3] })).toEqual({ selected_row_ids: [9, 12] });
  });
  it('keeps cached anchors attached to their indexes for a partial selection', () => {
    expect(selectionScope({ sheetId: '1', rowIds: ['9', '14'], rowIndexes: [2, 3, 4, 8], cachedAnchors: [{ index: 2, rowId: '9' }, { index: 8, rowId: '14' }] })).toEqual({ selected_index_ranges: [{ start: 2, end: 4 }, { start: 8, end: 8 }], cached_anchors: [{ index: 2, row_id: 9 }, { index: 8, row_id: 14 }] });
  });
  it('refuses a partial selection without an anchor', () => {
    expect(() => selectionScope({ sheetId: '1', rowIds: [], rowIndexes: [1, 2] })).toThrow(/Refresh/);
  });
});
