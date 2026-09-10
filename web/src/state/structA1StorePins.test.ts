import { describe, expect, it } from 'vitest';
import { createLensViewStore } from './lensViewStore';
import {
  previewViewForSheet,
  type PreviewGridView,
} from './previewViewStore';

const preview = (sheetId: string): PreviewGridView => ({
  sheetId,
  previewId: 'preview-1',
  status: 'done',
  progress: { done: 1, total: 1 },
  result: null,
  actionName: 'Action',
  rowCount: 1,
  totalRows: 1,
  error: null,
  req: {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: 1 },
    params: {},
    output_names: {},
    idempotency_key: 'preview-1',
  },
});

describe('STRUCT-A1 lens resolve generation', () => {
  it('resetForSheetChange invalidates a token issued before the reset', () => {
    const lensView = createLensViewStore();
    const token = lensView.beginLensResolve();

    expect(lensView.isCurrentLensResolve(token)).toBe(true);
    lensView.resetForSheetChange();
    expect(lensView.isCurrentLensResolve(token)).toBe(false);
  });

  it('the exit path generation bump invalidates a token issued before exit', () => {
    const lensView = createLensViewStore();
    const token = lensView.beginLensResolve();

    // exitLensView uses beginLensResolve as its generation bump before clearing.
    lensView.beginLensResolve();
    expect(lensView.isCurrentLensResolve(token)).toBe(false);
  });
});

describe('previewViewForSheet', () => {
  it('returns null for a preview owned by another sheet', () => {
    expect(previewViewForSheet(preview('sheet-1'), 'sheet-2')).toBeNull();
  });

  it('preserves the matching preview object identity', () => {
    const view = preview('sheet-1');
    expect(previewViewForSheet(view, 'sheet-1')).toBe(view);
  });
});
